"""Cross-run task plan storage for artemis-control.

A lean, action-format-native equivalent of mobile-control's
``plan_store`` + ``plan_executor``, tailored to artemis action types
(``tap`` / ``swipe`` / ``type`` / ``open`` / ``system_button`` /
``long_press`` / ``finish``) and artemis's pixel coordinates + element
``target`` indices (no 0-1000 normalization, no crop template matching).

Each recorded step keeps:
  - ``action_type`` / ``action_args``   — what to execute (pixel coords,
    text, button, target index …)
  - ``target_rid``                      — resource-id of the tapped element,
    for drift-robust re-location on replay
  - ``post_pkg``                        — foreground package AFTER the step,
    used to verify replay
  - ``description``                     — human-readable narration

Replay is driven from ``artemis_agent.py`` (which owns ``_execute_action_adb``
and the ``ElementIndexer``); this module only stores/retrieves plans and
their health counters.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ArtemisPlanStep:
    action_type: str
    action_args: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    target_rid: str = ""
    post_pkg: str = ""
    # Tier 1.5 compound-key fields — recorded so replay can relocate an
    # element that had no resource-id at record time.
    class_name: str = ""
    text: str = ""
    content_desc: str = ""
    success_count: int = 0
    fail_count: int = 0


@dataclass
class ArtemisPlan:
    intent_key: str
    instruction_sample: str = ""
    steps: list[ArtemisPlanStep] = field(default_factory=list)
    success_count: int = 0
    fail_count: int = 0
    last_verified: float = 0.0
    created_at: float = field(default_factory=time.time)


def _step_from_dict(d: dict[str, Any]) -> ArtemisPlanStep:
    return ArtemisPlanStep(
        action_type=d.get("action_type", ""),
        action_args=d.get("action_args", {}) or {},
        description=d.get("description", ""),
        target_rid=d.get("target_rid", ""),
        post_pkg=d.get("post_pkg", ""),
        class_name=d.get("class_name", ""),
        text=d.get("text", ""),
        content_desc=d.get("content_desc", ""),
        success_count=int(d.get("success_count", 0)),
        fail_count=int(d.get("fail_count", 0)),
    )


def _plan_from_dict(d: dict[str, Any]) -> ArtemisPlan:
    return ArtemisPlan(
        intent_key=d.get("intent_key", ""),
        instruction_sample=d.get("instruction_sample", ""),
        steps=[_step_from_dict(s) for s in (d.get("steps") or [])],
        success_count=int(d.get("success_count", 0)),
        fail_count=int(d.get("fail_count", 0)),
        last_verified=float(d.get("last_verified", 0.0)),
        created_at=float(d.get("created_at", 0.0)),
    )


def plan_is_healthy(plan: ArtemisPlan) -> bool:
    """A plan is unhealthy when a step has failed 2+ times and more often
    than it succeeded (drift/broken route)."""
    for step in plan.steps:
        if step.fail_count >= 2 and step.fail_count > step.success_count:
            return False
    return True


def _plan_score(plan: ArtemisPlan) -> float:
    attempts = max(plan.success_count + plan.fail_count, 1)
    return plan.success_count / attempts


class ArtemisPlanStore:
    """Persistent JSONL store of ArtemisPlan records (one JSON per line)."""

    MAX_PLANS_PER_INTENT = 3

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()
        self._cache: list[ArtemisPlan] | None = None
        self._cache_mtime: float = 0.0

    def _invalidate(self) -> None:
        self._cache = None
        self._cache_mtime = 0.0

    def load(self) -> list[ArtemisPlan]:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = 0.0
        if self._cache is not None and mtime == self._cache_mtime:
            return self._cache
        plans: list[ArtemisPlan] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                plans.append(_plan_from_dict(json.loads(line)))
            except Exception:
                continue
        self._cache = plans
        self._cache_mtime = mtime
        return plans

    def _rewrite(self, plans: list[ArtemisPlan]) -> None:
        with self.path.open("w", encoding="utf-8") as f:
            for p in plans:
                f.write(json.dumps(asdict(p), ensure_ascii=False) + "\n")
        self._invalidate()

    def find_best(self, intent_key: str) -> ArtemisPlan | None:
        """Return the highest-scoring healthy plan for *intent_key*.

        Only plans with at least one successful completion are eligible for
        replay — an empty/unverified plan is never replayed.
        """
        candidates = [
            p for p in self.load()
            if p.intent_key == intent_key
            and p.success_count >= 1
            and plan_is_healthy(p)
        ]
        if not candidates:
            return None
        return max(candidates, key=_plan_score)

    def upsert(self, plan: ArtemisPlan) -> None:
        """Replace the stored plan for the same intent_key (single route).

        Merges step success/fail counters so a corrected step inherits its
        history.  Caps plans per intent to MAX_PLANS_PER_INTENT.
        """
        plans = self.load()
        others = [p for p in plans if p.intent_key != plan.intent_key]
        existing = [p for p in plans if p.intent_key == plan.intent_key]
        for old in existing:
            if old.instruction_sample == plan.instruction_sample:
                self._merge_counters(plan, old)
                break
        same_intent = [p for p in existing if p is not plan][: self.MAX_PLANS_PER_INTENT - 1]
        result = others + [plan] + same_intent
        self._rewrite(result)

    def increment_plan_fail(self, intent_key: str) -> None:
        """Mark the plan for *intent_key* as failed (evicts unhealthy plans
        over time via ``plan_is_healthy``)."""
        plans = self.load()
        changed = False
        for p in plans:
            if p.intent_key == intent_key:
                p.fail_count += 1
                for s in p.steps:
                    s.fail_count += 1
                changed = True
        if changed:
            self._rewrite(plans)

    @staticmethod
    def _merge_counters(new: ArtemisPlan, old: ArtemisPlan) -> None:
        for ns in new.steps:
            for os in old.steps:
                if ns.action_type == os.action_type and ns.action_args == os.action_args:
                    ns.success_count += os.success_count
                    ns.fail_count += os.fail_count
                    break