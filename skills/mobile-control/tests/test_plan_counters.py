#!/usr/bin/env python3
"""
Tests for plan step/plan success/fail counters and their effects on:
  - plan_is_healthy()
  - find_best() / find_plan() — plan selection & score threshold
  - upsert_by_intent() — auto-promotion & counter merging
  - execute_and_verify() — when steps are skipped or plans abandoned
  - PlanExecutor state machine — pause / resume / end_replay / exhaustion

USAGE:
    cd ClawBoard/skills/mobile-control
    python3 tests/test_plan_counters.py
    # or
    python3 -m pytest tests/test_plan_counters.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# ── make the memory package importable ──────────────────────────────
SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))

from memory.models import PlanStep, TaskPlan
from memory.plan_store import PlanStore, plan_is_healthy


# ══════════════════════════════════════════════════════════════════════
# Test helpers
# ══════════════════════════════════════════════════════════════════════

def _make_step(
    index: int,
    action_type: str = "click",
    success: int = 0,
    fail: int = 0,
    **kwargs,
) -> PlanStep:
    # Build action_args from kwargs that are action-related, not PlanStep fields
    _step_fields = {
        "expected_pkg", "action_description", "pre_action_pkg",
        "post_action_ui_fp", "target_element_signature",
    }
    args = {"action": action_type, "coordinate": [500, 500]}
    for k, v in kwargs.items():
        if k not in _step_fields and k not in ("success", "fail"):
            args[k] = v
    return PlanStep(
        step_index=index,
        action_type=action_type,
        action_args=args,
        success_count=success,
        fail_count=fail,
        pre_action_pkg=kwargs.get("pre_action_pkg", ""),
        expected_pkg=kwargs.get("expected_pkg", ""),
        action_description=kwargs.get("action_description", ""),
        post_action_ui_fp=kwargs.get("post_action_ui_fp", ""),
        target_element_signature=kwargs.get("target_element_signature"),
    )


def _make_plan(
    intent_key: str = "abc123",
    steps: list[PlanStep] | None = None,
    success_count: int = 0,
    fail_count: int = 0,
    last_verified: float = 0.0,
) -> TaskPlan:
    return TaskPlan(
        intent_key=intent_key,
        instruction_sample="test instruction",
        steps=steps or [],
        success_count=success_count,
        fail_count=fail_count,
        last_verified=last_verified,
        created_at=time.time(),
    )


def _temp_store() -> PlanStore:
    """Return a PlanStore backed by a temp file (auto-cleaned)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
    tmp.close()
    store = PlanStore(tmp.name)
    # Register cleanup
    import atexit
    atexit.register(lambda: os.unlink(tmp.name))
    return store


# ══════════════════════════════════════════════════════════════════════
# Section 1 — plan_is_healthy()
# ══════════════════════════════════════════════════════════════════════

class TestPlanIsHealthy(unittest.TestCase):
    """Step-level fail_count determines whether a plan can be replayed."""

    def test_empty_steps_is_healthy(self):
        plan = _make_plan(steps=[])
        self.assertTrue(plan_is_healthy(plan))

    def test_all_steps_zero_zero_is_healthy(self):
        """Pre-scoring era: fail=0 success=0 → no evidence of failure."""
        plan = _make_plan(steps=[
            _make_step(0, success=0, fail=0),
            _make_step(1, success=0, fail=0),
        ])
        self.assertTrue(plan_is_healthy(plan))

    def test_fail_gt_success_is_unhealthy(self):
        """fail_count > success_count → step is unhealthy → plan unhealthy."""
        plan = _make_plan(steps=[
            _make_step(0, success=0, fail=1),  # 1 > 0
        ])
        self.assertFalse(plan_is_healthy(plan))

    def test_fail_ge_2_is_unhealthy(self):
        """fail_count >= 2 → unconditionally unhealthy."""
        plan = _make_plan(steps=[
            _make_step(0, success=5, fail=2),  # success > fail, but fail >= 2
        ])
        self.assertFalse(plan_is_healthy(plan))

    def test_fail_1_success_1_is_healthy(self):
        """fail <= success AND fail < 2 → healthy."""
        plan = _make_plan(steps=[
            _make_step(0, success=1, fail=1),
        ])
        self.assertTrue(plan_is_healthy(plan))

    def test_fail_1_success_2_is_healthy(self):
        plan = _make_plan(steps=[
            _make_step(0, success=2, fail=1),
        ])
        self.assertTrue(plan_is_healthy(plan))

    def test_mixed_steps_one_unhealthy_poisons_plan(self):
        """Any unhealthy step makes the entire plan unhealthy."""
        plan = _make_plan(steps=[
            _make_step(0, success=3, fail=0),   # healthy
            _make_step(1, success=0, fail=2),   # unhealthy (fail >= 2)
            _make_step(2, success=5, fail=0),   # healthy
        ])
        self.assertFalse(plan_is_healthy(plan))

    def test_all_steps_must_pass_both_conditions(self):
        """Each step must satisfy: fail < 2 AND fail <= success."""
        plan = _make_plan(steps=[
            _make_step(0, success=1, fail=1),   # healthy
            _make_step(1, success=1, fail=1),   # healthy
            _make_step(2, success=2, fail=0),   # healthy
        ])
        self.assertTrue(plan_is_healthy(plan))


# ══════════════════════════════════════════════════════════════════════
# Section 2 — find_best() — plan selection
# ══════════════════════════════════════════════════════════════════════

class TestFindBest(unittest.TestCase):
    """PlanStore.find_best filters by: health, min_success, score."""

    def test_no_plans_returns_none(self):
        store = _temp_store()
        self.assertIsNone(store.find_best("nonexistent"))

    def test_below_min_success_excluded(self):
        store = _temp_store()
        plan = _make_plan(
            intent_key="abc",
            steps=[_make_step(0, success=0, fail=0)],
            success_count=0,       # < min_success (default 1)
        )
        store.append(plan)
        self.assertIsNone(store.find_best("abc", min_success=1))

    def test_min_success_met_healthy_plan_returned(self):
        store = _temp_store()
        plan = _make_plan(
            intent_key="abc",
            steps=[_make_step(0, success=0, fail=0)],
            success_count=1,
        )
        store.append(plan)
        result = store.find_best("abc", min_success=1)
        self.assertIsNotNone(result)
        self.assertEqual(result.intent_key, "abc")

    def test_unhealthy_plan_excluded(self):
        store = _temp_store()
        plan = _make_plan(
            intent_key="abc",
            steps=[_make_step(0, success=0, fail=2)],  # unhealthy
            success_count=5,
        )
        store.append(plan)
        self.assertIsNone(store.find_best("abc", min_success=1))

    def test_highest_score_wins(self):
        """score = success / (success + fail). Higher is better."""
        store = _temp_store()
        good = _make_plan(
            intent_key="abc",
            steps=[_make_step(0)],
            success_count=10, fail_count=1,     # score = 10/11 ≈ 0.909
        )
        bad = _make_plan(
            intent_key="abc",
            steps=[_make_step(0)],
            success_count=2, fail_count=2,      # score = 2/4 = 0.50
        )
        store.append(bad)
        store.append(good)
        result = store.find_best("abc")
        self.assertIsNotNone(result)
        self.assertEqual(result.success_count, 10)  # higher score wins

    def test_different_intent_keys_dont_interfere(self):
        store = _temp_store()
        store.append(_make_plan(intent_key="aaa", steps=[_make_step(0)], success_count=1))
        store.append(_make_plan(intent_key="bbb", steps=[_make_step(0)], success_count=1))
        result = store.find_best("aaa")
        self.assertIsNotNone(result)
        self.assertEqual(result.intent_key, "aaa")


# ══════════════════════════════════════════════════════════════════════
# Section 3 — upsert_by_intent() — auto-promotion & counter merging
# ══════════════════════════════════════════════════════════════════════

class TestUpsertByIntent(unittest.TestCase):
    """The core auto-promotion logic determines whether a newly recorded
    plan replaces an existing one."""

    def test_new_healthy_old_unhealthy_replaces(self):
        """Correct plan always wins over known-broken plan."""
        store = _temp_store()
        old = _make_plan(
            intent_key="abc",
            steps=[_make_step(0, success=0, fail=2)],  # unhealthy
            success_count=3, fail_count=4,
        )
        store.append(old)

        new = _make_plan(
            intent_key="abc",
            steps=[_make_step(0, success=0, fail=0)],  # healthy
            success_count=1, fail_count=0,
        )
        store.upsert_by_intent(new)

        plans = store.load()
        self.assertEqual(len(plans), 1)
        # New plan replaced old — counters MERGED
        self.assertEqual(plans[0].success_count, 4)  # 3 + 1)
        self.assertEqual(plans[0].fail_count, 4)  # 4 + 0)
        self.assertTrue(plan_is_healthy(plans[0]))

    def test_new_unhealthy_old_healthy_keeps_old(self):
        """Do NOT replace a working plan with a broken one."""
        store = _temp_store()
        old = _make_plan(
            intent_key="abc",
            steps=[_make_step(0)],          # healthy
            success_count=5, fail_count=0,
        )
        store.append(old)

        new = _make_plan(
            intent_key="abc",
            steps=[_make_step(0, success=0, fail=2)],  # unhealthy
            success_count=1, fail_count=0,
        )
        store.upsert_by_intent(new)

        plans = store.load()
        self.assertEqual(len(plans), 1)
        # Old plan kept, fail_count incremented
        self.assertEqual(plans[0].success_count, 5)
        self.assertEqual(plans[0].fail_count, 1)  # +1 for this failed attempt)

    def test_both_healthy_new_shorter_replaces(self):
        """Shorter plan wins when both are healthy."""
        store = _temp_store()
        old = _make_plan(
            intent_key="abc",
            steps=[_make_step(0), _make_step(1), _make_step(2)],  # 3 steps
            success_count=3, fail_count=0,
        )
        store.append(old)

        new = _make_plan(
            intent_key="abc",
            steps=[_make_step(0), _make_step(1)],  # 2 steps — shorter
            success_count=1, fail_count=0,
        )
        store.upsert_by_intent(new)

        plans = store.load()
        self.assertEqual(len(plans), 1)
        self.assertEqual(len(plans[0].steps), 2)     # shorter plan won
        self.assertEqual(plans[0].success_count, 4)  # 3 + 1)

    def test_both_healthy_new_longer_keeps_old(self):
        """Keep the shorter healthy plan; count new one as a success."""
        store = _temp_store()
        old = _make_plan(
            intent_key="abc",
            steps=[_make_step(0)],           # 1 step — shorter
            success_count=3, fail_count=0,
        )
        store.append(old)

        new = _make_plan(
            intent_key="abc",
            steps=[_make_step(0), _make_step(1)],  # 2 steps — longer
            success_count=1, fail_count=0,
        )
        store.upsert_by_intent(new)

        plans = store.load()
        self.assertEqual(len(plans), 1)
        self.assertEqual(len(plans[0].steps), 1)     # old (shorter) kept
        self.assertEqual(plans[0].success_count, 4)  # 3 + 1 (counted as success))

    def test_both_unhealthy_shorter_wins(self):
        """When both are unhealthy, shorter still wins."""
        store = _temp_store()
        old = _make_plan(
            intent_key="abc",
            steps=[_make_step(0, fail=2), _make_step(1, fail=2)],  # 2 steps
            success_count=2, fail_count=3,
        )
        store.append(old)

        new = _make_plan(
            intent_key="abc",
            steps=[_make_step(0, fail=2)],  # 1 step — shorter
            success_count=1, fail_count=0,
        )
        store.upsert_by_intent(new)

        plans = store.load()
        self.assertEqual(len(plans), 1)
        self.assertEqual(len(plans[0].steps), 1)     # shorter won

    def test_no_existing_appends(self):
        store = _temp_store()
        plan = _make_plan(intent_key="new_one", steps=[_make_step(0)])
        store.upsert_by_intent(plan)
        self.assertEqual(len(store.load()), 1)

    def test_success_count_2_triggers_auto_promotion(self):
        """After 2 successful recordings, find_best returns a plan
        (the runner uses this as the auto-promotion signal)."""
        store = _temp_store()
        plan = _make_plan(
            intent_key="auto",
            steps=[_make_step(0)],
            success_count=2, fail_count=0,
        )
        store.append(plan)
        result = store.find_best("auto", min_success=1)
        self.assertIsNotNone(result)
        self.assertEqual(result.success_count, 2)


# ══════════════════════════════════════════════════════════════════════
# Section 4 — find_plan() — PlanExecutor scoring threshold
# ══════════════════════════════════════════════════════════════════════

class TestPlanExecutorFindPlan(unittest.TestCase):
    """PlanExecutor.find_plan adds a score >= 0.6 threshold on top of
    PlanStore.find_best."""

    def _make_executor(self, store):
        from memory.plan_executor import PlanExecutor
        # PlanExecutor needs an adb_tools mock and optional helpers
        mock_adb = MagicMock()
        mock_adb.get_foreground_package.return_value = "com.test"
        mock_adb.get_ui_dump.return_value = ""
        return PlanExecutor(store, mock_adb)

    def test_score_below_60_percent_returns_none(self):
        """Score = success/(success+fail). 2/5 = 0.40 < 0.6 → ignored."""
        store = _temp_store()
        plan = _make_plan(
            intent_key="unreliable",
            steps=[_make_step(0)],
            success_count=2, fail_count=3,   # score = 2/5 = 0.40
        )
        store.append(plan)
        executor = self._make_executor(store)
        result = executor.find_plan("unreliable")
        self.assertIsNone(result)

    def test_score_60_percent_exactly_is_returned(self):
        store = _temp_store()
        plan = _make_plan(
            intent_key="borderline",
            steps=[_make_step(0)],
            success_count=3, fail_count=2,   # score = 3/5 = 0.60
        )
        store.append(plan)
        executor = self._make_executor(store)
        result = executor.find_plan("borderline")
        self.assertIsNotNone(result)

    def test_score_above_60_percent_is_returned(self):
        store = _temp_store()
        plan = _make_plan(
            intent_key="reliable",
            steps=[_make_step(0)],
            success_count=8, fail_count=1,   # score = 8/9 ≈ 0.889
        )
        store.append(plan)
        executor = self._make_executor(store)
        result = executor.find_plan("reliable")
        self.assertIsNotNone(result)

    def test_healthy_but_low_score_ignored(self):
        """Even perfectly healthy plans are ignored if their score < 0.6."""
        store = _temp_store()
        plan = _make_plan(
            intent_key="barely_there",
            steps=[_make_step(0, success=0, fail=0)],   # healthy
            success_count=1, fail_count=1,               # score = 0.50
        )
        store.append(plan)
        executor = self._make_executor(store)
        self.assertIsNone(executor.find_plan("barely_there"))

    def test_empty_store_returns_none(self):
        executor = self._make_executor(_temp_store())
        self.assertIsNone(executor.find_plan("nonexistent"))


# ══════════════════════════════════════════════════════════════════════
# Section 5 — PlanExecutor state machine
# ══════════════════════════════════════════════════════════════════════

class TestPlanExecutorStateMachine(unittest.TestCase):
    """start → pause → resume → end lifecycle, and cursor exhaustion."""

    def _make_executor(self, store):
        from memory.plan_executor import PlanExecutor
        mock_adb = MagicMock()
        mock_adb.get_foreground_package.return_value = "com.test"
        mock_adb.get_ui_dump.return_value = ""
        return PlanExecutor(store, mock_adb)

    def test_start_replay_sets_active(self):
        executor = self._make_executor(_temp_store())
        plan = _make_plan(steps=[_make_step(0), _make_step(1)])
        executor.start_replay(plan)
        self.assertTrue(executor.is_replaying())
        self.assertEqual(executor.replay_cursor, 0)

    def test_pause_replay_flags_not_replaying(self):
        executor = self._make_executor(_temp_store())
        plan = _make_plan(steps=[_make_step(0)])
        executor.start_replay(plan)
        executor.pause_replay()
        self.assertFalse(executor.is_replaying())

    def test_next_step_returns_none_when_exhausted(self):
        executor = self._make_executor(_temp_store())
        plan = _make_plan(steps=[_make_step(0)])
        executor.start_replay(plan)
        self.assertIsNotNone(executor.next_step())   # step 0
        executor._replay_cursor = 1               # past end
        self.assertIsNone(executor.next_step())

    def test_end_replay_stops_replaying(self):
        executor = self._make_executor(_temp_store())
        plan = _make_plan(steps=[_make_step(0)])
        executor.start_replay(plan)
        executor.end_replay()
        self.assertFalse(executor.is_replaying())

    def test_resume_replay_advances_past_failed_step(self):
        executor = self._make_executor(_temp_store())
        plan = _make_plan(steps=[_make_step(0), _make_step(1), _make_step(2)])
        executor.start_replay(plan)
        # Simulate: step 0 failed, VLM handled it
        executor.pause_replay()
        executor.resume_replay()
        # Cursor should have advanced past step 0
        self.assertEqual(executor.replay_cursor, 1)

    def test_resume_replay_exhausts_when_past_end(self):
        executor = self._make_executor(_temp_store())
        plan = _make_plan(steps=[_make_step(0)])
        executor.start_replay(plan)
        executor.pause_replay()
        executor.resume_replay()
        # Cursor was 0, advances to 1, which >= len(steps)=1
        self.assertFalse(executor.is_replaying())   # end_replay called

    def test_exhausted_plan_triggers_termination(self):
        """When next_step() returns None after a successful step,
        the runner should set termination_reason = 'plan_replay_complete'."""
        executor = self._make_executor(_temp_store())
        plan = _make_plan(steps=[_make_step(0)])
        executor.start_replay(plan)
        step = executor.next_step()
        self.assertIsNotNone(step)
        # After executing step 0, cursor advances to 1
        executor._replay_cursor = 1
        self.assertIsNone(executor.next_step())  # exhausted
        # The runner then calls end_replay() and sets termination_reason
        executor.end_replay()
        self.assertFalse(executor.is_replaying())


# ══════════════════════════════════════════════════════════════════════
# Section 6 — note_step_failure & note_plan_failure integration
# ══════════════════════════════════════════════════════════════════════

class TestNoteMethods(unittest.TestCase):
    """After a plan replay run concludes, the runner calls the note_*
    methods to persist counter updates."""

    def _make_executor(self, store):
        from memory.plan_executor import PlanExecutor
        mock_adb = MagicMock()
        mock_adb.get_foreground_package.return_value = "com.test"
        return PlanExecutor(store, mock_adb)

    def test_note_plan_success_increments_success(self):
        store = _temp_store()
        plan = _make_plan(
            intent_key="abc", steps=[_make_step(0)],
            success_count=3, fail_count=0,
        )
        store.append(plan)

        executor = self._make_executor(store)
        executor.note_plan_success("abc")

        updated = store.find_best("abc")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.success_count, 4)
        self.assertGreater(updated.last_verified, 0)

    def test_note_plan_failure_increments_fail(self):
        store = _temp_store()
        plan = _make_plan(
            intent_key="abc", steps=[_make_step(0)],
            success_count=3, fail_count=1,
        )
        store.append(plan)

        executor = self._make_executor(store)
        executor.note_plan_failure("abc")

        updated = store.find_best("abc")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.fail_count, 2)

    def test_note_step_failure_silent_when_no_failed_step(self):
        """When _failed_step_index is None, note_step_failure is a no-op."""
        store = _temp_store()
        plan = _make_plan(
            intent_key="abc",
            steps=[_make_step(0, success=0, fail=0)],
            success_count=1,
        )
        store.append(plan)

        executor = self._make_executor(store)
        executor._failed_step_index = None
        executor.note_step_failure("abc")

        updated = store.find_best("abc")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.steps[0].fail_count, 0)  # unchanged)

    def test_note_step_failure_increments_specific_step(self):
        store = _temp_store()
        plan = _make_plan(
            intent_key="abc",
            steps=[
                _make_step(0, success=0, fail=0),
                _make_step(1, success=0, fail=0),
            ],
            success_count=1,
        )
        store.append(plan)

        executor = self._make_executor(store)
        executor._failed_step_index = 1       # step 1 failed
        executor.note_step_failure("abc")

        # Use load() not find_best() — plan is now unhealthy (fail>success)
        updated_list = store.load()
        self.assertEqual(len(updated_list), 1)
        updated = updated_list[0]
        self.assertEqual(updated.steps[0].fail_count, 0)  # unchanged
        self.assertEqual(updated.steps[1].fail_count, 1)  # incremented

    def test_step_fail_count_reaches_2_makes_plan_unhealthy(self):
        """After 2 step failures, plan_is_healthy returns False."""
        store = _temp_store()
        plan = _make_plan(
            intent_key="abc",
            steps=[_make_step(0, success=0, fail=1)],  # already failed once
            success_count=1,
        )
        store.append(plan)

        executor = self._make_executor(store)
        executor._failed_step_index = 0
        executor.note_step_failure("abc")  # fail_count becomes 2

        updated = store.find_best("abc")
        # Now the plan should be excluded by find_best (unhealthy)
        self.assertIsNone(updated)


# ══════════════════════════════════════════════════════════════════════
# Section 7 — execute_and_verify step-skip scenarios (no real ADB)
# ══════════════════════════════════════════════════════════════════════

class TestExecuteAndVerifySkipScenarios(unittest.TestCase):
    """These test the DECISION LOGIC of when a step is skipped, using mocks
    to simulate the device state.  No real ADB calls are made."""

    def _make_executor(self, store, **adb_overrides):
        from memory.plan_executor import PlanExecutor
        mock_adb = MagicMock()
        mock_adb.get_foreground_package.return_value = adb_overrides.get(
            "fg_pkg", "com.target.app"
        )
        mock_adb.get_ui_dump.return_value = adb_overrides.get("ui_xml", "")
        mock_adb.click.return_value = True
        mock_adb.home.return_value = True
        mock_adb.back.return_value = True
        mock_adb.type_with_verification.return_value = True
        mock_adb.get_screenshot.return_value = True
        return PlanExecutor(store, mock_adb)

    def test_pre_check_wrong_fg_pkg_skips_step(self):
        """Layer 0: plan expects com.foo but we're on com.bar → skip."""
        executor = self._make_executor(
            _temp_store(), fg_pkg="com.bar"
        )
        step = _make_step(0, action_type="click", pre_action_pkg="com.foo")
        executor.start_replay(_make_plan(steps=[step]))
        result = executor.execute_and_verify(step)
        self.assertFalse(result)
        self.assertEqual(executor._failed_step_index, 0)
        self.assertEqual(executor._consecutive_failures, 1)

    def test_pre_check_not_applied_to_open_action(self):
        """open IS the corrective action — pre-check skipped."""
        executor = self._make_executor(
            _temp_store(), fg_pkg="com.bar"
        )
        step = _make_step(0, action_type="open", text="TargetApp",
                          pre_action_pkg="com.foo")
        executor.start_replay(_make_plan(steps=[step]))
        # open has no _open_handler → _execute_action returns False
        # but pre-check should not be the reason
        result = executor.execute_and_verify(step)
        # Returns False because _open_handler is None, not because of pre-check
        self.assertIsNotNone(executor.last_verify_detail)

    def test_two_consecutive_failures_triggers_end_replay(self):
        executor = self._make_executor(_temp_store(), fg_pkg="com.wrong")
        step = _make_step(0, action_type="click", pre_action_pkg="com.right")
        executor.start_replay(_make_plan(steps=[step, _make_step(1)]))
        # First failure
        executor.execute_and_verify(step)
        self.assertTrue(executor.is_replaying())
        self.assertEqual(executor._consecutive_failures, 1)
        # Second failure → end_replay triggered
        executor.execute_and_verify(step)
        self.assertFalse(executor.is_replaying())  # ended
        self.assertEqual(executor._consecutive_failures, 2)

    def test_success_resets_consecutive_failures(self):
        executor = self._make_executor(_temp_store(), fg_pkg="com.target.app")
        # Step with no expected_pkg → pkg check passes
        step = _make_step(0, action_type="system_button", button="Home")
        executor.start_replay(_make_plan(steps=[step, _make_step(1)]))
        executor._consecutive_failures = 1  # simulate prior failure
        result = executor.execute_and_verify(step)
        self.assertTrue(result)
        self.assertEqual(executor._consecutive_failures, 0)  # reset)

    def test_type_extra_settle_time_applied(self):
        """type actions get POST_TYPE_SETTLE_SECONDS extra wait."""
        executor = self._make_executor(_temp_store(), fg_pkg="com.target.app")
        step = _make_step(0, action_type="type", text="hello")
        executor.start_replay(_make_plan(steps=[step]))
        # Should succeed with type_with_verification mock returning True
        result = executor.execute_and_verify(step)
        self.assertTrue(result)


# ══════════════════════════════════════════════════════════════════════
# Section 8 — Self-healing: unhealthy → healthy on upsert
# ══════════════════════════════════════════════════════════════════════

class TestSelfHealing(unittest.TestCase):
    """A plan becomes unhealthy after step failures, then self-heals
    when a fresh VLM-driven run records a healthy plan."""

    def test_healthy_replacement_restores_usability(self):
        store = _temp_store()
        # Start with a healthy plan that becomes unhealthy
        old = _make_plan(
            intent_key="abc",
            steps=[_make_step(0, success=0, fail=2)],  # unhealthy
            success_count=3, fail_count=2,
        )
        store.append(old)
        self.assertIsNone(store.find_best("abc"))  # unhealthy → excluded

        # VLM-driven run succeeds → records fresh healthy plan
        new = _make_plan(
            intent_key="abc",
            steps=[_make_step(0, success=0, fail=0)],  # healthy — clean slate
            success_count=1, fail_count=0,
        )
        store.upsert_by_intent(new)

        # Now the plan is findable again — self-healing complete
        result = store.find_best("abc")
        self.assertIsNotNone(result)
        self.assertTrue(plan_is_healthy(result))
        self.assertEqual(result.success_count, 4)  # 3 + 1 (merged))


# ══════════════════════════════════════════════════════════════════════
# Section 9 — Serialisation round-trip
# ══════════════════════════════════════════════════════════════════════

class TestSerialisation(unittest.TestCase):
    """TaskPlan and PlanStep must survive JSONL serialise → deserialise
    with all counter fields intact."""

    def test_round_trip_preserves_counters(self):
        store = _temp_store()
        step = PlanStep(
            step_index=0,
            action_type="click",
            action_args={"action": "click", "coordinate": [500, 500]},
            expected_pkg="com.target",
            action_description="tap search bar",
            pre_action_pkg="com.launcher",
            post_action_ui_fp="abcdef",
            target_element_signature={"resource_id": "search_bar"},
            success_count=3,
            fail_count=1,
        )
        plan = TaskPlan(
            intent_key="test_key",
            instruction_sample="open app and search",
            steps=[step],
            success_count=7,
            fail_count=2,
            last_verified=1234567890.0,
            created_at=1234567000.0,
            source_run_id="run_001",
        )
        store.append(plan)

        loaded = store.load()
        self.assertEqual(len(loaded), 1)
        p = loaded[0]
        self.assertEqual(p.intent_key, "test_key")
        self.assertEqual(p.success_count, 7)
        self.assertEqual(p.fail_count, 2)
        self.assertEqual(p.last_verified, 1234567890.0)
        self.assertEqual(len(p.steps), 1)
        s = p.steps[0]
        self.assertEqual(s.success_count, 3)
        self.assertEqual(s.fail_count, 1)
        self.assertEqual(s.action_description, "tap search bar")
        self.assertEqual(s.target_element_signature, {"resource_id": "search_bar"})


# ══════════════════════════════════════════════════════════════════════
# Section 10 — increment_success / increment_fail (plan store)
# ══════════════════════════════════════════════════════════════════════

class TestIncrementMethods(unittest.TestCase):
    """PlanStore.increment_success and increment_fail update counters
    in-place on the persisted JSONL file."""

    def test_increment_success_updates_both_counters(self):
        store = _temp_store()
        plan = _make_plan(
            intent_key="abc", steps=[_make_step(0)],
            success_count=1, fail_count=0, last_verified=0.0,
        )
        store.append(plan)
        store.increment_success("abc")
        updated = store.find_best("abc")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.success_count, 2)
        self.assertGreater(updated.last_verified, 0.0)

    def test_increment_fail_only_updates_fail(self):
        store = _temp_store()
        plan = _make_plan(
            intent_key="abc", steps=[_make_step(0)],
            success_count=3, fail_count=0, last_verified=100.0,
        )
        store.append(plan)
        store.increment_fail("abc")
        updated = store.find_best("abc")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.fail_count, 1)
        self.assertEqual(updated.last_verified, 100.0)  # NOT updated)

    def test_increment_step_fail_increments_specific_step(self):
        store = _temp_store()
        plan = _make_plan(
            intent_key="abc",
            steps=[_make_step(0, fail=0), _make_step(1, fail=0)],
            success_count=1,
        )
        store.append(plan)
        store.increment_step_fail("abc", step_index=1)
        # Use load() — plan becomes unhealthy after step fail
        updated_list = store.load()
        self.assertEqual(len(updated_list), 1)
        updated = updated_list[0]
        self.assertEqual(updated.steps[0].fail_count, 0)
        self.assertEqual(updated.steps[1].fail_count, 1)

    def test_increment_step_fail_nonexistent_step_noop(self):
        store = _temp_store()
        plan = _make_plan(
            intent_key="abc", steps=[_make_step(0, fail=0)],
            success_count=1,
        )
        store.append(plan)
        store.increment_step_fail("abc", step_index=99)  # doesn't exist
        updated = store.find_best("abc")
        self.assertIsNotNone(updated)
        self.assertEqual(updated.steps[0].fail_count, 0)  # unchanged)


# ══════════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main()
