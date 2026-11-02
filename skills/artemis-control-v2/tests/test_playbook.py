"""Tests for the training playbook and its guardrails."""

from __future__ import annotations

import unittest

from memory.playbook import (
    TrainingTask,
    find_task,
    is_trusted_for_replay,
    load_playbook,
    validate_plan,
)


class TestPlaybook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tasks = load_playbook()
        cls.by_id = {t.id: t for t in cls.tasks}

    def test_corpus_loads_and_has_flagship_mute(self):
        self.assertTrue(self.tasks, "playbook must not be empty")
        self.assertIn("mute-ringer", self.by_id)
        mute = self.by_id["mute-ringer"]
        self.assertTrue(mute.plan)
        self.assertFalse(is_trusted_for_replay(mute), "mute recipe is not yet device-verified")

    def test_every_task_parses_and_has_verify_with_probe(self):
        for task in self.tasks:
            with self.subTest(task=task.id):
                self.assertTrue(task.plan, f"{task.id} has no plan")
                self.assertEqual(validate_plan(task, task.plan), [],
                                 f"{task.id} violates its own guardrails")

    def test_find_task_matches_aliases(self):
        self.assertEqual(find_task(self.tasks, "帮我把手机静音").id, "mute-ringer")
        self.assertEqual(find_task(self.tasks, "turn on wifi please").id, "wifi-toggle")
        self.assertIsNone(find_task(self.tasks, "打个电话给妈妈"))

    def test_mute_plan_rejects_volume_up_first_action(self):
        mute = self.by_id["mute-ringer"]
        bad = mute.plan.replace(
            "- path 1 [cost=1 kind=primary]:     system_button volume_mute（keyevent 164，直接静音）",
            "- path 1 [cost=1 kind=primary]:     按 VOLUME_UP 调出音量面板 → 点铃铛",
        )
        violations = validate_plan(mute, bad)
        self.assertTrue(any("VOLUME_UP" in v for v in violations), violations)

    def test_mute_plan_rejects_plan_without_verify(self):
        mute = self.by_id["mute-ringer"]
        no_verify = "\n".join(
            ln for ln in mute.plan.splitlines() if not ln.strip().startswith("- verify:")
        )
        violations = validate_plan(mute, no_verify)
        self.assertTrue(any("verify" in v for v in violations), violations)


if __name__ == "__main__":
    unittest.main()