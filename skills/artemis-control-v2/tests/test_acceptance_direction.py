"""Direction-aware acceptance: reverse tasks must not be silently inverted.

Regression cover for the two field bugs:
  3. "关闭勿扰" was reported DONE while ``zen_mode == 1`` (hard-coded ``!= 0``).
  4. "调高音量" could never be satisfied (hard-coded ``volume == 0``).
"""

from __future__ import annotations

import unittest

from runner.acceptance import (
    POL_DOWN,
    POL_OFF,
    POL_ON,
    POL_SILENT,
    POL_UP,
    evaluate_acceptance,
    evaluate_goal,
    parse_evidence,
    parse_goal,
)


def _eval(text: str, evidence: str):
    return evaluate_goal(parse_goal(text), parse_evidence(evidence))


class TestGoalParsing(unittest.TestCase):
    def test_polarity_from_verb(self):
        self.assertEqual(parse_goal("关闭勿扰模式").polarity, POL_OFF)
        self.assertEqual(parse_goal("打开勿扰模式").polarity, POL_ON)
        self.assertEqual(parse_goal("把手机设置静音").polarity, POL_SILENT)
        self.assertEqual(parse_goal("调低屏幕亮度").polarity, POL_DOWN)
        self.assertEqual(parse_goal("调高音量").polarity, POL_UP)

    def test_subject_detection(self):
        self.assertEqual(parse_goal("关闭勿扰模式").subject, "zen")
        self.assertEqual(parse_goal("打开 WiFi").subject, "wifi")
        self.assertEqual(parse_goal("开启飞行模式").subject, "airplane")
        self.assertEqual(parse_goal("调低亮度").subject, "brightness")
        self.assertEqual(parse_goal("把铃声调到静音").subject, "ringer")

    def test_explicit_want_hint_overrides(self):
        goal = parse_goal("亮度合适 [want: <40]")
        self.assertEqual(goal.comparator, "<")
        self.assertEqual(goal.target, 40)


class TestReverseDirections(unittest.TestCase):
    """The two bugs: an off/up goal must not be judged by the on/down rule."""

    def test_dnd_off_is_only_satisfied_by_zero(self):
        self.assertIs(_eval("关闭勿扰模式", "probe setting global zen_mode: 1"), False)
        self.assertIs(_eval("关闭勿扰模式", "probe setting global zen_mode: 0"), True)

    def test_dnd_on_satisfied_by_nonzero(self):
        self.assertIs(_eval("打开勿扰模式", "probe setting global zen_mode: 1"), True)
        self.assertIs(_eval("打开勿扰模式", "probe setting global zen_mode: 0"), False)

    def test_wifi_on_off(self):
        self.assertIs(_eval("打开 WiFi", "probe setting global wifi_on: 1"), True)
        self.assertIs(_eval("打开 WiFi", "probe setting global wifi_on: 0"), False)
        self.assertIs(_eval("关闭 WiFi", "probe setting global wifi_on: 0"), True)
        self.assertIs(_eval("关闭 WiFi", "probe setting global wifi_on: 1"), False)

    def test_airplane_on(self):
        self.assertIs(
            _eval("开启飞行模式", "probe setting global airplane_mode_on: 1"), True
        )
        self.assertIs(
            _eval("开启飞行模式", "probe setting global airplane_mode_on: 0"), False
        )

    def test_raise_volume_defers_when_goal_needs_baseline(self):
        # "louder" cannot be judged from one reading — must defer, not claim
        # success on volume>0 (bug 4) nor fail on volume==0.
        self.assertIsNone(_eval("调高音量", "probe volume ring: [v] volume is 5 in range [0..7]"))
        self.assertIsNone(_eval("调高音量", "probe volume ring: [v] volume is 0 in range [0..7]"))

    def test_lower_brightness_defers(self):
        self.assertIsNone(
            _eval("调低亮度", "probe setting system screen_brightness: 67")
        )

    def test_explicit_numeric_target_is_honoured(self):
        self.assertIs(
            _eval("亮度合适 [want: <40]", "probe setting system screen_brightness: 30"),
            True,
        )
        self.assertIs(
            _eval("亮度合适 [want: <40]", "probe setting system screen_brightness: 90"),
            False,
        )


class TestSilentStillWorks(unittest.TestCase):
    """The silent path must keep its union semantics (volume / ringer / DND)."""

    def test_ring_zero(self):
        self.assertIs(_eval("把手机静音", "probe volume ring: [v] volume is 0 in range [0..7]"), True)

    def test_ring_one_alone_contradicts(self):
        self.assertIs(_eval("把手机静音", "probe volume ring: [v] volume is 1 in range [0..7]"), False)

    def test_ringer_mode_words(self):
        self.assertIs(_eval("静音", "probe audio: Ringer mode: silent"), True)
        self.assertIs(_eval("静音", "probe audio: Ringer mode: vibrate"), True)
        self.assertIs(_eval("静音", "probe audio: Ringer mode: normal"), False)

    def test_mringermode(self):
        self.assertIs(_eval("静音", "probe audio: ... mRingerMode=0 ..."), True)
        self.assertIs(_eval("静音", "probe audio: ... mRingerMode=2 ..."), False)

    def test_dnd_satisfies_silent(self):
        self.assertIs(
            _eval("静音", "probe audio: - zen mode:ZEN_MODE_IMPORTANT_INTERRUPTIONS"),
            True,
        )
        self.assertIs(_eval("静音", "probe setting global zen_mode: 3"), True)
        self.assertIs(_eval("静音", "probe setting global zen_mode: 0"), False)

    def test_nothing_probed_defers(self):
        self.assertIsNone(evaluate_acceptance(["静音"], {}))


if __name__ == "__main__":
    unittest.main()