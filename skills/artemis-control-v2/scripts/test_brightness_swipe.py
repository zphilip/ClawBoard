#!/usr/bin/env python3
"""Swipe-operation integration test: **adjust the screen brightness**.

The cleanest end-to-end way to exercise the swipe primitive is the brightness
slider in Quick Settings:

    1. swipe DOWN from the very TOP edge   -> notification shade
    2. swipe DOWN again                      -> full Quick Settings (brightness bar)
    3. locate the brightness SeekBar in the UI dump
    4. drag it to a target fraction using _slider_gesture (the exact production
       geometry, past the boundary, inside the edge-safe rect)
    5. verify with `settings get system screen_brightness`

This test catches the two failure classes we kept hitting in the field:

    * a swipe whose START lands in the gesture-navigation strip is swallowed as a
      BACK (left/right edges), and
    * a slider gesture that stops dead on the target (or a tap) is not delivered
      because touch slop eats the first pixels.

IMPORTANT single exception: opening the notification shade is the ONE gesture
that MUST start at the very TOP edge (y ≈ 0). `_edge_safe_rect()` deliberately
excludes the top 3% for ordinary gestures, so this script uses a raw
`input swipe` for steps 1-2 and the safe geometry only for the slider itself.

Usage (on the device host):
    python3 scripts/test_brightness_swipe.py [--device SERIAL] [--target 0.35]
                                             [--no-execute] [--geometry-only]

Exit code 0 == PASS, 1 == FAIL, 2 == SKIP (no device).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import artemis_agent  # noqa: E402
from artemis_agent import (  # noqa: E402
    _adb,
    _clip_point,
    _coerce_coord,
    _degenerate_gesture_reason,
    _edge_safe_rect,
    _is_slider_element,
    _slider_gesture,
)


# ─── tiny element model + uiautomator parser ────────────────────────────────
class _El:
    def __init__(self, index: int, cls: str, bounds: list[int], scrollable: bool = False):
        self.index = index
        self.class_name = cls
        self.bounds = bounds
        self.scrollable = scrollable
        self.display_text = cls
        self.resource_id = ""
        self.text = ""


_BOUNDS_RE = re.compile(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"')
_CLASS_RE = re.compile(r'class="([^"]+)"')
_RID_RE = re.compile(r'resource-id="([^"]*)"')
_TEXT_RE = re.compile(r'text="([^"]*)"')


def _parse_elements(xml: str) -> list[_El]:
    """Pull every node with a bounds attribute out of a uiautomator dump."""
    out: list[_El] = []
    idx = 0
    for chunk in re.split(r"(?=<node)", xml):
        bm = _BOUNDS_RE.search(chunk)
        if not bm:
            continue
        cm = _CLASS_RE.search(chunk)
        rm = _RID_RE.search(chunk)
        tm = _TEXT_RE.search(chunk)
        cls = cm.group(1) if cm else ""
        el = _El(idx, cls, [int(bm.group(i)) for i in range(1, 5)])
        el.resource_id = rm.group(1) if rm else ""
        el.text = tm.group(1) if tm else ""
        out.append(el)
        idx += 1
    return out


def _find_brightness_slider(elements: list[_El]) -> _El | None:
    """The brightness SeekBar specifically, then any slider, then the widest
    bar-like element. Adaptive-brightness ROMs often render extra progress bars
    in the shade (media, short video), so a bare \"largest SeekBar\" can pick the
    wrong control — demand a brightness-ish resource-id or text when present."""
    sliders = [e for e in elements if _is_slider_element(e)]

    # 1. explicit brightness identity
    for e in sliders:
        hint = (e.resource_id + " " + e.text).lower()
        if "brightness" in hint or "亮度" in hint or "adaptive" in hint:
            return e

    # 2. any slider that is nearly full-width and in the shade region
    if sliders:
        return max(
            sliders,
            key=lambda e: (e.bounds[2] - e.bounds[0]) * (e.bounds[3] - e.bounds[1]),
        )

    bars = [e for e in elements if (e.bounds[3] - e.bounds[1]) <= 80]
    if not bars:
        return None
    return max(bars, key=lambda e: e.bounds[2] - e.bounds[0])


def _wm_size(device: str | None) -> tuple[int, int] | None:
    code, out, _ = _adb(["shell", "wm", "size"], device=device)
    if code != 0:
        return None
    m = re.search(r"(\d{3,5})x(\d{3,5})", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _swipe(device: str | None, x1, y1, x2, y2, ms=400) -> bool:
    code, out, err = _adb(
        ["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(ms)],
        device=device,
    )
    return code == 0


def _brightness_now(device: str | None) -> int | None:
    for key in ("system", "global"):
        code, out, _ = _adb(
            ["shell", "settings", "get", key, "screen_brightness"], device=device
        )
        txt = out.strip()
        if code == 0 and txt and txt.lstrip("-").isdigit():
            return int(txt)
    return None


def _mode_now(device: str | None) -> tuple[str | None, str | None]:
    """Return (namespace, value) of the screen_brightness_mode setting."""
    for ns in ("system", "global"):
        code, out, _ = _adb(
            ["shell", "settings", "get", ns, "screen_brightness_mode"], device=device
        )
        txt = out.strip()
        if code == 0 and txt:
            return ns, txt
    return None, None


def _put_mode(device: str | None, value: str) -> None:
    """Set screen_brightness_mode in BOTH namespaces (the location varies by ROM)."""
    for ns in ("system", "global"):
        _adb(
            ["shell", "settings", "put", ns, "screen_brightness_mode", value],
            device=device,
        )


def _auto_brightness_off(device: str | None) -> tuple[bool, str | None]:
    """Disable adaptive brightness for the test. Returns (was_on, namespace).

    With adaptive brightness ON, dragging the manual slider only nudges an
    offset that the light sensor immediately overwrites, so
    ``screen_brightness`` never moves and the drag looks like a no-op — the
    exact failure this test reports. Disable it, then restore afterwards.
    """
    ns, val = _mode_now(device)
    was_on = bool(val and val.strip() == "1")
    if was_on:
        _put_mode(device, "0")
        time.sleep(0.5)
    return was_on, ns


def _auto_brightness_on(device: str | None, ns: str | None) -> None:
    """Best-effort restore of adaptive brightness."""
    if not ns:
        return
    _adb(
        ["shell", "settings", "put", ns, "screen_brightness_mode", "1"],
        device=device,
    )


def _geometry_only(target: float) -> int:
    """Run the slider geometry against a synthetic full-width SeekBar (no device).

    This is the CI-able half of the test: it proves _slider_gesture produces a
    travel that starts inside the edge-safe rect and ends PAST the target
    (past the boundary for 0.0 / 1.0).
    """
    w, h = 1440, 3120
    # Reproduce production: the resolver reads _DEVICE_RESOLUTION for the
    # edge-safe rect, so the geometry is identical to a live run.
    artemis_agent._DEVICE_RESOLUTION = (w, h)
    seek = _El(0, "android.widget.SeekBar", [100, 802, 1340, 856])
    g = _slider_gesture([seek], 0, target)
    if g is None:
        print("FAIL: _slider_gesture returned None")
        return 1

    (sx, sy), (tx, ty) = g["coordinate1"], g["coordinate2"]
    x0, y0, x1, y1 = _edge_safe_rect()
    ok_start = x0 <= sx <= x1

    # The end must travel PAST the target. For the extremes it lands past the
    # track boundary (which may exceed the safe rect — only the START is
    # edge-sensitive); for middle values it lands beyond the target by >=
    # the overshoot.
    track_left = max(100, x0)
    track_right = min(1340, x1)  # track clipped to the live edge-safe rect
    width = track_right - track_left
    exact = track_left + int(width * target)
    if target <= 0.0:
        ok_end = tx < track_left          # past the boundary (clamped to >= 0)
    elif target >= 1.0:
        ok_end = tx > track_right
    else:
        ok_end = (tx < exact - 20) if target <= 0.5 else (tx > exact + 20)  # noqa: SIM

    travel = abs(tx - sx)
    print(f"[geometry] target={target:.2f}  drag ({sx},{sy}) -> ({tx},{ty})  travel={travel}px")
    print(f"            start in safe rect: {ok_start}   end past target/boundary: {ok_end}")

    if not ok_start or not ok_end:
        print("FAIL: geometry violated the slider contract")
        return 1
    print("PASS: slider geometry correct")
    return 0


def _swipe_action_geometry(w: int, h: int) -> int:
    """Verify the AGENT's ``swipe`` ACTION coordinate path (the one the fixed
    slider geometry does NOT exercise): VLM 0-1000 coords -> ``_coerce_coord``
    -> ``_clip_point`` -> non-degenerate, which is exactly what _decide_action
    / the plan-guided operator run before ``adb shell input swipe``."""
    artemis_agent._DEVICE_RESOLUTION = (w, h)
    x0, y0, x1, y1 = _edge_safe_rect()
    problems: list[str] = []

    # 1. canonical 0-1000 swipe maps to device px and the start lands inside
    #    the edge-safe zone (so the OS won't steal it as a BACK gesture).
    sx, sy = _coerce_coord(500, 0), _coerce_coord(300, 1)
    ex, ey = _coerce_coord(500, 0), _coerce_coord(700, 1)
    csx, csy = _clip_point(sx, sy)
    cex, cey = _clip_point(ex, ey)
    start_ok = x0 <= csx <= x1 and y0 <= csy <= y1

    if _degenerate_gesture_reason(
        "swipe", {"coordinate1": [csx, csy], "coordinate2": [cex, cey]}
    ):
        problems.append("canonical swipe was flagged as a degenerate/no-op gesture")

    # 2. a VLM swipe STARTING in the left edge-back strip must be clipped into
    #    the safe zone, not executed as-is (otherwise the app sees a tap+back).
    lx, ly = _coerce_coord(0, 0), _coerce_coord(500, 1)
    clx, cly = _clip_point(lx, ly)
    left_clipped = clx >= x0
    if not left_clipped:
        problems.append(f"left-edge swipe start ({clx},{cly}) not clipped into safe zone (x0={x0})")

    # 3. a value ABOVE the 0-1000 space but within the device (e.g. 1200 on a
    #    1440 wide screen) must survive _coerce_coord un-scaled — that branch is
    #    what stops a device-pixel swipe from being `n/1000*dev`-scaled so both
    #    endpoints clamp onto one edge and the swipe collapses into zero travel.
    #    Values <=1000 are the documented normalized space (ambiguous readings
    #    resolve to normalized), so only >1000..<=dev is treated as pixels.
    if w == 1440:
        raw = _coerce_coord(1200, 0)
        clamped = _coerce_coord(9999, 0)
        if raw != 1200:
            problems.append(f"device-pixel coord 1200 (w={w}) should stay 1200, got {raw}")
        if clamped != w - 1:
            problems.append(f"out-of-range coord 9999 should clamp to {w - 1}, got {clamped}")

    print(
        f"[swipe-action] VLM(500,300)->(500,700) => DEV ({csx},{csy})->({cex},{cey})"
        f"  start-in-safe:{start_ok}"
    )
    print(f"              left-edge start VLM(0,500)->({clx},{cly})  clipped-into-safe:{left_clipped}")

    for p in problems:
        print(f"FAIL: {p}")
    if problems:
        return 1
    print("PASS: swipe-action coordinate path correct (0-1000 + edge-safe + no-degenerate)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--device", "-s", default=None, help="adb serial (default: first device)")
    ap.add_argument("--target", type=float, default=0.35, help="brightness fraction 0..1")
    ap.add_argument("--no-execute", action="store_true", help="print the plan, do not touch the device")
    ap.add_argument("--geometry-only", action="store_true", help="run geometry checks only (no device)")
    ap.add_argument("--swipe-action", action="store_true",
                    help="verify the agent's swipe ACTION coordinate path (0-1000 + edge-safe)")
    args = ap.parse_args()

    target = max(0.0, min(1.0, args.target))
    if args.swipe_action:
        return _swipe_action_geometry(1440, 3120)
    if args.geometry_only:
        return _geometry_only(target)

    device = args.device
    if device is None:
        code, out, _ = _adb(["devices"])
        lines = [ln for ln in out.splitlines()[1:] if ln.strip() and "\tdevice" in ln]
        if not lines:
            print("SKIP: no attached Android device (`adb devices` is empty).")
            return 2
        device = lines[0].split("\t")[0]
    print(f"[device] {device}")

    size = _wm_size(device)
    if not size:
        print("FAIL: could not read `wm size`")
        return 1
    w, h = size
    # Make the production edge-safe geometry see the REAL screen — otherwise it
    # falls back to the unbounded (0,0,10^6,10^6) sentinel and the printed rect
    # (and any start-point clipping) is meaningless.
    artemis_agent._DEVICE_RESOLUTION = (w, h)
    sx0, sy0, sx1, sy1 = _edge_safe_rect()
    print(f"[screen] {w}x{h}   edge-safe rect x:[{sx0},{sx1}] y:[{sy0},{sy1}]")

    before = _brightness_now(device)
    print(f"[brightness] before = {before}")

    # Adaptive brightness must be OFF or the sensor instantly overwrites any
    # manual drag — the most common reason for a "delta = 0" here.
    was_auto, auto_ns = _auto_brightness_off(device)
    if was_auto:
        print("[auto-brightness] was ON — disabled for the test (restored at exit)")
        before = _brightness_now(device)
        print(f"[brightness] re-baseline (auto off) = {before}")

    try:
        # Step 1: open the notification shade. This swipe MUST start at the top
        # edge (y ≈ 1%), outside the safe rect — by design, for this one gesture.
        print("[step1] swipe down from the top edge (notification shade)")
        if not args.no_execute:
            _swipe(device, w // 2, max(1, int(h * 0.01)), w // 2, int(h * 0.55), ms=350)
            time.sleep(1.0)

        # Step 2: expand to full Quick Settings so the brightness bar is visible.
        print("[step2] swipe down again (full Quick Settings)")
        if not args.no_execute:
            _swipe(device, w // 2, int(h * 0.30), w // 2, int(h * 0.75), ms=350)
            time.sleep(1.0)

        # Step 3: dump the UI and find the brightness slider.
        print("[step3] locating brightness SeekBar")
        if args.no_execute:
            print("        (dry run — no dump)")
            return 0
        code, out, err = _adb(["shell", "uiautomator", "dump", "/sdcard/win.xml"], device=device)
        if code != 0:
            code, out, err = _adb(["shell", "uiautomator", "dump"], device=device)
        code, xml, _ = _adb(["shell", "cat", "/sdcard/win.xml"], device=device)
        if code != 0 or not xml.strip():
            print("FAIL: uiautomator dump empty (is the shade fully expanded?)")
            return 1

        elements = _parse_elements(xml)
        slider = _find_brightness_slider(elements)
        if slider is None:
            print("FAIL: no SeekBar/bar element found in the dump")
            return 1

        b = slider.bounds
        rid = f"  resource-id={slider.resource_id!r}" if slider.resource_id else ""
        print(f"[slider] element #{slider.index} {slider.class_name} bounds={b}{rid}")

        # Step 4: compute the drag with the production geometry and execute it.
        gesture = _slider_gesture(elements, slider.index, target)
        if gesture is None:
            print("FAIL: _slider_gesture returned None (track too narrow inside safe rect?)")
            return 1
        (ax, ay), (bx, by) = gesture["coordinate1"], gesture["coordinate2"]
        print(f"[step4] drag {gesture['coordinate1']} -> {gesture['coordinate2']} "
              f"({gesture['duration']}ms)   target={target:.2f}")
        _swipe(device, ax, ay, bx, by, ms=gesture["duration"])
        time.sleep(1.0)

        # Step 5: verify.
        after = _brightness_now(device)
        print(f"[brightness] after = {after}")
        if before is not None and after is not None:
            moved = abs(after - before)
            print(f"[verify] brightness delta = {moved}")
            if moved == 0:
                print("FAIL: brightness unchanged (0x delta). Causes, in order: "
                      "adaptive brightness still on; shade not fully expanded; "
                      "wrong slider picked. Re-run with `--target 0.0` to drag "
                      "to the extreme and rule out a too-small target delta.")
                return 1
        print("PASS: swipe + slider drag executed")
        return 0
    finally:
        if was_auto:
            _auto_brightness_on(device, auto_ns)
            print("[auto-brightness] restored (ON)")


if __name__ == "__main__":
    sys.exit(main())