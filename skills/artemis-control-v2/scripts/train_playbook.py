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


def _score_run(task, instruction: str, adb_path: str, device: str,
               max_steps: int, timeout: int, verification: str) -> dict:
    """Run one task and judge it. Returns the run record (dict)."""
    baseline = _capture_device_baseline(adb_path, device)
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
    for t in selected:
        print(f"\n▶ training {t.id}: {t.intent}")
        try:
            rec = _score_run(t, t.intent, "adb", device,
                             args.max_steps, args.timeout, args.verification)
        except Exception as e:  # pragma: no cover - keep the loop alive
            print(f"  ✗ crashed: {e}")
            rec = {
                "task_id": t.id, "intent": t.intent, "instruction": t.intent,
                "device": device, "timestamp": datetime.now().isoformat(timespec="seconds"),
                "status": "fail", "score": 0.0, "reason": f"harness crash: {e}",
            }

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
                print(f"  ♻ {msg}")

    failures = [tid for tid, ok in results if not ok]
    print(f"\nDone: {len(results) - len(failures)}/{len(results)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())