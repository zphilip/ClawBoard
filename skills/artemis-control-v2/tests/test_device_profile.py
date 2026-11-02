"""Device baseline: relative criteria and per-device quirks come from MEASUREMENT.

A VLM cannot know that this ROM clamps STREAM_RING to >= 1 in NORMAL mode, nor
that DND is what actually silences it — that is device state, not general
knowledge. These tests pin the deterministic reading + the relative acceptance it
unlocks.
"""

from __future__ import annotations

import unittest

from runner import device_profile as DP
from runner.acceptance import evaluate_acceptance

#: Realistic verbose ``media volume --get`` output from the field device.
VERBOSE_VOL = (
    "[v] will control stream=2 (STREAM_RING)\n"
    "[v] will get volume\n"
    "[v] Connecting to AudioService\n"
    "[v] volume is 1 in range [0..7]"
)
#: AOSP prints the bare integer.
BARE_VOL = "5"

AUDIO = """
  Ringer mode:
  - zen mode:ZEN_MODE_IMPORTANT_INTERRUPTIONS
"""


def _fake_run(table: dict[tuple, str]):
    """Build a run() stub keyed by (namespace/key or stream/label)."""

    def _run(argv: list[str]) -> tuple[int, str, str]:
        key = tuple(argv)
        for pattern, out in table.items():
            if all(p in argv for p in pattern):
                return 0, out, ""
        return 0, "", ""

    return _run


class TestVolumeParsing(unittest.TestCase):
    def test_verbose_shape(self):
        info = DP._parse_volume(VERBOSE_VOL)
        self.assertEqual(info, {"value": 1, "min": 0, "max": 7})

    def test_bare_integer_shape(self):
        self.assertEqual(DP._parse_volume(BARE_VOL), {"value": 5})

    def test_unparseable_is_not_guessed(self):
        self.assertEqual(DP._parse_volume("error: no service"), {})


class TestAudioParsing(unittest.TestCase):
    def test_word_form_zen_and_empty_ringer(self):
        ringer, zen = DP._parse_audio(AUDIO)
        self.assertIsNone(ringer)      # "Ringer mode:" with no value
        self.assertEqual(zen, 1)       # ZEN_MODE_IMPORTANT_INTERRUPTIONS

    def test_numeric_forms(self):
        ringer, zen = DP._parse_audio("mRingerMode=0  zen_mode=0")
        self.assertEqual(ringer, 0)
        self.assertEqual(zen, 0)


class TestCapture(unittest.TestCase):
    def test_capture_reads_state_and_flags_minimum(self):
        run = _fake_run({
            ("getprop", "ro.product.model"): "PJD110",
            ("getprop", "ro.build.version.sdk"): "33",
            ("--stream", "2"): VERBOSE_VOL,
            ("--stream", "3"): "[v] volume is 0 in range [0..30]",
            ("settings", "get", "global", "zen_mode"): "1",
            ("settings", "get", "system", "screen_brightness"): "67",
            ("dumpsys", "audio"): AUDIO,
        })
        prof = DP.capture(run)
        self.assertEqual(prof.model, "PJD110")
        self.assertEqual(prof.sdk, 33)
        self.assertEqual(prof.ring_value, 1)
        self.assertEqual(prof.ring_min, 0)
        self.assertEqual(prof.zen_mode, 1)
        self.assertEqual(prof.settings["system.screen_brightness"], 67)
        # value == min is the *effective* floor only when min is reached; here
        # min is 0 and value 1, so it is NOT flagged.
        self.assertFalse(prof.ring_is_at_minimum)

    def test_minimum_detected_when_value_hits_lower_bound(self):
        run = _fake_run({
            ("getprop", "ro.product.model"): "X",
            ("--stream", "2"): "[v] volume is 1 in range [1..7]",
        })
        prof = DP.capture(run)
        self.assertTrue(prof.ring_is_at_minimum)

    def test_prompt_block_is_measured_facts(self):
        run = _fake_run({
            ("getprop", "ro.product.model"): "PJD110",
            ("--stream", "2"): "[v] volume is 1 in range [1..7]",
            ("settings", "get", "system", "screen_brightness"): "67",
            ("dumpsys", "audio"): AUDIO,
        })
        text = DP.capture(run).describe_for_prompt()
        self.assertIn("ring volume: 1 of range [1..7]", text)
        self.assertIn("ACTIVE", text)
        self.assertIn("ALREADY at its lower bound", text)


class TestRelativeAcceptance(unittest.TestCase):
    """A baseline turns "lower it" into a decidable predicate."""

    def test_brightness_down_judged_by_baseline(self):
        crit = "调低亮度 [probe: setting system screen_brightness]"
        baseline = {"screen_brightness": 67}

        def verdict(value: int) -> object:
            return evaluate_acceptance(
                [crit], {crit: f"probe setting system screen_brightness: {value}"}, baseline
            )

        self.assertIs(verdict(40), True)
        self.assertIs(verdict(67), False)
        self.assertIs(verdict(90), False)

    def test_volume_up_judged_by_baseline(self):
        crit = "调高音量"
        baseline = {"ring_volume": 1}

        def verdict(value: int) -> object:
            return evaluate_acceptance(
                [crit],
                {crit: f"probe volume ring: [v] volume is {value} in range [0..7]"},
                baseline,
            )

        self.assertIs(verdict(5), True)
        self.assertIs(verdict(1), False)

    def test_no_baseline_stays_inconclusive(self):
        crit = "调高音量"
        ev = {crit: "probe volume ring: [v] volume is 5 in range [0..7]"}
        self.assertIsNone(evaluate_acceptance([crit], ev, None))


if __name__ == "__main__":
    unittest.main()