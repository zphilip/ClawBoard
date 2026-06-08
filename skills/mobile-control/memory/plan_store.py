"""Persistent JSONL store for TaskPlan records.

Each line in the file is one serialised TaskPlan (with nested PlanStep list).
The store supports:
  - load all plans
  - append a new plan
  - upsert: replace an existing plan that matches on intent_key + source_run_id
  - find_best: return the highest-scored plan for a given intent_key
  - increment_success / increment_fail: update counters in-place
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .models import PlanStep, TaskPlan


class PlanStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()
        # In-memory cache — avoids O(n) re-read + re-parse on every lookup.
        self._cache: list[TaskPlan] | None = None
        self._cache_mtime: float = 0.0

    def _invalidate_cache(self) -> None:
        self._cache = None
        self._cache_mtime = 0.0

    # ------------------------------------------------------------------
    # I/O helpers
    # ------------------------------------------------------------------

    def load(self) -> list[TaskPlan]:
        # Return cached plans when the file hasn't changed since last read.
        try:
            _mtime = self.path.stat().st_mtime
        except OSError:
            _mtime = 0.0
        if self._cache is not None and _mtime == self._cache_mtime:
            return self._cache

        plans: list[TaskPlan] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                plans.append(_deserialise_plan(obj))
            except Exception:
                continue

        self._cache = plans
        self._cache_mtime = _mtime
        return plans

    def _rewrite_all(self, plans: list[TaskPlan]) -> None:
        with self.path.open("w", encoding="utf-8") as f:
            for plan in plans:
                f.write(json.dumps(_serialise_plan(plan), ensure_ascii=False) + "\n")
        self._invalidate_cache()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def append(self, plan: TaskPlan) -> None:
        plan.created_at = plan.created_at or time.time()
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(_serialise_plan(plan), ensure_ascii=False) + "\n")
        self._invalidate_cache()

    def upsert_by_intent(self, plan: TaskPlan) -> None:
        """Replace the first plan with the same intent_key, merging counters.

        When a new LLM-driven recording overwrites an existing plan, the
        accumulated replay success/fail history is preserved so the
        auto-promotion threshold (2+ successes) survives updates.
        """
        plans = self.load()
        replaced = False
        for i, existing in enumerate(plans):
            if existing.intent_key == plan.intent_key:
                # Merge accumulated replay track record into the new plan
                plan.success_count += existing.success_count
                plan.fail_count += existing.fail_count
                plan.last_verified = max(plan.last_verified, existing.last_verified)
                plan.created_at = existing.created_at  # preserve original creation
                plans[i] = plan
                replaced = True
                break
        if not replaced:
            plans.append(plan)
        self._rewrite_all(plans)

    def find_best(self, intent_key: str, min_success: int = 1) -> TaskPlan | None:
        """Return the highest-scored plan for *intent_key*, or None.

        Score = success_count / (success_count + fail_count).
        Plans with fewer than *min_success* successful runs are excluded.
        """
        candidates = [
            p for p in self.load()
            if p.intent_key == intent_key and p.success_count >= min_success
        ]
        if not candidates:
            return None

        def _score(p: TaskPlan) -> float:
            total = p.success_count + p.fail_count
            return p.success_count / max(total, 1)

        candidates.sort(key=_score, reverse=True)
        return candidates[0]

    def increment_success(self, intent_key: str) -> None:
        plans = self.load()
        for plan in plans:
            if plan.intent_key == intent_key:
                plan.success_count += 1
                plan.last_verified = time.time()
                break
        self._rewrite_all(plans)

    def increment_fail(self, intent_key: str) -> None:
        plans = self.load()
        for plan in plans:
            if plan.intent_key == intent_key:
                plan.fail_count += 1
                break
        self._rewrite_all(plans)

    def all_plans(self) -> list[TaskPlan]:
        """Return all stored plans (for reporting / debugging)."""
        return self.load()


# ------------------------------------------------------------------
# Serialisation helpers
# ------------------------------------------------------------------

def _serialise_plan(plan: TaskPlan) -> dict[str, Any]:
    return {
        "intent_key": plan.intent_key,
        "instruction_sample": plan.instruction_sample,
        "steps": [
            {
                "step_index": s.step_index,
                "action_type": s.action_type,
                "action_args": s.action_args,
                "expected_pkg": s.expected_pkg,
                "action_description": s.action_description,
                "pre_action_pkg": s.pre_action_pkg,
                "post_action_ui_fp": s.post_action_ui_fp,
                "target_element_signature": s.target_element_signature,
            }
            for s in plan.steps
        ],
        "success_count": plan.success_count,
        "fail_count": plan.fail_count,
        "last_verified": plan.last_verified,
        "created_at": plan.created_at,
        "source_run_id": plan.source_run_id,
        "device_bucket": plan.device_bucket,
    }


def _deserialise_plan(obj: dict[str, Any]) -> TaskPlan:
    steps = [
        PlanStep(
            step_index=int(s.get("step_index", i)),
            action_type=s.get("action_type", ""),
            action_args=s.get("action_args", {}),
            expected_pkg=s.get("expected_pkg", ""),
            action_description=s.get("action_description", ""),
            pre_action_pkg=s.get("pre_action_pkg", ""),
            post_action_ui_fp=s.get("post_action_ui_fp", ""),
            target_element_signature=s.get("target_element_signature"),
        )
        for i, s in enumerate(obj.get("steps", []))
    ]
    return TaskPlan(
        intent_key=obj.get("intent_key", ""),
        instruction_sample=obj.get("instruction_sample", ""),
        steps=steps,
        success_count=int(obj.get("success_count", 0)),
        fail_count=int(obj.get("fail_count", 0)),
        last_verified=float(obj.get("last_verified", 0.0)),
        created_at=float(obj.get("created_at", 0.0)),
        source_run_id=obj.get("source_run_id", ""),
        device_bucket=obj.get("device_bucket", "default"),
    )
