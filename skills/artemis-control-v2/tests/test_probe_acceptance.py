"""Silent-goal acceptance: probe shapes and the union semantics.

Complements ``test_acceptance_direction.py`` (which covers reverse directions).
Here every criterion is a real "make it silent" predicate, because the
acceptance direction is parsed FROM the criterion text — a placeholder like
``"t"`` carries no polarity and correctly evaluates to inconclusive.
"""

from __future__ import annotations

import unittest

import artemis_agent
from runner.probes import derive_probes

#: A real criterion: silent is satisfiable via volume 0, ringer SILENT/VIBRATE,
#: or DND — this is the union the evaluator must understand.
SILENT_CRITERION = "响铃音量为 0 或铃声模式为静音/振动"


def _accept(evidence: str) -> object:
    return artemis_agent._acceptance_from_probes(
        [SILENT_CRITERION], {SILENT_CRITERION: evidence}
    )


class TestSilentProbeAcceptance(unittest.TestCase):
    def test_ring_volume_zero_satisfies(self):
        self.assertIs(_accept("probe volume ring: [v] volume is 0 in range [0..7]"), True)

    def test_ring_volume_one_alone_is_contradicted_not_inconclusive(self):
        # A decisive "volume is 1" contradicts the silent predicate; it must not
        # silently defer to the Checker as if nothing had been read.
        self.assertIs(_accept("probe volume ring: [v] volume is 1 in range [0..7]"), False)

    def test_mringermode_silent_or_vibrate_satisfies(self):
        for mode in ("0", "1"):
            self.assertIs(
                _accept(f"probe audio: ... mRingerMode={mode} ..."),
                True,
                f"mRingerMode={mode}",
            )

    def test_mringermode_normal_does_not_satisfy(self):
        self.assertIs(_accept("probe audio: ... mRingerMode=2 ..."), False)

    def test_word_form_ringer_mode_satisfies(self):
        for word in ("silent", "vibrate"):
            self.assertIs(_accept(f"probe audio: Ringer mode: {word}"), True, word)

    def test_word_form_ringer_mode_normal_not_satisfied(self):
        self.assertIs(_accept("probe audio: Ringer mode: normal"), False)

    def test_zen_mode_nonzero_satisfies(self):
        self.assertIs(_accept("probe setting global zen_mode: 3"), True)

    def test_zen_mode_word_form_satisfies(self):
        self.assertIs(
            _accept("probe audio: - zen mode:ZEN_MODE_IMPORTANT_INTERRUPTIONS"), True
        )

    def test_zen_mode_zero_does_not_satisfy(self):
        self.assertIs(_accept("probe setting global zen_mode: 0"), False)


class TestDeriveSilentProbes(unittest.TestCase):
    def test_volume_hint_drags_in_audio_and_zen(self):
        probes = derive_probes("响铃音量为 0 或铃声模式为静音/振动 [probe: volume ring]")
        kinds = [k for k, _ in probes]
        self.assertIn("volume", kinds)
        self.assertIn("audio", kinds)
        zen = [p for k, p in probes if k == "setting"]
        self.assertTrue(any(p.get("key") == "zen_mode" for p in zen))

    def test_silent_keyword_derives_volume_audio_zen(self):
        probes = derive_probes("把手机调成静音")
        kinds = [k for k, _ in probes]
        self.assertIn("volume", kinds)
        self.assertIn("audio", kinds)
        zen = [p for k, p in probes if k == "setting"]
        self.assertTrue(any(p.get("key") == "zen_mode" for p in zen))

    def test_reverse_goal_does_not_drag_in_silent_probes(self):
        # "关闭勿扰" is an OFF goal, not a silent goal: it must probe zen only,
        # not pull in the ring-volume / dumpsys-audio union.
        probes = derive_probes("关闭勿扰模式")
        kinds = [k for k, _ in probes]
        self.assertEqual(kinds, ["setting"])
        self.assertEqual(probes[0][1].get("key"), "zen_mode")


if __name__ == "__main__":
    unittest.main()