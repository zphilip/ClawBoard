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
import re
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


def _wm_size(device: str | None) -> tuple[int, int] | None:
    code, out, _ = artemis_agent._adb(["shell", "wm", "size"], device=device)
    if code != 0:
        return None
    m = re.search(r"(\d{3,5})x(\d{3,5})", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _ensure_full_quick_settings(device: str | None) -> None:
    """Open the shade, then expand to FULL Quick Settings.

    On this ROM the brightness slider in the half-open (peeked) shade is a
    NON-interactive preview: it renders as a real SeekBar with a live value
    text, so the VLM targets it and the drag animates the knob, but the result
    is discarded on release and ``screen_brightness`` never moves. Only the
    fully-expanded shade commits the drag. Mirrors test_brightness_swipe.py
    steps 1-2 (the top-edge swipe is the ONE gesture exempt from the
    edge-safe rect by design).
    """
    w, h = _wm_size(device) or (1440, 3120)
    artemis_agent._adb(
        ["shell", "input", "swipe",
         str(w // 2), str(max(1, int(h * 0.01))),
         str(w // 2), str(int(h * 0.55)), "350"],
        device=device,
    )
    time.sleep(0.8)
    artemis_agent._adb(
        ["shell", "input", "swipe",
         str(w // 2), str(int(h * 0.30)),
         str(w // 2), str(int(h * 0.75)), "350"],
        device=device,
    )
    time.sleep(0.8)


def _wait_brightness_settle(device: str | None, before: int | None,
                            timeout: float = 20.0, interval: float = 0.5) -> int | None:
    """Poll ``screen_brightness`` until it moves off ``before``.

    A brightness slider commits its value on ACTION_UP and SystemUI then ramps
    the change with a smooth animation; reading the setting immediately after
    ``input swipe`` returns (it blocks roughly the gesture duration) keeps
    seeing the old value, which the first field run showed as a false
    "delta = 0". Poll briefly so the settled value is what gets asserted.
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = _brightness_now(device)
        if last is not None and last != before:
            # one more read after a beat, to catch the ramp's final value
            time.sleep(interval)
            settled = _brightness_now(device)
            return settled if settled is not None else last
        time.sleep(interval)
    return last


def _slider_text(elements) -> tuple[str | None, int | None]:
    """The numeric ``text`` of the first SeekBar/ProgressBar in the dump.

    The slider's own value label (e.g. 812.0) is committed by the drag even
    when ``settings get screen_brightness`` lags behind (a known Settings.System
    cache effect on some ROMs). Reading it is the ground truth for "did the
    knob actually move", independent of the settings table.
    """
    for el in (elements or []):
        cls = (getattr(el, "class_name", "") or "").lower()
        if "seekbar" in cls or "progressbar" in cls:
            t = getattr(el, "text", "") or ""
            if t.strip():
                return t, getattr(el, "index", None)
    for el in (elements or []):
        cls = (getattr(el, "class_name", "") or "").lower()
        if "seekbar" in cls or "progressbar" in cls:
            return (getattr(el, "text", "") or None), getattr(el, "index", None)
    return None, None


def _as_float(text: str | None) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _backlight_now(device: str | None) -> int | None:
    """The ACTUAL applied backlight from ``dumpsys power``.

    Neither the screenshot nor ``settings get screen_brightness`` sees the real
    panel backlight: ``screencap`` captures the composited framebuffer (no
    backlight), and the Settings.System write is asynchronous. ``dumpsys power``
    reports the brightness the power controller is actually driving, so it is
    the only reading that crosses the "the screen really got dimmer" line at
    verify time.
    """
    code, out, _ = artemis_agent._adb(
        ["shell", "dumpsys", "power"], device=device, timeout=15
    )
    if code != 0:
        return None
    for pat in (r"mScreenBrightness=(\d+)",
                r"sScreenBrightness=(\d+)",
                r"mBrightness=(\d+)",
                r"screenBrightness=(\d+)"):
        m = re.search(pat, out)
        if m:
            v = int(m.group(1))
            # -1 / 0 usually mean "no override", not the driven value
            if v > 0:
                return v
    return None


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

    # Precondition for a brightness task: adaptive brightness OFF first (so the
    # shade reads mode=0 when it opens) and the FULL Quick Settings expanded —
    # the peeked brightness bar is a non-interactive preview and its drag does
    # not commit, which is exactly the "knob moved, screen_brightness stuck"
    # signature the earlier runs showed.
    brightness_task = _is_brightness_instruction(args.instruction)
    was_auto, auto_ns = (False, None)
    if brightness_task:
        was_auto, auto_ns = _auto_brightness_off(device)
        if was_auto:
            print("      [state] adaptive/auto brightness was ON — disabled for the test")
        _ensure_full_quick_settings(device)

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

    state_before = _brightness_now(device) if brightness_task else None
    slider_text_before = None
    backlight_before = None
    if brightness_task:
        slider_text_before, _ = _slider_text(elements)
        backlight_before = _backlight_now(device)
        print(f"      [state] screen_brightness before = {state_before}   "
              f"slider text before = {slider_text_before!r}   "
              f"backlight before = {backlight_before}")

    print(f"[4/5] execute {action_type} …")
    err = _execute_action_adb("adb", device, action_type, action_args, elements)
    if err:
        print(f"      execute returned error: {err!r}")

    try:
        print("[5/5] verify effect …")

        if brightness_task:
            # Plain sleep with NO adb calls in this window. The earlier rapid
            # settings-get polling + screencap + uiautomator re-dump kept the
            # device busy, so the slider's commit/animation was starved and only
            # played out after the program exited — the exact "the bar starts
            # changing after the run finishes" symptom. Let the commit land
            # undisturbed, then read everything once.
            time.sleep(2.5)
            state_after = _brightness_now(device)
            backlight_after = _backlight_now(device)
            after_elements = indexer.build_index(Path(before))
            slider_text_after, _ = _slider_text(after_elements)

            print(f"      [state] screen_brightness after = {state_after} "
                  f"(delta = {state_after - state_before if state_after is not None and state_before is not None else '?'})   "
                  f"backlight after = {backlight_after}")
            print(f"      [slider] slider text before = {slider_text_before!r}   "
                  f"after = {slider_text_after!r}")

            want_down = any(k in args.instruction for k in ("调低", "调暗", "lower", "dim", "降低"))
            v_before = _as_float(slider_text_before)
            v_after = _as_float(slider_text_after)

            moved = False
            wrong_way = False
            # The slider's own value label — the most direct "did the knob move"
            if v_before is not None and v_after is not None and v_after != v_before:
                moved = True
                wrong_way = (v_after > v_before) if want_down else (v_after < v_before)
            # Applied backlight (the real panel brightness).
            if backlight_before is not None and backlight_after is not None \
                    and backlight_after != backlight_before:
                moved = True
                if backlight_after > backlight_before and want_down:
                    wrong_way = True
                if backlight_after < backlight_before and not want_down:
                    wrong_way = True
            # Settings table (slow/cached on this ROM — last resort).
            if state_before is not None and state_after is not None and state_after != state_before:
                moved = True
                if state_after > state_before and want_down:
                    wrong_way = True
                if state_after < state_before and not want_down:
                    wrong_way = True

            if wrong_way:
                print(f"FAIL: asked to LOWER brightness, but a value INCREASED "
                      f"(setting {state_before} → {state_after}; slider {slider_text_before} → {slider_text_after}; "
                      f"backlight {backlight_before} → {backlight_after}). Wrong direction/slider.")
                return 1
            if not moved:
                print("FAIL: brightness task ran, but the setting, the slider value AND the "
                      "backlight are all unchanged after a 2.5s settle. The drag was not "
                      "delivered (half-open shade preview / auto-brightness / wrong control).")
                return 1
        else:
            time.sleep(1.2)
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

        print("PASS: full pipeline (screenshot → index → VLM → execute → effect) works.")
        return 0
    finally:
        if brightness_task:
            if was_auto:
                print("      [state] restoring adaptive brightness (was ON) — the screen may "
                      "re-adjust right after this program exits; THAT change is the light "
                      "sensor re-engaging, not the slider result.")
            _auto_brightness_on(device, auto_ns)


if __name__ == "__main__":
    sys.exit(main())