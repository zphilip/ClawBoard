#!/usr/bin/env python3
"""
Unit tests for the Tier-1 robust memory matching:

  1. Ad-container resource-id stripping (image-only ads that have no text and
     therefore miss the keyword blacklist).
  2. Component extraction (build_ui_components).
  3. Fuzzy state similarity (ui_similarity) — tolerant to a small text ad /
     feed card / badge change but strict on foreground-package change.
  4. Fuzzy scoring in retriever.score_record — clears min_score=0.7 even when
     the exact state_key differs, while keeping exact matches at 1.0.
  5. MemoryRecord persistence round-trip of the new fuzzy fields.

USAGE:
    cd ClawBoard/skills/mobile-control
    python3 tests/test_memory_fuzzy.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))

from memory.models import MemoryRecord
from memory.retriever import score_record
from memory.signature import (
    build_intent_signature,
    build_state_key,
    build_ui_components,
    build_ui_fingerprint,
    ui_similarity,
)

PKG = "com.example.app"


def _ui(*extra_lines: str) -> str:
    base = [
        "UI elements on screen:",
        '  [Button] "登录" id=login_btn bounds=[10,20][200,80] clickable',
        '  [EditText] "手机号" id=phone_input bounds=[10,100][400,160] clickable',
        '  [Button] "密码登录" id=pwd_tab bounds=[10,200][200,260] clickable',
    ]
    return "\n".join(base + list(extra_lines))


class TestAdResourceIdStripping(unittest.TestCase):
    def test_image_ad_without_text_is_dropped(self):
        ui_ad = _ui('  [ImageView] "" id=ttad_banner bounds=[0,300][1080,440] clickable')
        comp = build_ui_components(PKG, ui_ad)
        self.assertNotIn("ttad_banner", comp.resource_ids)

    def test_image_ad_does_not_change_fingerprint(self):
        a = build_ui_fingerprint(PKG, _ui())
        b = build_ui_fingerprint(
            PKG, _ui('  [ImageView] "" id=ttad_banner bounds=[0,300][1080,440] clickable')
        )
        self.assertEqual(a, b)


class TestUiSimilarity(unittest.TestCase):
    def test_text_ad_tolerated(self):
        stored = build_ui_components(PKG, _ui())
        current = build_ui_components(
            PKG, _ui('  [TextView] "限时抢购" id=feed_card bounds=[0,300][1080,440] clickable')
        )
        self.assertGreaterEqual(ui_similarity(stored, current), 0.7)

    def test_foreground_package_is_hard_gate(self):
        stored = build_ui_components(PKG, _ui())
        current = build_ui_components("com.other.app", _ui())
        self.assertEqual(ui_similarity(stored, current), 0.0)

    def test_identical_screens_score_one(self):
        stored = build_ui_components(PKG, _ui())
        self.assertEqual(ui_similarity(stored, stored), 1.0)


class TestFuzzyScoring(unittest.TestCase):
    def _record(self, fp: str, components) -> MemoryRecord:
        return MemoryRecord(
            state_key=build_state_key(INTENT, fp, "default"),
            intent_key=INTENT,
            action_type="click",
            action_args={"coordinate": [500, 500]},
            success_count=1,
            fail_count=0,
            foreground_pkg=components.foreground_pkg,
            resource_ids=sorted(components.resource_ids),
            texts=sorted(components.labels),
        )

    def test_exact_match_still_scores_one(self):
        comp = build_ui_components(PKG, _ui())
        fp = build_ui_fingerprint(PKG, _ui())
        rec = self._record(fp, comp)
        self.assertAlmostEqual(
            score_record(rec, build_state_key(INTENT, fp, "default"), INTENT),
            1.0,
        )

    def test_fuzzy_match_clears_min_score_when_exact_misses(self):
        comp_a = build_ui_components(PKG, _ui())
        ui_b = _ui('  [TextView] "限时抢购" id=feed_card bounds=[0,300][1080,440] clickable')
        comp_b = build_ui_components(PKG, ui_b)
        fp_a = build_ui_fingerprint(PKG, _ui())
        fp_b = build_ui_fingerprint(PKG, ui_b)

        rec = self._record(fp_a, comp_a)
        state_key_b = build_state_key(INTENT, fp_b, "default")

        # exact path: state_key differs -> intent-only 0.5
        self.assertLess(score_record(rec, state_key_b, INTENT), 0.7)
        # fuzzy path: component overlap pushes it over min_score=0.7
        self.assertGreaterEqual(
            score_record(rec, state_key_b, INTENT, current_ui=comp_b), 0.7
        )

    def test_wrong_app_not_replayed_even_fuzzily(self):
        comp_a = build_ui_components(PKG, _ui())
        comp_other = build_ui_components("com.other.app", _ui())
        rec = self._record(build_ui_fingerprint(PKG, _ui()), comp_a)
        score = score_record(rec, "unused-state-key", INTENT, current_ui=comp_other)
        self.assertLess(score, 0.7)


class TestMemoryRecordRoundTrip(unittest.TestCase):
    def test_new_fields_survive_round_trip(self):
        rec = MemoryRecord(
            state_key="sk", intent_key="ik", action_type="click",
            action_args={"coordinate": [1, 2]},
            foreground_pkg="com.x", resource_ids=["a", "b"], texts=["c", "d"],
        )
        rec2 = MemoryRecord.from_dict(rec.to_dict())
        self.assertEqual(rec2.foreground_pkg, "com.x")
        self.assertEqual(rec2.resource_ids, ["a", "b"])
        self.assertEqual(rec2.texts, ["c", "d"])


INTENT = build_intent_signature("登录微信")

if __name__ == "__main__":
    unittest.main()