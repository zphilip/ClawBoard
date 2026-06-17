#!/usr/bin/env python3
"""
Integration test for plan counters — real device runs.

Runs mobile_agent.py with specified tasks (each repeated N times),
snapshots the plan store after each run, and validates that the
success/fail counter semantics hold.

USAGE:
    # Single task, repeated runs:
    python3 tests/test_plan_integration.py --task "打开淘宝搜索3D打印机" --repeat 3

    # Multiple tasks from a plan file:
    python3 tests/test_plan_integration.py --plan-file tests/tasks.txt --repeat 2

    # Dry-run: only validate existing plans.jsonl (no agent runs):
    python3 tests/test_plan_integration.py --validate-only

DESIGN:
    Rather than hardcoding expected counter values (which depend on
    unpredictable device/network/VLM behaviour), this test validates
    INVARIANTS — properties that MUST hold regardless of whether any
    particular run succeeded or failed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# ── make the memory package importable ──────────────────────────────
SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))

from memory.models import PlanStep, TaskPlan
from memory.plan_store import PlanStore, plan_is_healthy


# ══════════════════════════════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════════════════════════════

AGENT_SCRIPT = SKILL_DIR / "mobile_agent.py"
PLANS_FILE = SKILL_DIR / "memory_data" / "plans.jsonl"
EVENTS_FILE = SKILL_DIR / "memory_data" / "events.jsonl"
RECORDS_FILE = SKILL_DIR / "memory_data" / "records.jsonl"
SNAPSHOT_DIR = SKILL_DIR / "tests" / "snapshots"

DEFAULT_TASK = "打开淘宝搜索3D打印机"

# Tasks that exercise different plan behaviours
BUILTIN_TASKS = [
    "打开淘宝搜索3D打印机",
    "打开百度地图导航回家",
    "打开微信发消息给妈妈说我到家了",
]


# ══════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════

def _ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


# ── Stderr filter — only show plan-relevant messages ─────────────────

# Prefixes worth showing (plan replay, supervisor decisions, trust scoring).
# Everything else (timing, ADB, UI dump, coordinate debug) is suppressed.
_PLAN_STDERR_PREFIXES = (
    "[PLAN]", "[PLAN REC]", "[TRUST SCORE]", "[SUPERVISOR] overriding",
    "[SUPERVISOR] approved", "[SUPERVISOR] override rejected",
    "[SUPERVISOR] sending screenshot", "[SUPERVISOR] validate attempt",
    "[SUPERVISOR] error", "[SUPERVISOR] timeout",
    "[DRIVING]",  # supervisor driving mode
    "[SUPERVISOR] periodic", "[SUPERVISOR] task confirmed",
    "[MEMORY] enforce", "[MEMORY] pre-LLM fastpath",
    "[MEMORY] post-LLM confirmation",
    "[STEP SUMMARY]", "[STEP ACTION]",
    "[WARN]", "[PARSE LOOP]", "[TERMINATED]",
    "============",  # step separators
    "[VLM] provider used:", "[VLM] primary attempt",
    "[MODEL OUTPUT]", "[INPUT] ❌", "[INPUT] ✅",
    "[ACTION EXEC] ❌ TYPE FAILED",
)


def _stderr_is_relevant(line: str) -> bool:
    """Return True if *line* is worth showing in compact test output."""
    return line.startswith(_PLAN_STDERR_PREFIXES)

# Map app display-name → package for force-stop between runs.
# Extended on the fly via _resolve_package().
_APP_PACKAGE_MAP: dict[str, str] = {
    "淘宝": "com.taobao.taobao",
    "微信": "com.tencent.mm",
    "百度地图": "com.baidu.BaiduMap",
    "百度": "com.baidu.BaiduMap",
    "京东": "com.jingdong.app.mall",
    "美团": "com.sankuai.meituan",
    "饿了么": "me.ele",
    "抖音": "com.ss.android.ugc.aweme",
    "高德地图": "com.autonavi.minimap",
    "支付宝": "com.eg.android.AlipayGphone",
    "QQ": "com.tencent.mobileqq",
    "网易云音乐": "com.netease.cloudmusic",
    "QQ音乐": "com.tencent.qqmusic",
    "哔哩哔哩": "tv.danmaku.bili",
    "B站": "tv.danmaku.bili",
    "小红书": "com.xingin.xhs",
    "拼多多": "com.xunmeng.pinduoduo",
    "滴滴": "com.sdu.didi.psnger",
}


def _guess_target_package(instruction: str) -> str | None:
    """Guess which app package the instruction targets (for force-stop)."""
    norm = instruction.lower().replace(" ", "").replace("-", "")
    for name, pkg in sorted(_APP_PACKAGE_MAP.items(), key=lambda x: -len(x[0])):
        if name.lower().replace(" ", "").replace("-", "") in norm:
            return pkg
    return None


def _reset_device(
    instruction: str = "",
    adb_path: str = "adb",
    device: str | None = None,
    settle: float = 2.0,
) -> bool:
    """Reset device to a known state between test runs.

    1. Press Home to return to launcher.
    2. Force-stop the target app (so it starts fresh).
    3. Wait for the device to settle.

    Returns True if at least the Home press succeeded.
    """
    dev_flag = ["-s", device] if device else []

    # 1. Home
    print("[RESET] Pressing Home...")
    rc, _, _ = _adb(["shell", "input", "keyevent", "3"],
                     adb_path=adb_path, device=device)
    home_ok = (rc == 0)

    # 2. Force-stop target app
    pkg = _guess_target_package(instruction)
    if pkg:
        print(f"[RESET] Force-stopping {pkg}...")
        _adb(["shell", "am", "force-stop", pkg],
             adb_path=adb_path, device=device)
    else:
        print("[RESET] Could not guess target package — skipping force-stop")

    # 3. Settle
    time.sleep(settle)
    print(f"[RESET] Done (home_ok={home_ok})")
    return home_ok


def _adb(args: list[str], adb_path: str = "adb",
         device: str | None = None, timeout: int = 10) -> tuple[int, str, str]:
    """Run an adb command, return (returncode, stdout, stderr)."""
    cmd = [adb_path]
    if device:
        cmd += ["-s", device]
    cmd += args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError:
        return -1, "", "adb not found"


def _run_agent(instruction: str, extra_args: str = "", timeout: int = 600,
               compact: bool = False) -> dict:
    """Run mobile_agent.py and return parsed JSON result from stdout.

    The agent emits one JSON object per line (progress + final result).
    We capture the final ``{"type": "result", ...}`` object.

    In normal mode, agent progress is streamed in real-time.  In compact
    mode only plan-relevant stderr and the final result are shown.
    """
    cmd = [
        sys.executable, str(AGENT_SCRIPT),
        "--instruction", instruction,
        "--memory-min-score", "0.7",
        "--post-run-report",
        "--memory-decision", "enforce",
        "--memory-replay-mode", "plan",
    ]
    if extra_args:
        cmd.extend(extra_args.split())

    print(f"\n{'─'*60}")
    print(f"[TEST] Running: {' '.join(cmd)}")
    print(f"[TEST] Start: {_ts()}")
    t0 = time.time()

    # ── Stream stdout/stderr in real-time, also capture for parsing ──
    captured_stdout: list[str] = []
    captured_stderr: list[str] = []
    result_obj: dict | None = None
    rc: int | None = None

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(SKILL_DIR),
            bufsize=1,  # line-buffered
        )

        # Read both streams concurrently via threads
        def _read_stdout():
            nonlocal result_obj
            for line in iter(proc.stdout.readline, ""):
                if not line:
                    break
                stripped = line.rstrip("\n")
                captured_stdout.append(stripped)
                # Always parse for result_obj, but only print in non-compact
                try:
                    obj = json.loads(stripped)
                    t = obj.get("type", "")
                    if t == "progress":
                        if not compact:
                            step = obj.get("step", "?")
                            action = obj.get("action", "")
                            msg = obj.get("message", "")
                            if msg:
                                print(f"[AGENT] step={step}  action={action}  {msg}")
                            else:
                                print(f"[AGENT] step={step}  action={action}")
                    elif t == "result":
                        result_obj = obj
                        print(f"[AGENT] RESULT: status={obj.get('status')} steps={obj.get('steps')} "
                              f"last_action={obj.get('last_action')}")
                    elif not compact:
                        print(f"[AGENT] {stripped[:200]}")
                except json.JSONDecodeError:
                    if not compact and stripped.strip():
                        print(f"[AGENT] {stripped[:200]}")

        def _read_stderr():
            for line in iter(proc.stderr.readline, ""):
                if not line:
                    break
                stripped = line.rstrip("\n")
                captured_stderr.append(stripped)
                if compact:
                    continue  # suppress all stderr in compact mode
                if _stderr_is_relevant(stripped.strip()):
                    print(f"[AGENT] {stripped.strip()[:200]}")

        t_out = threading.Thread(target=_read_stdout, daemon=True)
        t_err = threading.Thread(target=_read_stderr, daemon=True)
        t_out.start()
        t_err.start()

        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait()
            print(f"[TEST] TIMEOUT after {timeout}s — killed agent")

        t_out.join(timeout=5)
        t_err.join(timeout=5)

    except FileNotFoundError:
        print(f"[TEST] ERROR: script not found: {AGENT_SCRIPT}")
        return {"status": "error", "steps": -1, "last_action": "", "message": f"script not found: {AGENT_SCRIPT}"}

    elapsed = time.time() - t0
    print(f"[TEST] Elapsed: {elapsed:.0f}s  Exit: {rc}")

    if result_obj is not None:
        result_obj.setdefault("_elapsed", elapsed)
        return result_obj

    # Fallback: scan captured lines for a result
    for line in captured_stdout:
        try:
            obj = json.loads(line)
            if obj.get("type") == "result":
                result_obj = obj
                break
        except json.JSONDecodeError:
            pass

    if result_obj is None:
        result_obj = {
            "status": "error",
            "steps": -1,
            "last_action": "",
            "message": f"no result JSON found; rc={rc}",
            "debug": {"last_stderr": "\n".join(captured_stderr[-20:])},
        }

    # Ensure elapsed is always present (timeout/error paths may miss it)
    result_obj.setdefault("_elapsed", elapsed)
    return result_obj


def _load_plans() -> list[dict]:
    """Load plans.jsonl as raw dicts (for snapshotting)."""
    if not PLANS_FILE.exists():
        return []
    plans = []
    for line in PLANS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            plans.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return plans


def _load_store() -> PlanStore:
    """Load the PlanStore (typed)."""
    return PlanStore(str(PLANS_FILE))


def _snapshot(label: str) -> Path:
    """Save a snapshot of plans.jsonl for later inspection."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    plans = _load_plans()
    dest = SNAPSHOT_DIR / f"snapshot_{label}_{_ts()}.json"
    dest.write_text(json.dumps(plans, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[SNAPSHOT] {label} → {dest.name}  ({len(plans)} plans)")
    return dest


def _compute_plan_delta(before_plans: list[dict], after_plans: list[dict]) -> str:
    """Return a compact string describing what changed in the plan store."""
    before_by_key = {p["intent_key"]: p for p in before_plans}
    after_by_key = {p["intent_key"]: p for p in after_plans}

    parts: list[str] = []
    all_keys = set(before_by_key) | set(after_by_key)

    for key in sorted(all_keys):
        b = before_by_key.get(key)
        a = after_by_key.get(key)
        if b is None and a is not None:
            # New plan created
            sc = a.get("success_count", 0)
            fc = a.get("fail_count", 0)
            n_steps = len(a.get("steps", []))
            h = "✓" if plan_is_healthy(_deser(a)) else "✗"
            parts.append(f"+new({n_steps}s s={sc}/f={fc} {h})")
        elif b is not None and a is None:
            parts.append(f"-removed")
        elif b is not None and a is not None:
            b_sc, a_sc = b.get("success_count", 0), a.get("success_count", 0)
            b_fc, a_fc = b.get("fail_count", 0), a.get("fail_count", 0)
            ds = a_sc - b_sc
            df = a_fc - b_fc
            if ds == 0 and df == 0:
                continue  # no change
            b_steps = {s["step_index"]: s for s in b.get("steps", [])}
            a_steps = {s["step_index"]: s for s in a.get("steps", [])}
            step_deltas = []
            for si in sorted(set(b_steps) | set(a_steps)):
                bs = b_steps.get(si, {})
                as_ = a_steps.get(si, {})
                s_ds = as_.get("success_count", 0) - bs.get("success_count", 0)
                s_df = as_.get("fail_count", 0) - bs.get("fail_count", 0)
                if s_ds != 0 or s_df != 0:
                    step_deltas.append(f"s{si}({s_ds:+d}/{s_df:+d})")
            b_h = "✓" if plan_is_healthy(_deser(b)) else "✗"
            a_h = "✓" if plan_is_healthy(_deser(a)) else "✗"
            h_str = f" {b_h}→{a_h}" if b_h != a_h else ""
            parts.append(
                f"Δ(s={ds:+d}/f={df:+d}){h_str} "
                f"[{', '.join(step_deltas)}]" if step_deltas else f"Δ(s={ds:+d}/f={df:+d}){h_str}"
            )
    return " | ".join(parts) if parts else "(no change)"


def _deser(d: dict) -> TaskPlan:
    """Convert a raw plan dict back to TaskPlan for health checks."""
    from memory.models import PlanStep as PS
    steps = [
        PS(
            step_index=s.get("step_index", i),
            action_type=s.get("action_type", ""),
            action_args=s.get("action_args", {}),
            expected_pkg=s.get("expected_pkg", ""),
            action_description=s.get("action_description", ""),
            pre_action_pkg=s.get("pre_action_pkg", ""),
            post_action_ui_fp=s.get("post_action_ui_fp", ""),
            target_element_signature=s.get("target_element_signature"),
            success_count=s.get("success_count", 0),
            fail_count=s.get("fail_count", 0),
        )
        for i, s in enumerate(d.get("steps", []))
    ]
    return TaskPlan(
        intent_key=d.get("intent_key", ""),
        instruction_sample=d.get("instruction_sample", ""),
        steps=steps,
        success_count=d.get("success_count", 0),
        fail_count=d.get("fail_count", 0),
        last_verified=d.get("last_verified", 0.0),
        created_at=d.get("created_at", 0.0),
        source_run_id=d.get("source_run_id", ""),
        device_bucket=d.get("device_bucket", "default"),
    )


def _print_plan_summary(store: PlanStore, intent_key: str | None = None) -> None:
    """Print a human-readable summary of plan counters."""
    plans = store.load()
    if intent_key:
        plans = [p for p in plans if p.intent_key == intent_key]
    print(f"\n{'Plan':<20} {'s_OK':>5} {'s_FAIL':>5} {'Score':>7} {'Healthy':>8} {'Steps':>6}")
    print("-" * 55)
    for p in plans:
        total = p.success_count + p.fail_count
        score = p.success_count / max(total, 1)
        healthy = "✓" if plan_is_healthy(p) else "✗"
        step_info = ", ".join(
            f"s{s.step_index}(+{s.success_count}/-{s.fail_count})"
            for s in p.steps
        )
        print(
            f"{p.intent_key[:18]:<20} "
            f"{p.success_count:>5} {p.fail_count:>5} "
            f"{score:>6.3f} {healthy:>8} "
            f"{len(p.steps):>3}  [{step_info}]"
        )


# ══════════════════════════════════════════════════════════════════════
# Validation — invariants that MUST hold
# ══════════════════════════════════════════════════════════════════════

@dataclass
class ValidationResult:
    name: str
    passed: bool
    detail: str = ""


def validate_plan_invariants(store: PlanStore) -> list[ValidationResult]:
    """Run all invariant checks on the current plan store.

    These are properties that MUST be true regardless of whether any
    particular agent run succeeded or failed.
    """
    results: list[ValidationResult] = []
    plans = store.load()

    # ── Structural invariants ──────────────────────────────────────
    results.append(ValidationResult(
        "no_duplicate_intent_keys",
        len(plans) == len({p.intent_key for p in plans}),
        f"{len(plans)} plans, {len({p.intent_key for p in plans})} unique keys",
    ))

    # ── Counter invariants ─────────────────────────────────────────
    for p in plans:
        prefix = f"[{p.intent_key[:12]}]"

        # Counters are never negative
        results.append(ValidationResult(
            f"{prefix} success_count >= 0",
            p.success_count >= 0,
            f"success_count={p.success_count}",
        ))
        results.append(ValidationResult(
            f"{prefix} fail_count >= 0",
            p.fail_count >= 0,
            f"fail_count={p.fail_count}",
        ))

        # last_verified only set when there's at least one success
        if p.success_count > 0:
            results.append(ValidationResult(
                f"{prefix} last_verified > 0 when success_count > 0",
                p.last_verified > 0,
                f"last_verified={p.last_verified}",
            ))

        # created_at is always set
        results.append(ValidationResult(
            f"{prefix} created_at > 0",
            p.created_at > 0,
            f"created_at={p.created_at}",
        ))

        for s in p.steps:
            sp = f"{prefix} s{s.step_index}"
            results.append(ValidationResult(
                f"{sp} success_count >= 0",
                s.success_count >= 0,
                f"success_count={s.success_count}",
            ))
            results.append(ValidationResult(
                f"{sp} fail_count >= 0",
                s.fail_count >= 0,
                f"fail_count={s.fail_count}",
            ))

    # ── Health / find_best consistency ─────────────────────────────
    for p in plans:
        prefix = f"[{p.intent_key[:12]}]"
        healthy = plan_is_healthy(p)
        found = store.find_best(p.intent_key, min_success=1)

        if healthy and p.success_count >= 1:
            # A healthy plan with enough successes should be findable
            results.append(ValidationResult(
                f"{prefix} healthy → findable by find_best",
                found is not None,
                f"healthy={healthy} success_count={p.success_count} found={'yes' if found else 'no'}",
            ))

        if not healthy:
            # An unhealthy plan should NOT be the best match
            # (but another healthy plan for the same intent might exist)
            if found is not None:
                results.append(ValidationResult(
                    f"{prefix} unhealthy plan excluded from find_best",
                    found.intent_key != p.intent_key or plan_is_healthy(found),
                    f"unhealthy plan intent={p.intent_key} found intent={found.intent_key}",
                ))

    # ── Score threshold (PlanExecutor.find_plan) ───────────────────
    from memory.plan_executor import PlanExecutor
    mock_adb = MagicMock()
    mock_adb.get_foreground_package.return_value = ""
    mock_adb.get_ui_dump.return_value = ""
    executor = PlanExecutor(store, mock_adb)

    for p in plans:
        prefix = f"[{p.intent_key[:12]}]"
        total = p.success_count + p.fail_count
        score = p.success_count / max(total, 1)
        found = executor.find_plan(p.intent_key)

        if score < 0.6 or not plan_is_healthy(p):
            results.append(ValidationResult(
                f"{prefix} score<0.6 or unhealthy → find_plan returns None",
                found is None,
                f"score={score:.3f} healthy={plan_is_healthy(p)} found={'yes' if found else 'no'}",
            ))

    return results


# ══════════════════════════════════════════════════════════════════════
# Test sequence runner
# ══════════════════════════════════════════════════════════════════════

class IntegrationTestRunner:
    """Orchestrates real-device runs and validates plan counters."""

    def __init__(
        self,
        keep_snapshots: bool = True,
        reset_device: bool = True,
        adb_path: str = "adb",
        device: str | None = None,
        compact: bool = False,
    ):
        self.keep_snapshots = keep_snapshots
        self.reset_device = reset_device
        self.adb_path = adb_path
        self.device = device
        self.compact = compact
        self.run_log: list[dict] = []

    def run_task_sequence(
        self,
        tasks: list[str],
        repeat: int = 1,
        extra_args: str = "",
        timeout: int = 600,
    ) -> None:
        """Run each task *repeat* times, snapshotting after each run.

        Between consecutive runs of the same task, the device is reset
        to the home screen and the target app is force-stopped.  This
        ensures each run starts from a known state — otherwise run N+1
        starts on the results screen of run N and plan replay fails the
        pre-check immediately.
        """
        total_runs = len(tasks) * repeat
        run_idx = 0

        for task_idx, task in enumerate(tasks):
            for rep in range(repeat):
                run_idx += 1
                label = f"task{task_idx}_{task[:12]}_rep{rep}"

                # ── Reset device BEFORE each run ───────────────────
                # Skip the very first run if the device is already at a
                # reasonable state (user may have positioned it).  All
                # subsequent repeats get a reset so they start from home.
                if self.reset_device and run_idx > 1:
                    print(f"\n[RESET] Resetting device before run {run_idx}/{total_runs}...")
                    _reset_device(
                        instruction=task,
                        adb_path=self.adb_path,
                        device=self.device,
                    )

                print(f"\n{'='*60}")
                print(f"[TEST] RUN {run_idx}/{total_runs}: {task!r}  (repeat {rep+1}/{repeat})")
                print(f"{'='*60}")

                # Snapshot BEFORE (raw dicts for delta)
                before_plans = _load_plans()
                if not self.compact:
                    _snapshot(f"{label}_BEFORE")

                # Run the agent
                result = _run_agent(task, extra_args=extra_args, timeout=timeout,
                                    compact=self.compact)

                # Snapshot AFTER
                after_plans = _load_plans()
                if not self.compact:
                    _snapshot(f"{label}_AFTER")

                # ── Plan delta ────────────────────────────────────────
                delta = _compute_plan_delta(before_plans, after_plans)
                print(f"[PLAN Δ] {delta}")

                # Log
                _elapsed = result.get("_elapsed", 0)
                entry = {
                    "run": run_idx,
                    "task": task,
                    "repeat": rep,
                    "timestamp": _ts(),
                    "status": result.get("status"),
                    "steps": result.get("steps"),
                    "elapsed": _elapsed,
                    "last_action": result.get("last_action"),
                    "message": result.get("message", ""),
                }
                self.run_log.append(entry)
                print(f"[TEST RESULT] status={result.get('status')} steps={result.get('steps')} elapsed={_elapsed:.0f}s")

                # Validate after each run (skip in compact mode — done at end)
                if not self.compact:
                    store = _load_store()
                    validations = validate_plan_invariants(store)
                    failures = [v for v in validations if not v.passed]
                    if failures:
                        print(f"[VALIDATE] ❌ {len(failures)}/{len(validations)} checks FAILED")
                    else:
                        print(f"[VALIDATE] ✅ {len(validations)} checks passed")

                # Print plan summary for this task
                from memory.signature import build_canonical_intent_key
                intent_key = build_canonical_intent_key(task)
                _print_plan_summary(_load_store(), intent_key)

        # ── Final report ────────────────────────────────────────────
        self._print_final_report(tasks)

    def _print_final_report(self, tasks: list[str]) -> None:
        """Print a comprehensive final report."""
        sep = "=" * 60
        print(f"\n\n{sep}")
        print("FINAL REPORT")
        print(sep)

        # Per-task summary
        print(f"\n{'Task':<32} {'Run':>4} {'Status':>8} {'Steps':>6} {'Time':>7} {'Plan Δ':>30}")
        print("-" * 95)
        for r in self.run_log:
            task_short = r["task"][:30]
            status = r.get("status", "?")[:8]
            steps = r.get("steps", "?")
            elapsed = r.get("elapsed", 0)
            time_str = f"{elapsed:.0f}s" if elapsed else "?"
            # Find delta for this run (computed earlier — not in log, skip)
            print(f"{task_short:<32} {r['run']:>4} {status:>8} {str(steps):>6} {time_str:>7}")

        # Timing trend: first vs last run per task
        print(f"\n{'─'*60}")
        print("TIMING TREND (plan cache should make replays faster)")
        print(f"{'─'*60}")
        for task in tasks:
            task_runs = [r for r in self.run_log if r["task"] == task]
            ok_runs = [r for r in task_runs if r.get("elapsed", 0) > 0]
            if len(ok_runs) >= 2:
                first_t = ok_runs[0]["elapsed"]
                last_t = ok_runs[-1]["elapsed"]
                delta_t = first_t - last_t
                trend = "📈 faster" if delta_t > 0 else ("📉 slower" if delta_t < 0 else "➡️  same")
                print(f"  {task[:40]:<42} {first_t:.0f}s → {last_t:.0f}s  Δ={delta_t:+.0f}s  {trend}")
            elif ok_runs:
                print(f"  {task[:40]:<42} {ok_runs[0]['elapsed']:.0f}s  (only 1 completed run)")
            else:
                print(f"  {task[:40]:<42} no completed runs")

        # Plan store state
        store = _load_store()
        plans = store.load()
        print(f"\n{'─'*60}")
        print(f"PLAN STORE ({len(plans)} plans)")
        print(f"{'─'*60}")
        _print_plan_summary(store)

        # Plan cache health analysis
        print(f"\n{'─'*60}")
        print("PLAN CACHE HEALTH")
        print(f"{'─'*60}")
        for p in plans:
            healthy = plan_is_healthy(p)
            total = p.success_count + p.fail_count
            score = p.success_count / max(total, 1)
            step_health = [
                f"s{s.step_index}(+{s.success_count}/-{s.fail_count})"
                for s in p.steps
            ]
            worst_step = max(
                (s for s in p.steps),
                key=lambda s: s.fail_count - s.success_count,
                default=None,
            )
            verdict = "✅ working" if healthy else "⚠️  needs repair"
            if worst_step and worst_step.fail_count > 0:
                verdict += f" — worst step: s{worst_step.step_index}(+{worst_step.success_count}/-{worst_step.fail_count})"
            print(f"  {p.intent_key[:30]:<32} score={score:.2f} {verdict}")
            if step_health:
                print(f"    steps: {', '.join(step_health)}")

        # Final validation (only show failures)
        validations = validate_plan_invariants(store)
        failures = [v for v in validations if not v.passed]
        if failures:
            print(f"\n[VALIDATE] ❌ {len(failures)} invariant checks FAILED:")
            for v in failures:
                print(f"  ❌ {v.name}: {v.detail}")
        else:
            print(f"\n[VALIDATE] ✅ All invariant checks passed")

        # Snapshot final state
        if not self.compact:
            _snapshot("FINAL")

        # Save run log
        log_path = SNAPSHOT_DIR / f"run_log_{_ts()}.json"
        log_path.write_text(
            json.dumps(self.run_log, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n[TEST] Run log saved: {log_path}")


# ══════════════════════════════════════════════════════════════════════
# Validate-only mode (no agent runs)
# ══════════════════════════════════════════════════════════════════════

def validate_only() -> int:
    """Run invariant checks on the existing plans.jsonl.  No agent runs."""
    if not PLANS_FILE.exists():
        print(f"[VALIDATE] No plans file at {PLANS_FILE}")
        return 1

    store = _load_store()
    plans = store.load()
    print(f"[VALIDATE] Loaded {len(plans)} plans from {PLANS_FILE}")
    _print_plan_summary(store)

    print(f"\n[VALIDATE] Running invariant checks...")
    validations = validate_plan_invariants(store)
    passed = [v for v in validations if v.passed]
    failures = [v for v in validations if not v.passed]

    for v in passed:
        print(f"  ✅ {v.name}")
    for v in failures:
        print(f"  ❌ {v.name}: {v.detail}")

    print(f"\n[VALIDATE] {len(passed)} passed, {len(failures)} failed")
    return len(failures)


# ══════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════

def main() -> int:
    p = argparse.ArgumentParser(
        description="Integration test for plan counters (real device).",
    )
    p.add_argument("--task", default="",
                   help="Single task instruction to run")
    p.add_argument("--plan-file", default="",
                   help="File with one task instruction per line")
    p.add_argument("--repeat", type=int, default=2,
                   help="How many times to repeat each task (default: 2)")
    p.add_argument("--timeout", type=int, default=600,
                   help="Timeout per agent run in seconds (default: 600)")
    p.add_argument("--extra-args", default="",
                   help="Extra CLI args to pass to mobile_agent.py")
    p.add_argument("--adb-path", default="adb",
                   help="Path to ADB binary (for device reset)")
    p.add_argument("--device", default=None,
                   help="ADB device serial (for device reset)")
    p.add_argument("--no-reset", action="store_true",
                   help="Don't reset device between runs")
    p.add_argument("--validate-only", action="store_true",
                   help="Only validate existing plans.jsonl (no agent runs)")
    p.add_argument("--no-snapshots", action="store_true",
                   help="Don't save plan snapshots")
    p.add_argument("--compact", action="store_true",
                   help="Minimal output — only plan deltas and final report")
    args = p.parse_args()

    if args.validate_only:
        return validate_only()

    # Build task list
    tasks: list[str] = []
    if args.task:
        tasks = [args.task]
    elif args.plan_file:
        tasks = [
            line.strip()
            for line in Path(args.plan_file).read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    else:
        tasks = BUILTIN_TASKS

    if not tasks:
        print("[TEST] No tasks specified.")
        return 1

    print(f"[TEST] Tasks to run ({len(tasks)} × {args.repeat} = {len(tasks) * args.repeat} runs):")
    for t in tasks:
        print(f"  • {t}")

    runner = IntegrationTestRunner(
        keep_snapshots=not args.no_snapshots,
        reset_device=not args.no_reset,
        adb_path=args.adb_path,
        device=args.device,
        compact=args.compact,
    )
    runner.run_task_sequence(
        tasks=tasks,
        repeat=args.repeat,
        extra_args=args.extra_args,
        timeout=args.timeout,
    )

    return 0


# Need this import for validate_plan_invariants
from unittest.mock import MagicMock

if __name__ == "__main__":
    sys.exit(main())
