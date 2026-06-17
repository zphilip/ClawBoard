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


def _merge_step_counters(new_plan: TaskPlan, old_plan: TaskPlan) -> None:
    """Merge step-level success/fail counters from *old_plan* into *new_plan*.

    Steps are matched by (action_type, action_args) so a corrected step
    that replaces a failing one inherits the counter history.
    """
    for _new_step in new_plan.steps:
        for _old_step in old_plan.steps:
            if (_new_step.action_type == _old_step.action_type
                    and _new_step.action_args == _old_step.action_args):
                _new_step.success_count += _old_step.success_count
                _new_step.fail_count += _old_step.fail_count
                break


def plan_is_healthy(plan: TaskPlan) -> bool:
    """Return True if all steps have not failed more than they succeeded.

    A step with fail_count=0 and success_count=0 (pre-scoring era) is
    considered healthy — no evidence of failure.  A step that has failed
    2+ times OR has fail_count > success_count is unhealthy.
    """
    for step in plan.steps:
        if step.fail_count >= 2:
            return False
        if step.fail_count > step.success_count:
            return False
    return True


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

        Uses step-level health to decide between old and new plan:

        1. New plan healthy, old plan unhealthy → REPLACE (corrected plan
           always wins over a known-broken plan, even if it has more steps).
        2. New plan unhealthy, old plan healthy → KEEP old, count as failure.
        3. Both healthy (or both unhealthy) → shorter plan wins.  When the
           new plan is longer or equal, the old plan is kept and its success
           counter is incremented.

        When replacing, step-level success/fail counters are merged for
        steps that match by action_type and action_args between old and new.
        """
        plans = self.load()
        replaced = False
        for i, existing in enumerate(plans):
            if existing.intent_key == plan.intent_key:
                _new_healthy = plan_is_healthy(plan)
                _old_healthy = plan_is_healthy(existing)

                if _new_healthy and not _old_healthy:
                    # New plan is healthy; old plan is known-broken.
                    # Replace unconditionally — correctness over brevity.
                    plan.success_count += existing.success_count
                    plan.fail_count += existing.fail_count
                    plan.last_verified = max(plan.last_verified, existing.last_verified)
                    plan.created_at = existing.created_at
                    _merge_step_counters(plan, existing)
                    plans[i] = plan
                elif not _new_healthy and _old_healthy:
                    # New plan has a failing step; old plan is fine.
                    # Do NOT replace — count this run as a failure.
                    existing.fail_count += 1
                elif len(plan.steps) <= len(existing.steps):
                    # Both healthy (or both unhealthy): shorter plan wins.
                    plan.success_count += existing.success_count
                    plan.fail_count += existing.fail_count
                    plan.last_verified = max(plan.last_verified, existing.last_verified)
                    plan.created_at = existing.created_at
                    _merge_step_counters(plan, existing)
                    plans[i] = plan
                else:
                    # Both similar health; new plan is longer — keep old.
                    existing.success_count += 1
                    existing.last_verified = max(existing.last_verified, time.time())
                replaced = True
                break
        if not replaced:
            plans.append(plan)
        self._rewrite_all(plans)

    def find_best(self, intent_key: str, min_success: int = 1) -> TaskPlan | None:
        """Return the highest-scored **healthy** plan for *intent_key*, or None.

        Score = success_count / (success_count + fail_count).
        Plans with fewer than *min_success* successful runs OR with any
        unhealthy step are excluded.
        """
        candidates = [
            p for p in self.load()
            if p.intent_key == intent_key
            and p.success_count >= min_success
            and plan_is_healthy(p)
        ]
        if not candidates:
            return None

        def _score(p: TaskPlan) -> float:
            total = p.success_count + p.fail_count
            return p.success_count / max(total, 1)

        candidates.sort(key=_score, reverse=True)
        return candidates[0]

    def remove(self, intent_key: str) -> bool:
        """Remove the plan with the given intent_key.

        Returns True if a plan was removed, False if none matched.
        """
        plans = self.load()
        removed = False
        new_plans = []
        for p in plans:
            if p.intent_key == intent_key:
                removed = True
            else:
                new_plans.append(p)
        if removed:
            self._rewrite_all(new_plans)
        return removed

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

    def increment_step_fail(self, intent_key: str, step_index: int) -> None:
        """Increment the fail_count of a specific step in a plan.

        Used when a plan-replay step fails and the VLM has to handle it.
        This penalises the offending step so the plan eventually becomes
        unhealthy and self-heals on the next upsert.
        """
        plans = self.load()
        for plan in plans:
            if plan.intent_key == intent_key:
                for step in plan.steps:
                    if step.step_index == step_index:
                        step.fail_count += 1
                        break
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
                "target_element_crop_b64": s.target_element_crop_b64,
                "success_count": s.success_count,
                "fail_count": s.fail_count,
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
            target_element_crop_b64=s.get("target_element_crop_b64", ""),
            success_count=int(s.get("success_count", 0)),
            fail_count=int(s.get("fail_count", 0)),
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
