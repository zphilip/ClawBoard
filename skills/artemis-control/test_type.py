#!/usr/bin/env python3
"""Test script for text input methods on Android device.

Tests the tiered approach used by artemis_agent.py:
  Tier 0 (primary): uiautomator2 set_text() — AccessibilityService ACTION_SET_TEXT
  Tier 1: Clipboard paste — base64 → cmd clipboard set → PASTE keyevent
  Tier 2: adb shell input text — single-quote escaping

Usage:
    python3 test_type.py --text "GAI哥" --device <serial>
    python3 test_type.py --text "Hello World" --method u2
    python3 test_type.py --text "测试中文" --method all
"""

import argparse
import base64
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET


def adb(args: list[str], device: str | None = None, timeout: int = 10) -> tuple[int, str, str]:
    """Run an ADB command."""
    cmd = ["adb"]
    if device:
        cmd += ["-s", device]
    cmd += args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError:
        return -1, "", "adb not found"


# ── uiautomator2 helpers (Tier 0 — primary) ─────────────────────────────

def _u2_connect(device: str | None = None):
    """Connect to device via uiautomator2, backward-compatibly."""
    import uiautomator2 as u2

    try:
        return (
            u2.connect(device, init_atx_agent=False)
            if device
            else u2.connect(init_atx_agent=False)
        )
    except TypeError:
        return u2.connect(device) if device else u2.connect()


def _u2_disable_fast_ime(d) -> None:
    """Disable ATX FastInputIME so ACTION_SET_TEXT works without IME."""
    try:
        d.set_input_ime(False)
    except AttributeError:
        try:
            d.set_fastinput_ime(False)
        except AttributeError:
            pass


def _u2_find_editable(ui_xml: str) -> dict | None:
    """Find the best editable element in a UI XML dump."""
    if not ui_xml:
        return None
    try:
        root = ET.fromstring(ui_xml)
    except ET.ParseError:
        return None

    candidates: list[dict] = []

    for node in root.iter("node"):
        cls = (node.attrib.get("class", "") or "").lower()
        is_editable = any(
            kw in cls
            for kw in ("edittext", "edit", "input", "searchview", "search")
        )
        if not is_editable:
            continue
        focusable = node.attrib.get("focusable", "false") == "true"
        focused = node.attrib.get("focused", "false") == "true"
        bounds_str = node.attrib.get("bounds", "")
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
        if not m:
            continue
        x1, y1, x2, y2 = int(m[1]), int(m[2]), int(m[3]), int(m[4])
        if x2 <= x1 or y2 <= y1:
            continue

        candidates.append({
            "center": ((x1 + x2) // 2, (y1 + y2) // 2),
            "class": node.attrib.get("class", ""),
            "text": node.attrib.get("text", ""),
            "hint": node.attrib.get("hint", ""),
            "focused": focused,
            "focusable": focusable,
        })

    if not candidates:
        return None
    # Prefer already-focused
    for c in candidates:
        if c["focused"]:
            return c
    # Then search-related hints
    for c in candidates:
        if any(kw in (c.get("hint", "") or "").lower()
               for kw in ("search", "搜索", "查找")):
            return c
    # First available
    return candidates[0]


def u2_set_text(device: str, text: str) -> bool:
    """Tier 0: uiautomator2 set_text via AccessibilityService ACTION_SET_TEXT."""
    print(f"[U2] Text: {text!r} ({len(text)} chars)")

    try:
        import uiautomator2 as u2

        d = _u2_connect(device)
        _u2_disable_fast_ime(d)

        # Try focused element first
        try:
            d(focused=True).set_text(text)
            print("[U2] ✅ set_text on focused element succeeded")
            return True
        except Exception as e1:
            err_str = str(e1)
            is_focus_error = "focused=True" in err_str or "focused" in err_str.lower()
            if not is_focus_error:
                raise
            print(f"[U2] No focused element: {err_str}")


        # Auto-focus: dump UI, find editable, tap, retry
        dump_path = "/sdcard/_test_u2_ui.xml"
        rc, _, _ = adb(["shell", "uiautomator", "dump", dump_path], device=device)
        if rc != 0:
            print("[U2] ❌ UI dump failed")
            return False
        time.sleep(0.5)

        rc, xml_str, _ = adb(["shell", "cat", dump_path], device=device)
        adb(["shell", "rm", "-f", dump_path], device=device)
        if rc != 0 or not xml_str:
            print("[U2] ❌ No UI XML content")
            return False

        target = _u2_find_editable(xml_str)
        if not target:
            print("[U2] ❌ No editable element found in UI")
            return False

        bx, by = target["center"]
        print(
            f"[U2] Auto-focus: tapping {target['class'][:40]} "
            f"at ({bx},{by}) text={target.get('text','')[:30]}"
        )
        d.click(bx, by)
        time.sleep(0.5)

        d(focused=True).set_text(text)
        print(f"[U2] ✅ auto-focus + set_text succeeded")
        return True

    except ImportError:
        print("[U2] ⚠️ uiautomator2 not installed — skipping")
        return False
    except Exception as e:
        print(f"[U2] ❌ Failed: {e}")
        return False


# ── Clipboarcd paste (Tier 1) ────────────────────────────────────────────

def clipboard_paste(device: str, text: str) -> bool:
    """Type text via clipboard paste (no ADB Keyboard APK required)."""
    print(f"[CLIPBOARD] Text: {text!r} ({len(text)} chars)")

    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    clip_b64 = "/sdcard/_test_clip_b64"
    clip_txt = "/sdcard/_test_clip_txt"

    print(f"[CLIPBOARD] Writing base64 to {clip_b64}...")
    rc, _, err = adb(["shell", "printf", "%s", b64, ">", clip_b64], device=device)
    if rc != 0:
        print(f"[CLIPBOARD] ❌ Failed to write temp file: {err}")
        return False
    print(f"[CLIPBOARD] ✅ Temp file written ({len(b64)} bytes)")

    print("[CLIPBOARD] Decoding → setting clipboard → PASTE...")
    rc, out, err = adb([
        "shell",
        f"base64 -d {clip_b64} > {clip_txt} && "
        f"cmd clipboard set \"$(cat {clip_txt})\" && "
        f"input keyevent 279 && "
        f"rm -f {clip_b64} {clip_txt}"
    ], device=device, timeout=10)

    if rc == 0:
        print(f"[CLIPBOARD] ✅ PASTE keyevent sent successfully")
        return True

    print(f"[CLIPBOARD] ❌ Failed: rc={rc} err={err}")
    adb(["shell", "rm", "-f", clip_b64, clip_txt], device=device)
    return False


def input_text_method(device: str, text: str) -> bool:
    """Type text via adb shell input text."""
    print(f"[INPUT_TEXT] Text: {text!r} ({len(text)} chars)")

    escaped = text.replace("'", "\\'")
    rc, out, err = adb(["shell", "input", "text", f"'{escaped}'"], device=device)

    if rc == 0:
        print(f"[INPUT_TEXT] ✅ input text succeeded")
        return True

    print(f"[INPUT_TEXT] ❌ Failed: rc={rc} err={err}")
    return False


# ── Auto-focus (for clipboard/input_text — requires IME focus) ───────────

def auto_focus_input(device: str) -> bool:
    """Dump UI XML and tap the first EditText/SearchView found."""
    dump_path = "/sdcard/_test_ui.xml"
    rc, _, _ = adb(["shell", "uiautomator", "dump", dump_path], device=device)
    if rc != 0:
        return False
    time.sleep(0.5)
    rc, xml_str, _ = adb(["shell", "cat", dump_path], device=device)
    adb(["shell", "rm", "-f", dump_path], device=device)
    if rc != 0 or not xml_str:
        return False

    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return False

    target = _u2_find_editable(xml_str)
    if target:
        cx, cy = target["center"]
        print(f"[AUTO-FOCUS] Found {target['class']} at ({cx},{cy}) — tapping to focus")
        adb(["shell", "input", "tap", str(cx), str(cy)], device=device)
        time.sleep(0.3)
        return True

    print("[AUTO-FOCUS] No editable field found in UI dump")
    return False


# ── Verification ──────────────────────────────────────────────────────────

def dump_ui(device: str) -> str | None:
    """Dump UI hierarchy and return the XML as a string."""
    dump_path = "/sdcard/_test_ui.xml"
    rc, _, err = adb(["shell", "uiautomator", "dump", dump_path], device=device)
    if rc != 0:
        print(f"[UI] Failed to dump UI: {err}")
        return None
    time.sleep(1)
    rc, xml, _ = adb(["shell", "cat", dump_path], device=device)
    adb(["shell", "rm", "-f", dump_path], device=device)
    return xml if rc == 0 else None


def check_text_on_screen(device: str, text: str) -> bool:
    """Check if the typed text appears in the UI hierarchy."""
    xml = dump_ui(device)
    if not xml:
        return False
    if text in xml:
        print(f"[VERIFY] ✅ Text '{text}' found in UI dump")
        return True
    print(f"[VERIFY] ❌ Text '{text}' NOT found in UI dump")
    return False


def main():
    parser = argparse.ArgumentParser(description="Test text input on Android device")
    parser.add_argument("--text", required=True, help="Text to type")
    parser.add_argument("--device", default=None, help="ADB device serial")
    parser.add_argument("--adb", default="adb", help="Path to adb binary")
    parser.add_argument("--method", choices=["u2", "clipboard", "input_text", "all"],
                        default="all", help="Text input method")
    parser.add_argument("--verify", action="store_true",
                        help="Verify text appeared on screen (dumps UI XML)")
    parser.add_argument("--tap-first", type=str, default=None,
                        help="Tap at x,y before typing (e.g. '400,200')")
    parser.add_argument("--auto-focus", action="store_true",
                        help="Auto-find and tap an EditText/SearchView before typing")
    args = parser.parse_args()

    print("=" * 60)
    print(f"Text Input Test — method={args.method} device={args.device or 'auto'}")
    print(f"Text: {args.text!r} ({len(args.text)} chars)")
    print("=" * 60)

    # Optional: tap first to focus a field
    if args.tap_first:
        x, y = args.tap_first.split(",")
        print(f"[PRE] Tapping at ({x},{y}) to focus input field...")
        rc, _, _ = adb(["shell", "input", "tap", x, y], device=args.device)
        time.sleep(0.5)
        print(f"[PRE] Tap {'OK' if rc == 0 else 'FAILED'}")

    if args.auto_focus:
        print("[PRE] Auto-focus: searching for EditText/SearchView via uiautomator dump...")
        if auto_focus_input(args.device):
            print("[PRE] ✅ Input field focused")
        else:
            print("[PRE] ⚠️  No editable field found on screen")

    success = False

    # Tier 0: uiautomator2 (primary)
    if args.method in ("u2", "all"):
        success = u2_set_text(args.device, args.text)

    # Tier 1: Clipboard paste
    if not success and args.method in ("clipboard", "all"):
        success = clipboard_paste(args.device, args.text)

    # Tier 2: input text
    if not success and args.method in ("input_text", "all"):
        success = input_text_method(args.device, args.text)

    print("-" * 60)
    if success:
        print("✅ Text input succeeded")
    else:
        print("❌ All text input methods failed")
        sys.exit(1)

    # Optional verification
    if args.verify:
        time.sleep(0.5)
        if check_text_on_screen(args.device, args.text):
            print("✅ Text verified on screen")
        else:
            print("⚠️  Text may not have appeared on screen (or UI dump missed it)")
            sys.exit(2)

    print("=" * 60)


if __name__ == "__main__":
    main()