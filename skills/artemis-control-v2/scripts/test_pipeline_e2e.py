#!/usr/bin/env python3
"""End-to-end pipeline test — the REAL agent chain, one step at a time.

Runs exactly what the production agent runs for a single decision step:

    1. screenshot        ``_take_screenshot``          (adb exec-out screencap)
    2. element indexing  ``ElementIndexer.build_index`` (uiautomator dump + parse)
    3. VLM decision      ``_decide_action_fallback``    (screenshot + element
                          index + instruction -> one action JSON)
    4. execute           ``_execute_action_adb``        (input tap/swipe/keyevent…)
    5. verify effect     before/after screenshot pixel diff

This is the complement to ``test_brightness_swipe.py``, which only checks the
GESTURE GEOMETRY (no device, no VLM) and therefore, by design, moves nothing on
the phone. THIS test moves the phone.

Usage (on the device host):
    python3 scripts/test_pipeline_e2e.py [--device SERIAL]
                                         [--instruction "把屏幕亮度调低"]
                                         [--no-execute] [--debug]

Exit code: 0 PASS / 1 FAIL / 2 SKIP (no device).
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL_ROOT)

import artemis_agent  # noqa: E402
from artemis_agent import (  # noqa: E402
    _decide_action_fallback,
    _execute_action_adb,
    _take_screenshot,
    check_adb_device,
    ensure_screen_on,
    load_skill_config,
)
from runner.explorer import ElementIndexer  # noqa: E402

# actions that actually touch the device (vs. finish/fail/invalid)
_EXECUTABLE_ACTIONS = {
    "tap", "long_press", "swipe", "scroll", "set_slider",
    "type", "system_button", "open",
}


def _pixel_diff(path_a: str, path_b: str, threshold: int = 12):
    """(changed_pixels, changed_pct) between two screenshots, or None on error."""
    try:
        from PIL import Image, ImageChops

        a = Image.open(path_a).convert("L")
        b = Image.open(path_b).convert("L")
        if a.size != b.size:
            b = b.resize(a.size)
        diff = ImageChops.difference(a, b)
        hist = diff.histogram()
        changed = sum(hist[t] for t in range(threshold, 256))
        total = a.size[0] * a.size[1]
        return changed, round(changed / total * 100, 3)
    except Exception as e:  # pragma: no cover
        print(f"WARN: pixel diff failed: {e}")
        return None


# ── real device state (what the pixel diff cannot tell you) ────────────────
def _brightness_now(device: str | None) -> int | None:
    for ns in ("system", "global"):
        code, out, _ = artemis_agent._adb(
            ["shell", "settings", "get", ns, "screen_brightness"], device=device
        )
        txt = out.strip()
        if code == 0 and txt and txt.lstrip("-").isdigit():
            return int(txt)
    return None


def _brightness_mode(device: str | None) -> tuple[str | None, str | None]:
    """(namespace, value) of ``screen_brightness_mode`` (1 = adaptive/auto)."""
    for ns in ("system", "global"):
        code, out, _ = artemis_agent._adb(
            ["shell", "settings", "get", ns, "screen_brightness_mode"], device=device
        )
        txt = out.strip()
        if code == 0 and txt:
            return ns, txt
    return None, None


def _auto_brightness_off(device: str | None) -> tuple[bool, str | None]:
    """Disable adaptive brightness; returns (was_on, namespace) to restore later.

    With adaptive brightness ON, dragging the manual slider only moves the knob
    while the light sensor keeps the real brightness in place — the palette's
    pixel diff then shows a tiny wiggle but the screen never dims. This is the
    QS-slider equivalent of the check ``test_brightness_swipe.py`` already does.
    """
    ns, val = _brightness_mode(device)
    was_on = bool(val and val.strip() == "1")
    if was_on:
        for put_ns in ("system", "global"):
            artemis_agent._adb(
                ["shell", "settings", "put", put_ns, "screen_brightness_mode", "0"],
                device=device,
            )
        time.sleep(0.5)
    return was_on, ns


def _auto_brightness_on(device: str | None, ns: str | None) -> None:
    if not ns:
        return
    artemis_agent._adb(
        ["shell", "settings", "put", ns, "screen_brightness_mode", "1"], device=device
    )


def _is_brightness_instruction(instruction: str) -> bool:
    return any(k in instruction.lower() for k in ("亮度", "brightness", "调暗", "调亮"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--device", "-s", default=None, help="adb serial (default: first device)")
    ap.add_argument("--instruction", default="把屏幕亮度调低",
                    help="natural-language task for the VLM (default: lower brightness)")
    ap.add_argument("--no-execute", action="store_true",
                    help="run screenshot+index+VLM but do NOT execute the action")
    ap.add_argument("--debug", action="store_true", help="enable agent trace logging")
    args = ap.parse_args()

    if args.debug:
        artemis_agent._DEBUG = True

    # 0. config sanity — the VLM step needs a model/base_url.
    try:
        cfg = load_skill_config()
    except Exception as e:
        print(f"FAIL: cannot load config.json: {e}")
        return 1
    if not cfg.get("model") or not cfg.get("base_url"):
        print("FAIL: config.json missing model/base_url — the VLM step cannot run.")
        return 1
    print(f"[config] model={cfg.get('model')} base_url={cfg.get('base_url')}")

    # device
    device = check_adb_device("adb", args.device)
    if device is None:
        print("SKIP: no ADB device found.")
        return 2
    print(f"[device] {device}")
    ensure_screen_on("adb", device)

    shots = tempfile.mkdtemp(prefix="e2e_shots_")
    before = os.path.join(shots, "before.png")
    after = os.path.join(shots, "after.png")

    # 1. screenshot
    print("[1/5] screenshot …")
    if not _take_screenshot("adb", device, Path(before)):
        print("FAIL: screenshot capture failed.")
        return 1
    res = artemis_agent._DEVICE_RESOLUTION
    print(f"      captured {before}  ({res[0]}x{res[1] if res else '?'})")

    # 2. element indexing (uiautomator dump -> parsed, numbered elements)
    print("[2/5] element indexing …")
    indexer = ElementIndexer(adb_path="adb", device=device)
    elements = indexer.build_index(Path(before))
    if not elements:
        print("FAIL: element index is empty — uiautomator dump yielded no nodes.")
        return 1
    print(f"      indexed {len(elements)} elements; first 5:")
    for el in elements[:5]:
        try:
            print("        " + el.render_index_line())
        except Exception:
            print("        " + str(el))

    # 3. VLM decision (screenshot + element index -> one action)
    print(f"[3/5] VLM decision for: {args.instruction!r} …")
    decision = _decide_action_fallback(
        instruction=args.instruction,
        step=1,
        elements=elements,
        incident_tracker=None,
        last_coords=[],
        screenshot_path=before,
        previous_actions=[],
    )
    action_type, action_args, desc = decision
    print(f"      action={action_type}  args={action_args}")
    print(f"      desc={desc!r}")

    if action_type in ("invalid",):
        print("FAIL: VLM returned an unrecognised action (pipeline produced nothing executable).")
        return 1
    if action_type in ("finish", "fail"):
        print(f"NOTE: VLM decided '{action_type}' — pipeline is alive, but the task was"
              f" not performed. desc={desc!r}")
        return 0

    # 4. execute
    if args.no_execute:
        print("[4/5] execute (DRY RUN — not executed)")
        print("PASS: screenshot + index + VLM pipeline works; execution skipped.")
        return 0

    brightness_task = _is_brightness_instruction(args.instruction)
    state_before = _brightness_now(device) if brightness_task else None
    if brightness_task:
        print(f"      [state] screen_brightness before = {state_before}")
        was_auto, auto_ns = _auto_brightness_off(device)
        if was_auto:
            print("      [state] adaptive/auto brightness was ON — disabled for the test")
            state_before = _brightness_now(device)

    print(f"[4/5] execute {action_type} …")
    err = _execute_action_adb("adb", device, action_type, action_args, elements)
    if err:
        print(f"      execute returned error: {err!r}")
    time.sleep(1.2)

    try:
        # 5. verify effect by pixel diff AND by real device state
        print("[5/5] verify effect …")
        if not _take_screenshot("adb", device, Path(after)):
            print("FAIL: post-action screenshot failed.")
            return 1
        d = _pixel_diff(before, after)
        if d is None:
            print("FAIL: could not compute pixel diff.")
            return 1
        changed, pct = d
        print(f"      screen changed {changed} px ({pct}%)")

        if action_type in _EXECUTABLE_ACTIONS and changed == 0:
            print("FAIL: action executed but the screen did not change a single pixel — "
                  "the gesture was not delivered (touch slop / wrong control / auto-brightness).")
            return 1

        if brightness_task:
            state_after = _brightness_now(device)
            print(f"      [state] screen_brightness after = {state_after} "
                  f"(delta = {state_after - state_before if state_after is not None and state_before is not None else '?'})")
            if state_before is not None and state_after is not None:
                if state_after == state_before:
                    print("FAIL: brightness task ran, but screen_brightness did NOT move — "
                          "the knob was dragged yet the real brightness is unchanged. "
                          "This is the adaptive-brightness / wrong-slider case the pixel "
                          "diff (a tiny knob wiggle) cannot detect.")
                    return 1
                want_down = any(k in args.instruction for k in ("调低", "调暗", "lower", "dim", "降低"))
                if want_down and state_after > state_before:
                    print("FAIL: instruction asked to LOWER brightness, but it INCREASED "
                          f"({state_before} → {state_after}). The wrong slider was dragged.")
                    return 1
        print("PASS: full pipeline (screenshot → index → VLM → execute → effect) works.")
        return 0
    finally:
        if brightness_task:
            _auto_brightness_on(device, auto_ns)


if __name__ == "__main__":
    sys.exit(main())