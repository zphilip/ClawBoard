"""Unit tests for the artemis-control-v2 Phase 0 memory runtime.

Run with either:
    python3 -m unittest tests.test_memory_runtime -v
    python3 -m pytest tests/test_memory_runtime.py -q

These exercise the pure-Python memory layer only — no ADB device or VLM.
"""
import tempfile
import unittest

from memory import (
    NON_CACHEABLE_ACTIONS,
    ArtemisPlanStore,
    build_ui_components,
    plan_is_healthy,
)
from runner.memory_runtime import (
    MemoryRuntime,
    find_element_by_rid,
    ui_summary_from_elements,
)


class _El:
    def __init__(self, rid, text, center, index, clickable=True):
        self.resource_id = rid
        self.text = text
        self.content_desc = ""
        self.class_name = "android.widget.Button"
        self.clickable = clickable
        self.long_clickable = False
        self.center = center
        self.class_short = "Button"
        self.index = index


class _Idx:
    def __init__(self, pkg="com.x"):
        self.pkg = pkg
        self.calls = 0

    def get_foreground_app(self):
        return self.pkg

    def build_index(self):
        self.calls += 1
        return [_El("com.x:id/btn", "确定", (100, 200), 0)]


class TestUiSummary(unittest.TestCase):
    def test_summary_parses_back_to_components(self):
        els = [
            _El("com.tencent.mm:id/login_btn", "登录", (540, 1200), 3),
            _El("", "", (10, 10), 1, clickable=False),
        ]
        summary = ui_summary_from_elements(els)
        self.assertIn('id=login_btn', summary)
        self.assertIn('[Button]', summary)
        comp = build_ui_components("com.tencent.mm", summary)
        self.assertIn("login_btn", comp.resource_ids)
        self.assertIn("登录", comp.labels)

    def test_find_element_by_short_rid(self):
        els = [_El("com.x:id/btn", "确定", (100, 200), 0)]
        self.assertEqual(find_element_by_rid(els, "btn").center, (100, 200))
        self.assertEqual(find_element_by_rid(els, "com.x:id/btn").index, 0)
        self.assertIsNone(find_element_by_rid(els, "missing"))


class TestPlanStore(unittest.TestCase):
    def test_round_trip_and_health(self):
        from memory import ArtemisPlan, ArtemisPlanStep

        d = tempfile.mkdtemp()
        store = ArtemisPlanStore(f"{d}/plans.jsonl")
        store.upsert(ArtemisPlan(
            intent_key="k",
            instruction_sample="打开微信",
            steps=[ArtemisPlanStep(
                action_type="tap",
                action_args={"coordinate": [540, 1200]},
                target_rid="login_btn",
                post_pkg="com.tencent.mm",
            )],
            success_count=1,
        ))
        best = store.find_best("k")
        self.assertEqual(best.steps[0].target_rid, "login_btn")
        self.assertTrue(plan_is_healthy(best))
        self.assertIsNone(store.find_best("nope"))


class TestMemoryRuntime(unittest.TestCase):
    def _make(self, mode, d, pkg="com.x"):
        idx = _Idx(pkg)
        execd = []
        mem = MemoryRuntime(
            instruction="打开设置并点击确定",
            memory_cfg={
                "enabled": True,
                "mode": mode,
                "min_score": 0.7,
                "plan_min_successes": 1,
                "max_replays_per_state": 2,
            },
            skill_dir=d,
            execute_fn=lambda at, aa, els: execd.append((at, aa)),
            indexer=idx,
            log_fn=lambda m: None,
        )
        mem.setup()
        return mem, idx, execd

    def test_record_then_replay_across_runs(self):
        d = tempfile.mkdtemp()
        m1, _, _ = self._make("enforce", d)
        st = m1.describe_state([_El("com.x:id/btn", "确定", (100, 200), 0)])
        m1.record_action(st, "tap", {"coordinate": [100, 200]}, "点击确定",
                         target_rid="btn", post_pkg="com.x")
        m1.on_run_end("success")

        m2, idx2, execd2 = self._make("enforce", d)
        r = m2.try_plan_replay()
        self.assertEqual(r["status"], "completed")
        self.assertEqual(execd2, [("tap", {"coordinate": [100, 200], "target": 0})])
        self.assertEqual(idx2.calls, 1)

    def test_record_mode_never_replays(self):
        d = tempfile.mkdtemp()
        m1, _, _ = self._make("record", d)
        st = m1.describe_state([_El("com.x:id/btn", "确定", (100, 200), 0)])
        m1.record_action(st, "tap", {"coordinate": [100, 200]}, "点击确定",
                         target_rid="btn", post_pkg="com.x")
        m1.on_run_end("success")

        m2, _, execd2 = self._make("record", d)
        r = m2.try_plan_replay()
        self.assertEqual(r["status"], "no_plan")
        self.assertEqual(execd2, [])

    def test_fastpath_replay_cap(self):
        d = tempfile.mkdtemp()
        m, _, _ = self._make("enforce", d)
        m.record_action(m.describe_state([_El("com.x:id/btn", "确定", (100, 200), 0)]),
                        "tap", {"coordinate": [100, 200]}, "点击确定",
                        target_rid="btn", post_pkg="com.x")
        st = m.describe_state([_El("com.x:id/btn", "确定", (100, 200), 0)])
        self.assertIsNotNone(m.fastpath(st, [_El("com.x:id/btn", "确定", (100, 200), 0)]))
        self.assertIsNotNone(m.fastpath(st, [_El("com.x:id/btn", "确定", (100, 200), 0)]))
        self.assertIsNone(m.fastpath(st, [_El("com.x:id/btn", "确定", (100, 200), 0)]))

    def test_finish_is_non_cacheable(self):
        self.assertIn("finish", NON_CACHEABLE_ACTIONS)


if __name__ == "__main__":
    unittest.main()