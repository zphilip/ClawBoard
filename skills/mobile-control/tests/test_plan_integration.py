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


# ── Device reset ────────────────────────────────────────────────────

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


def _run_agent(instruction: str, extra_args: str = "", timeout: int = 600) -> dict:
    """Run mobile_agent.py and return parsed JSON result from stdout.

    The agent emits one JSON object per line (progress + final result).
    We capture the final ``{"type": "result", ...}`` object.
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

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=timeout,
            cwd=str(SKILL_DIR),
        )
    except subprocess.TimeoutExpired:
        print(f"[TEST] TIMEOUT after {timeout}s")
        return {"status": "timeout", "steps": -1, "last_action": "", "message": "test timeout"}

    elapsed = time.time() - t0
    print(f"[TEST] Elapsed: {elapsed:.0f}s  Exit: {proc.returncode}")

    # Find the final result JSON in stdout
    result = None
    for line in proc.stdout.splitlines():
        try:
            obj = json.loads(line)
            if obj.get("type") == "result":
                result = obj
        except json.JSONDecodeError:
            pass

    if result is None:
        # Try parsing from the last line
        result = {
            "status": "error",
            "steps": -1,
            "last_action": "",
            "message": f"no result JSON found; rc={proc.returncode}",
            "debug": {"last_stderr": (proc.stderr or "")[-500:]},
        }

    return result


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
    ):
        self.keep_snapshots = keep_snapshots
        self.reset_device = reset_device
        self.adb_path = adb_path
        self.device = device
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

                # Snapshot BEFORE
                _snapshot(f"{label}_BEFORE")

                # Run the agent
                result = _run_agent(task, extra_args=extra_args, timeout=timeout)

                # Snapshot AFTER
                _snapshot(f"{label}_AFTER")

                # Log
                entry = {
                    "run": run_idx,
                    "task": task,
                    "repeat": rep,
                    "timestamp": _ts(),
                    "status": result.get("status"),
                    "steps": result.get("steps"),
                    "last_action": result.get("last_action"),
                    "message": result.get("message", ""),
                }
                self.run_log.append(entry)
                print(f"[TEST RESULT] status={result.get('status')} steps={result.get('steps')}")

                # Validate after each run
                store = _load_store()
                print(f"\n[VALIDATE] Running invariant checks...")
                validations = validate_plan_invariants(store)
                failures = [v for v in validations if not v.passed]
                if failures:
                    print(f"[VALIDATE] ❌ {len(failures)}/{len(validations)} checks FAILED:")
                    for v in failures:
                        print(f"  ❌ {v.name}: {v.detail}")
                else:
                    print(f"[VALIDATE] ✅ All {len(validations)} invariant checks passed")

                # Print plan summary
                store_for_key = _load_store()
                # Find intent key for this task
                from memory.signature import build_canonical_intent_key
                intent_key = build_canonical_intent_key(task)
                _print_plan_summary(store_for_key, intent_key)

        # ── Final report ────────────────────────────────────────────
        self._print_final_report(tasks)

    def _print_final_report(self, tasks: list[str]) -> None:
        """Print a comprehensive final report."""
        print(f"\n\n{'='*60}")
        print("FINAL REPORT")
        print(f"{'='*60}")

        # Per-task summary
        print(f"\n{'Task':<40} {'Runs':>5} {'Success':>8} {'Failed':>8} {'Other':>8}")
        print("-" * 75)
        for task in tasks:
            task_runs = [r for r in self.run_log if r["task"] == task]
            n_success = sum(1 for r in task_runs if r["status"] == "success")
            n_failed = sum(1 for r in task_runs if r["status"] in ("error", "parse_failed"))
            n_other = len(task_runs) - n_success - n_failed
            print(f"{task[:38]:<40} {len(task_runs):>5} {n_success:>8} {n_failed:>8} {n_other:>8}")

        # Plan store state
        store = _load_store()
        print(f"\n{'─'*60}")
        print("FINAL PLAN STORE STATE")
        print(f"{'─'*60}")
        _print_plan_summary(store)

        # Final validation
        print(f"\n{'─'*60}")
        print("FINAL VALIDATION (all plans)")
        print(f"{'─'*60}")
        validations = validate_plan_invariants(store)
        failures = [v for v in validations if not v.passed]
        passed = [v for v in validations if v.passed]
        print(f"  Passed: {len(passed)}")
        print(f"  Failed: {len(failures)}")
        if failures:
            print(f"\n  FAILURES:")
            for v in failures:
                print(f"    ❌ {v.name}")
                print(f"       {v.detail}")

        # Snapshot final state
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
