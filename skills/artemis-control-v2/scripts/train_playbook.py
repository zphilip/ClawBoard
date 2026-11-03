#!/usr/bin/env python3
"""Training harness: run the REAL agent over training-playbook tasks and score them.

This is the loop ``tests/test_playbook.py`` does NOT do. ``test_playbook.py``
only checks that a recipe's plan parses and satisfies its own guardrails
(``memory.playbook.validate_plan``) — it never touches the device or the VLM.

This script closes the loop:

    1. load every task in ``memory/training_playbook.json`` (or a subset);
    2. for each task:
       a. capture the read-only baseline (``_capture_device_baseline``), so
          RELATIVE goals ("调低亮度", "调高媒体音量") are judgeable;
       b. run the real agent (``run_artemis_agent``, ``pro`` profile) with the
          task's ``intent`` as the instruction — planner sees the recipe via
          ``build_playbook_block``, guardrails enforced every attempt;
       c. re-run the read-only probes after the run and score the result with
          the task's OWN acceptance criteria against the baseline
          (``_acceptance_from_probes`` → ``runner.acceptance``, direction-aware);
       d. append one run record to ``memory/data/training_runs.jsonl``;
       e. when a task reaches ``--min-successes`` passing runs at score
          >= ``--min-score``, promote it to ``status=verified`` in
          ``training_playbook.json`` (so ``is_trusted_for_replay`` becomes True).

Usage (on the device host):
    python3 scripts/train_playbook.py --task mute-ringer --device a60f6655
    python3 scripts/train_playbook.py --task wifi-toggle --task dnd-on --device a60f6655
    python3 scripts/train_playbook.py --all --device a60f6655
    python3 scripts/train_playbook.py --dry-run               # list tasks, no device

Exit code: 0 all selected tasks passed, 1 any failed, 2 no device / nothing ran.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT))

import artemis_agent  # noqa: E402
from artemis_agent import (  # noqa: E402
    _acceptance_from_probes,
    _adb,
    _capture_device_baseline,
    _run_check_probes,
    check_adb_device,
    ensure_screen_on,
    run_artemis_agent,
    parse_plan,
)
from memory.playbook import load_playbook  # noqa: E402

PLAYBOOK_PATH = SKILL_ROOT / "memory" / "training_playbook.json"
RUNS_PATH = SKILL_ROOT / "memory" / "data" / "training_runs.jsonl"
REPORT_PATH = SKILL_ROOT / "memory" / "data" / "training_report.md"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _task_verify_items(task) -> list[dict]:
    """The task plan's ``- verify:`` lines as [{text}, ...], for probe runs."""
    snapshot = parse_plan(task.plan)
    return [
        {"text": ci.text}
        for ci in snapshot.all_check_items
        if getattr(ci, "kind", "") == "verify"
    ]


def _fingerprint_label(baseline) -> str:
    """``gm1910_sdk30 (OnePlus 7 Pro, Android 11)`` — matches the mute recipe's
    ``verified_on`` convention."""
    if baseline is None:
        return ""
    fg = getattr(baseline, "fingerprint", "") or ""
    model = getattr(baseline, "model", "") or ""
    rel = getattr(baseline, "release", "") or ""
    if not (model or rel):
        return fg or "unknown"
    return f"{fg} ({model}, Android {rel})"


def _baseline_summary(baseline) -> dict:
    if baseline is None:
        return {}
    keys = (
        "fingerprint", "model", "sdk", "release",
        "wifi_enabled", "wifi_ssid", "wifi_ip",
        "bt_enabled", "bt_devices", "mobile_network", "sim_operator",
        "battery_level", "screen_state",
    )
    out: dict = {}
    for k in keys:
        v = getattr(baseline, k, None)
        if v is not None:
            out[k] = v
    if getattr(baseline, "volumes", None):
        out["volumes"] = {
            name: info.get("value") for name, info in baseline.volumes.items()
        }
    if getattr(baseline, "settings", None):
        out["settings"] = {
            k: v for k, v in baseline.settings.items()
            if k.endswith(("zen_mode", "wifi_on", "airplane_mode_on",
                           "bluetooth_on", "screen_brightness",
                           "screen_brightness_mode", "accelerometer_rotation"))
        }
    return out


def _shell(device: str, *argv: str, timeout: int = 10) -> tuple[int, str]:
    """Run an ``adb shell`` argv; returns (rc, stdout)."""
    rc, out, _ = _adb(["shell", *argv], device=device, timeout=timeout)
    return rc, (out or "").strip()


def _restore_baseline(device: str, baseline) -> list[str]:
    """Best-effort reset of the phone to the pre-run baseline after each item.

    The agent mutates device state while training a task (mute silences it,
    brightness moves it, dnd toggles it…). The next training item must start
    from the SAME state the previous one did, or relative acceptance and the
    planner's pre-check both fire on a polluted state. So between items we
    restore everything the baseline measured.

    Returns a list of human-readable notes about what was restored.
    """
    notes: list[str] = []
    if baseline is None:
        return ["no baseline to restore"]

    ensure_screen_on("adb", device)

    # 1. ringer mode — restore FIRST: DND/ringer state locks the stream volumes,
    #    so streams are set afterwards and zen is applied last. ``mode_ringer``
    #    lives in a ROM-dependent namespace; write to all candidates.
    if baseline.ringer_mode is not None:
        for ns in ("global", "system", "secure"):
            _shell(device, "settings", "put", ns, "mode_ringer", str(baseline.ringer_mode))
        notes.append(f"ringer_mode={baseline.ringer_mode}")

    # 2. volume streams.
    _STREAMS = (("ring", "2"), ("music", "3"), ("alarm", "4"), ("notification", "5"))
    for name, stream in _STREAMS:
        info = (baseline.volumes or {}).get(name)
        if info is None or info.get("value") is None:
            continue
        _shell(device, "media", "volume", "--stream", stream, "--set", str(info["value"]))
        notes.append(f"{name}={info['value']}")

    # 3. radios through their dedicated toggles (settings put alone does not
    #    flip the radio service; svc/cmd actually toggles it).
    wifi = (baseline.wifi_enabled
            if baseline.wifi_enabled is not None
            else baseline.setting("wifi_on"))
    if wifi is not None:
        _shell(device, "svc", "wifi", "enable" if wifi else "disable")
        notes.append("wifi=" + ("on" if wifi else "off"))

    bt = (baseline.bt_enabled
          if baseline.bt_enabled is not None
          else baseline.setting("bluetooth_on"))
    if bt is not None:
        _shell(device, "svc", "bluetooth", "enable" if bt else "disable")
        notes.append("bluetooth=" + ("on" if bt else "off"))

    airplane = baseline.setting("airplane_mode_on")
    if airplane is not None:
        _shell(device, "cmd", "connectivity", "airplane-mode",
               "enable" if airplane else "disable")
        notes.append("airplane=" + ("on" if airplane else "off"))

    # 4. everything else as a numeric setting (brightness, auto-brightness,
    #    auto-rotate, screen timeout, mobile data, vibrate, navigation mode…).
    #    The radio keys are skipped because svc/cmd already handled them.
    for full, val in (baseline.settings or {}).items():
        if "." not in full:
            continue
        ns, key = full.split(".", 1)
        if key in ("wifi_on", "bluetooth_on", "airplane_mode_on"):
            continue
        sval = str(int(val)) if isinstance(val, int) else str(val)
        _shell(device, "settings", "put", ns, key, sval)
        notes.append(f"{full}={sval}")

    # 5. zen_mode LAST: it is what actually silences this ROM (locks all the
    #    streams we just set), and it must be the final state to match.
    zen = baseline.zen_mode
    if zen is None:
        zen = baseline.setting("zen_mode")
    if zen is not None:
        _shell(device, "settings", "put", "global", "zen_mode", str(zen))
        notes.append(f"zen_mode={zen}")

    return notes


def _score_run(task, instruction: str, adb_path: str, device: str,
               max_steps: int, timeout: int, verification: str,
               baseline) -> dict:
    """Run one task and judge it. Returns the run record (dict).

    ``baseline`` is the pre-run snapshot captured by the caller (so the same
    snapshot can also drive the post-run restore).
    """
    label = _fingerprint_label(baseline)

    verify_items = _task_verify_items(task)
    verify_texts = [ci["text"] for ci in verify_items]

    t0 = time.time()
    result = run_artemis_agent(
        instruction=instruction,
        adb_path=adb_path,
        device=device,
        profile="pro",
        max_steps=max_steps,
        timeout=timeout,
        verification=verification,
        explorer_tier="pro",
    )
    elapsed = time.time() - t0

    # independent post-run probe evidence, judged against the pre-run baseline
    probe_results = _run_check_probes(verify_items, adb_path, device) if verify_items else {}
    verdict = _acceptance_from_probes(verify_texts, probe_results, baseline)

    agent_goal = None
    checker = result.get("checker") or {}
    if "goal_achieved" in checker:
        agent_goal = bool(checker["goal_achieved"])

    # decisive True wins; decisive False loses; inconclusive defers to the agent.
    if verdict is True:
        passed, why = True, "acceptance satisfied"
    elif verdict is False:
        passed, why = False, "acceptance contradicted"
    else:
        passed = bool(agent_goal)
        why = "agent checker" if agent_goal else "inconclusive + agent failed"

    verif_counts = (result.get("verification") or {})
    return {
        "task_id": task.id,
        "intent": task.intent,
        "instruction": instruction,
        "device": device,
        "fingerprint": label,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "run_id": result.get("run_id", ""),
        "status": "pass" if passed else "fail",
        "score": 1.0 if passed else 0.0,
        "verdict": verdict,
        "agent_goal_achieved": agent_goal,
        "agent_status": result.get("status", ""),
        "steps": result.get("steps", 0),
        "elapsed_s": round(elapsed, 1),
        "verification": verif_counts,
        "reason": why,
        "baseline_before": _baseline_summary(baseline),
    }


def _load_runs() -> list[dict]:
    runs: list[dict] = []
    if RUNS_PATH.exists():
        for line in RUNS_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                runs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return runs


def _promote_verified(task_id: str, min_successes: int, label: str) -> tuple[bool, str]:
    """Set ``status=verified`` when the success tally reaches the threshold.

    Returns (changed, message). Never touches the file when already verified.
    """
    runs = _load_runs()
    successes = sum(
        1 for r in runs
        if r.get("task_id") == task_id and r.get("status") == "pass"
    )
    if successes < min_successes:
        return False, f"{successes}/{min_successes} passing runs — not promoting yet"

    data = json.loads(PLAYBOOK_PATH.read_text(encoding="utf-8"))
    task = next((t for t in data.get("tasks", []) if t.get("id") == task_id), None)
    if task is None:
        return False, f"task {task_id!r} missing from playbook"
    if task.get("status") == "verified":
        return False, f"already verified on {task.get('verified_on')}"

    task["status"] = "verified"
    task["verified_on"] = label
    PLAYBOOK_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return True, f"promoted {task_id} → verified on {label}"


def _task_stats(task_id: str, runs: list[dict]) -> dict:
    own = [r for r in runs if r.get("task_id") == task_id]
    return {
        "total": len(own),
        "passes": sum(1 for r in own if r.get("status") == "pass"),
        "fails": sum(1 for r in own if r.get("status") == "fail"),
        "last": own[-1] if own else None,
    }


def _build_report(selected: list, session_results: list[tuple[str, bool]],
                  session_elapsed: float, device: str) -> str:
    """Render a markdown training report: session summary + per-task history."""
    runs = _load_runs()
    lines: list[str] = []
    lines.append("# Artemis training report")
    lines.append("")
    lines.append(f"- date: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"- device: {device}")
    lines.append("- session elapsed: {:.1f}s".format(session_elapsed))

    n = len(session_results)
    n_pass = sum(1 for _, ok in session_results if ok)
    n_fail = n - n_pass
    lines.append(f"- session: {n} ran — {n_pass} passed / {n_fail} failed "
                 f"({100 * n_pass / n:.0f}%)" if n else "- session: nothing ran")
    lines.append("")

    # cumulative totals across ALL recorded runs
    total = sum(1 for r in runs)
    total_pass = sum(1 for r in runs if r.get("status") == "pass")
    lines.append(f"## Cumulative (all recorded runs: {total})")
    lines.append(f"- {total_pass} passed / {total - total_pass} failed")

    # auto-verify progress
    verified_ids = {t.id for t in selected
                    if t.status == "verified" and t.verified_on}
    if verified_ids:
        lines.append("- verified tasks: " + ", ".join(sorted(verified_ids)))
    lines.append("")

    lines.append("## Per task")
    lines.append("")
    outcome_by_id = dict(session_results)
    for t in selected:
        st = _task_stats(t.id, runs)
        outcome = outcome_by_id.get(t.id)
        session_mark = "−" if outcome is None else ("PASS" if outcome else "FAIL")
        last = st["last"]
        last_r = ""
        if last:
            last_r = f" (last: {last.get('status')} @ {last.get('timestamp', '')[:16]})"
        verified = " ✓verified" if (t.status == "verified") else ""
        lines.append(
            f"- **{t.id}**{verified} — {t.intent}"
            f"\n    session: {session_mark} | cumulative: {st['passes']}P/{st['fails']}F"
            f" | playbook status: {t.status}{last_r}"
        )
    lines.append("")
    return "\n".join(lines)


def _emit_report(selected: list, session_results: list[tuple[str, bool]],
                 session_elapsed: float, device: str) -> None:
    """Write the report next to the run log and print it to the console."""
    report = _build_report(selected, session_results, session_elapsed, device)
    try:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(report + "\n", encoding="utf-8")
        print(f"\n📄 report → {REPORT_PATH}")
    except Exception as e:  # pragma: no cover
        print(f"  (report write failed: {e})")
    print("\n" + "=" * 60)
    print(report)
    print("=" * 60)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--task", action="append", default=[],
                    help="task id(s) to run (repeatable); default: all")
    ap.add_argument("--all", action="store_true", help="run every task in the playbook")
    ap.add_argument("--device", "-s", default=None, help="adb serial")
    ap.add_argument("--max-steps", type=int, default=30)
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--verification", choices=["final", "checkpoints", "strict"],
                    default="checkpoints")
    ap.add_argument("--min-successes", type=int, default=2,
                    help="passing runs required before a task is auto-verified")
    ap.add_argument("--no-verify-bump", action="store_true",
                    help="record runs but never edit training_playbook.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="list the tasks that WOULD run and exit (no device)")
    args = ap.parse_args()

    tasks = load_playbook(PLAYBOOK_PATH)
    by_id = {t.id: t for t in tasks}

    selected = tasks
    if args.task:
        missing = [t for t in args.task if t not in by_id]
        if missing:
            print(f"ERROR: unknown task id(s): {missing}", file=sys.stderr)
            print("Available:", ", ".join(sorted(by_id)), file=sys.stderr)
            return 1
        selected = [by_id[t] for t in args.task]

    print(f"Training playbook: {len(selected)} task(s) selected")
    for t in selected:
        runs = [r for r in _load_runs() if r.get("task_id") == t.id]
        passes = sum(1 for r in runs if r.get("status") == "pass")
        print(f"  - {t.id:<24} status={t.status:<12} intent={t.intent} "
              f"(prior passes: {passes})")

    if args.dry_run:
        return 0

    device = check_adb_device("adb", args.device)
    if device is None:
        print("SKIP: no ADB device.", file=sys.stderr)
        return 2

    ensure_screen_on("adb", device)

    RUNS_PATH.parent.mkdir(parents=True, exist_ok=True)
    results: list[tuple[str, bool]] = []
    session_started = time.time()
    for t in selected:
        print(f"\n▶ training {t.id}: {t.intent}")
        baseline = None
        try:
            baseline = _capture_device_baseline("adb", device)
            rec = _score_run(t, t.intent, "adb", device,
                             args.max_steps, args.timeout, args.verification,
                             baseline=baseline)
        except Exception as e:  # pragma: no cover - keep the loop alive
            print(f"  ✗ crashed: {e}")
            rec = {
                "task_id": t.id, "intent": t.intent, "instruction": t.intent,
                "device": device, "timestamp": datetime.now().isoformat(timespec="seconds"),
                "status": "fail", "score": 0.0, "reason": f"harness crash: {e}",
            }
        finally:
            try:
                notes = _restore_baseline(device, baseline)
                print(f"  ↺ restored to baseline: {', '.join(notes[:12])}{'…' if len(notes) > 12 else ''}")
            except Exception as e:  # pragma: no cover
                print(f"  ↺ restore failed: {e}")

        with RUNS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

        passed = rec.get("status") == "pass"
        results.append((t.id, passed))
        v = rec.get("verdict")
        v_s = "decisive" if v is not None else "inconclusive"
        print(f"  {'✓' if passed else '✗'} {rec.get('reason')} ({v_s}), "
              f"score={rec.get('score')}, steps={rec.get('steps')}, "
              f"agent={rec.get('agent_status')}/{rec.get('agent_goal_achieved')}")

        if not args.no_verify_bump:
            changed, msg = _promote_verified(
                t.id, args.min_successes, rec.get("fingerprint", "")
            )
            if changed:
                t.status = "verified"
                t.verified_on = rec.get("fingerprint", "")
                print(f"  ♻ {msg}")

    failures = [tid for tid, ok in results if not ok]
    _emit_report(selected, results, time.time() - session_started, device)
    print(f"Done: {len(results) - len(failures)}/{len(results)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())