"""Unit tests for artemis-control-v2 Phase 1.5 + Phase 1 + Phase 2.

Covers:
  - Tier 1.5 compound-key relocalization (find_element_by_compound)
  - Phase 1 persistent notes (runner.notes + MemoryRuntime scratchpad)
  - Phase 2 lazy Diagnoser (deterministic fallback) + post-run report

Run with:
    python3 -m unittest tests.test_phase12 -v
    python3 -m pytest tests/test_phase12.py -q
"""
import json
import tempfile
import unittest
from pathlib import Path

from runner.memory_runtime import (
    MemoryRuntime,
    find_element_by_compound,
)
from runner import notes as notes_mod
from runner.diagnoser import call_diagnoser, deterministic_diagnosis
from runner.post_run_report import build_post_run_report


class _El:
    def __init__(self, rid="", text="", cd="", cls="android.widget.Button",
                 center=(0, 0), index=0):
        self.resource_id = rid
        self.text = text
        self.content_desc = cd
        self.class_name = cls
        self.center = list(center)
        self.index = index


class _Inc:
    def __init__(self, reason, category="validator_rejected", cf=1):
        self.reason = reason
        self.category = category
        self.consecutive_failures = cf


class TestCompoundRelocalize(unittest.TestCase):
    def test_level1_rid_exact_and_short(self):
        els = [_El(rid="com.x:id/btn", center=(10, 10), index=0)]
        self.assertIs(find_element_by_compound(els, rid="com.x:id/btn"), els[0])
        self.assertIs(find_element_by_compound(els, rid="btn"), els[0])
        self.assertIsNone(find_element_by_compound(els, rid="other"))

    def test_level2_class_plus_text_no_rid(self):
        els = [
            _El(rid="", text="登录", cls="android.widget.Button", center=(100, 200), index=0),
            _El(rid="", text="注册", cls="android.widget.Button", center=(300, 200), index=1),
        ]
        found = find_element_by_compound(
            els, class_name="android.widget.Button", text="登录", coord=[100, 200],
        )
        self.assertEqual(found.index, 0)

    def test_level3_content_desc_only(self):
        els = [_El(rid="", cd="退出导航", center=(500, 500), index=7)]
        found = find_element_by_compound(els, content_desc="退出导航")
        self.assertEqual(found.index, 7)

    def test_tiebreak_nearest_to_historical_coord(self):
        els = [
            _El(rid="", text="确定", cls="Button", center=(100, 100), index=0),
            _El(rid="", text="确定", cls="Button", center=(900, 900), index=1),
        ]
        found = find_element_by_compound(
            els, class_name="Button", text="确定", coord=[850, 850],
        )
        self.assertEqual(found.index, 1)

    def test_none_when_no_match(self):
        self.assertIsNone(find_element_by_compound([], rid="x"))
        self.assertIsNone(find_element_by_compound(
            [_El(rid="", text="a", cls="Button")], class_name="Button", text="b",
        ))


class TestPersistentNotes(unittest.TestCase):
    def test_note_crud_round_trip(self):
        base = Path(tempfile.mkdtemp()) / "memory"
        notes_mod.save_note_content(base, "login", "line one\nline two\nline three")
        self.assertEqual(notes_mod.read_note_content(base, "login"), "line one\nline two\nline three")
        self.assertEqual(notes_mod.read_note_content(base, "login", 2, 3), "line two\nline three")

        notes_mod.append_note_content(base, "login", "line four")
        self.assertIn("line four", notes_mod.read_note_content(base, "login"))

        self.assertTrue(notes_mod.update_note_content(base, "login", "line two", "LINE2"))
        self.assertIn("LINE2", notes_mod.read_note_content(base, "login"))
        self.assertFalse(notes_mod.update_note_content(base, "login", "missing", "x"))

        info = notes_mod.list_notes_info(base)
        self.assertIn("login", info)

    def test_task_plan_maps_to_task_plan_md(self):
        base = Path(tempfile.mkdtemp()) / "memory"
        notes_mod.save_note_content(base, "task_plan", "- [x] done")
        self.assertTrue((base / "notes" / "task_plan.md").exists())
        # "task_plan.md" key should map to the same file (trailing .md stripped)
        self.assertEqual(
            notes_mod.read_note_content(base, "task_plan.md"),
            "- [x] done",
        )

    def test_memory_runtime_scratchpad_persists_across_runs(self):
        d = tempfile.mkdtemp()

        def make():
            idx = _Idx()
            mem = MemoryRuntime(
                instruction="test",
                memory_cfg={"enabled": True, "mode": "record"},
                skill_dir=d,
                execute_fn=lambda *a: None,
                indexer=idx,
                log_fn=lambda m: None,
            )
            mem.setup()
            return mem

        m1 = make()
        m1.persist_scratchpad({"login_note": "use 手机登录 tab", "k2": "v2"})

        m2 = make()
        loaded = m2.load_scratchpad()
        self.assertEqual(loaded.get("login_note"), "use 手机登录 tab")
        self.assertEqual(loaded.get("k2"), "v2")

    def test_scratchpad_caps_keys(self):
        d = tempfile.mkdtemp()
        idx = _Idx()
        mem = MemoryRuntime(
            instruction="test",
            memory_cfg={"enabled": True, "mode": "record", "scratchpad_max_keys": 2},
            skill_dir=d,
            execute_fn=lambda *a: None,
            indexer=idx,
            log_fn=lambda m: None,
        )
        mem.setup()
        mem.persist_scratchpad({"a": "1", "b": "2", "c": "3"})
        loaded = mem.load_scratchpad()
        self.assertEqual(list(loaded.keys()), ["b", "c"])


class _Idx:
    def __init__(self, pkg="com.x"):
        self.pkg = pkg
        self.calls = 0

    def get_foreground_app(self):
        return self.pkg

    def build_index(self):
        self.calls += 1
        return []


class TestDiagnoser(unittest.TestCase):
    def test_deterministic_fallback(self):
        out = deterministic_diagnosis([_Inc("validator rejected", cf=2)])
        self.assertTrue(out["success"])
        self.assertTrue(out["recovery_hints"])

    def test_call_diagnoser_no_caller_uses_fallback(self):
        out = call_diagnoser(
            instruction="打开设置",
            plan_text="- [ ] open settings",
            actions=["tap 设置"],
            incidents=[_Inc("safety net: element not found", cf=2)],
            text_llm_caller=None,
        )
        self.assertTrue(out["success"])
        self.assertIsInstance(out["recovery_hints"], list)
        self.assertIsInstance(out["notes"], dict)


class TestPostRunReport(unittest.TestCase):
    def test_report_contains_sections(self):
        md = build_post_run_report(
            profile="pro",
            instruction="打开百度地图",
            status="success",
            end_reason="operator_finished",
            steps=6,
            actions=["open_app(百度地图)"],
            plan_text="- [x] launch",
            validator_stats={"approved": 1, "warned": 0, "rejected": 0},
            incident_count=0,
            checker={"goal_achieved": True, "confidence": 0.9,
                     "summary": "ok", "recommendation": "done"},
            memory_replay=False,
            elapsed_s=12.3,
        )
        self.assertIn("# Post-run report — pro", md)
        self.assertIn("打开百度地图", md)
        self.assertIn("Validator stats", md)
        self.assertIn("Checker verdict", md)


if __name__ == "__main__":
    unittest.main()