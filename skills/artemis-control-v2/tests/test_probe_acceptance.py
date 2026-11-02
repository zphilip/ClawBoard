"""Acceptance-probe regression tests for the mute/silent goal.

Covers the failure mode seen on ColorOS-like ROMs (radxa-cubie-a7a): the ring
stream is clamped to >= 1 while the ringer mode is NORMAL, so ``volume==0`` is
unreachable — but an agent must still be able to *see* SILENT/VIBRATE and DND
via probes, or it loops forever even after the phone is actually silent.
"""

from __future__ import annotations

import unittest

import artemis_agent
from runner.probes import derive_probes


def _accept(verify_text: str, evidence: dict) -> object:
    return artemis_agent._acceptance_from_probes([verify_text], evidence)


class TestSilentProbeAcceptance(unittest.TestCase):
    def test_ring_volume_zero_satisfies(self):
        evidence = {"t": "probe volume ring: [v] volume is 0 in range [0..7]"}
        self.assertIs(_accept("t", evidence), True)

    def test_ring_volume_one_alone_is_contradicted_not_inconclusive(self):
        # "volume is 1" is a decisive reading: it contradicts "== 0" instead of
        # silently deferring to the Checker.
        evidence = {"t": "probe volume ring: [v] volume is 1 in range [0..7]"}
        self.assertIs(_accept("t", evidence), False)

    def test_mringermode_silent_or_vibrate_satisfies(self):
        for mode in ("0", "1"):
            evidence = {"t": f"probe audio: ... mRingerMode={mode} ..."}
            self.assertIs(_accept("t", evidence), True, f"mRingerMode={mode}")

    def test_mringermode_normal_does_not_satisfy(self):
        evidence = {"t": "probe audio: ... mRingerMode=2 ..."}
        self.assertIs(_accept("t", evidence), False)  # decisive: normal ≠ silent

    def test_word_form_ringer_mode_satisfies(self):
        for word in ("silent", "vibrate"):
            evidence = {"t": f"probe audio: Ringer mode: {word}"}
            self.assertIs(_accept("t", evidence), True, f"Ringer mode: {word}")

    def test_word_form_ringer_mode_normal_not_satisfied(self):
        evidence = {"t": "probe audio: Ringer mode: normal"}
        self.assertIs(_accept("t", evidence), False)

    def test_zen_mode_nonzero_satisfies(self):
        evidence = {"t": "probe setting global zen_mode: 3"}
        self.assertIs(_accept("t", evidence), True)

    def test_zen_mode_zero_does_not_satisfy(self):
        evidence = {"t": "probe setting global zen_mode: 0"}
        self.assertIs(_accept("t", evidence), False)


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
        self.assertIn("setting", kinds)
        zen = [p for k, p in probes if k == "setting"]
        self.assertTrue(any(p.get("key") == "zen_mode" for p in zen))


if __name__ == "__main__":
    unittest.main()