#!/usr/bin/env python3
"""Diagnose why ``set_slider`` on the volume page does nothing.

Dumps, on the CURRENT screen, every SeekBar/ProgressBar with its index, text,
resource-id and bounds, then prints the device truth that the acceptance probes
rely on: the raw ``media volume --stream 2 --get`` output, the ringer mode, and
the DND (zen_mode) setting.

Run AFTER opening ``设置 → 声音与振动`` (Settings → Sound & Vibration) on the
phone, with the screen on.

    python3 scripts/diagnose_volume_slider.py [-s SERIAL]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT))

from artemis_agent import _adb, check_adb_device  # noqa: E402

_BOUNDS_RE = re.compile(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"')
_CLASS_RE = re.compile(r'class="([^"]+)"')
_RID_RE = re.compile(r'resource-id="([^"]*)"')
_TEXT_RE = re.compile(r'text="([^"]*)"')


def _dump_elements(device) -> str:
    code, _, err = _adb(
        ["shell", "uiautomator", "dump", "/sdcard/diag.xml"], device=device
    )
    if code != 0:
        # fallback: dump to stdout default path
        _adb(["shell", "uiautomator", "dump"], device=device)
    code, xml, _ = _adb(["shell", "cat", "/sdcard/diag.xml"], device=device)
    if code != 0 or not xml.strip():
        print("FAIL: uiautomator dump empty.", file=sys.stderr)
        sys.exit(1)
    return xml


def _run(device, *argv) -> str:
    code, out, err = _adb(["shell"] + list(argv), device=device, timeout=10)
    return out if code == 0 else f"(rc={code}) {err.strip()[:120]}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--device", "-s", default=None, help="adb serial")
    args = ap.parse_args()

    device = check_adb_device("adb", args.device)
    if device is None:
        print("SKIP: no ADB device.")
        return 2
    print(f"[device] {device}\n")

    xml = _dump_elements(device)

    print("==== SeekBar / ProgressBar on screen ====")
    idx = 0
    found = 0
    for chunk in re.split(r"(?=<node)", xml):
        bm = _BOUNDS_RE.search(chunk)
        if not bm:
            continue
        cls = (_CLASS_RE.search(chunk) or [None, ""])[1].lower()
        if "seekbar" not in cls and "progressbar" not in cls:
            idx += 1
            continue
        rid = (_RID_RE.search(chunk) or [None, ""])[1]
        text = (_TEXT_RE.search(chunk) or [None, ""])[1]
        x1, y1, x2, y2 = map(int, bm.groups())
        print(f"  #{idx} {cls} text={text!r} rid={rid!r} bounds=[{x1},{y1},{x2},{y2}] "
              f"center=({(x1+x2)//2},{(y1+y2)//2})")
        found += 1
        idx += 1
    if not found:
        print("  (none found — the volume control may NOT be a SeekBar on this ROM)")
    print()

    print("==== raw probes (the real device truth) ====")
    print(f"media volume --stream 2 --get:")
    print("  " + _run(device, "media", "volume", "--stream", "2", "--get").replace("\n", "\n  "))
    print()
    print("media volume --stream 3 --get (music):")
    print("  " + _run(device, "media", "volume", "--stream", "3", "--get").replace("\n", "\n  "))
    print()
    print("dumpsys audio | ringer/mode/state lines:")
    audio = _run(device, "dumpsys", "audio")
    for ln in audio.splitlines():
        if re.search(r"ring(er)?[_-]?mode|audio_mode|mode\s*[:=]", ln, re.IGNORECASE):
            print("  " + ln)
    print()
    print("global zen_mode (DND):", _run(device, "settings", "get", "global", "zen_mode"))
    print("system mode_ringer (OEM):", _run(device, "settings", "get", "system", "mode_ringer"))
    return 0


if __name__ == "__main__":
    sys.exit(main())