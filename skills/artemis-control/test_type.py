#!/usr/bin/env python3
"""Test script for text input methods on Android device.

Tests the clipboard paste approach used by artemis_agent.py.
Usage:
    python3 test_type.py --text "GAI哥" --device <serial>
    python3 test_type.py --text "Hello World" --method clipboard
    python3 test_type.py --text "测试中文" --method input_text

Methods:
    clipboard   — base64 → temp file → cmd clipboard set → PASTE (keyevent 279)
    input_text  — adb shell input text (single-quote escaping)
    both        — try clipboard first, fall back to input_text
"""

import argparse
import base64
import subprocess
import sys
import time


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


def clipboard_paste(device: str, text: str) -> bool:
    """Type text via clipboard paste (no ADB Keyboard APK required)."""
    print(f"[CLIPBOARD] Text: {text!r} ({len(text)} chars)")

    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    clip_b64 = "/sdcard/_test_clip_b64"
    clip_txt = "/sdcard/_test_clip_txt"

    # Step 1: Write base64 to device temp file
    print(f"[CLIPBOARD] Writing base64 to {clip_b64}...")
    rc, _, err = adb(["shell", "printf", "%s", b64, ">", clip_b64], device=device)
    if rc != 0:
        print(f"[CLIPBOARD] ❌ Failed to write temp file: {err}")
        return False
    print(f"[CLIPBOARD] ✅ Temp file written ({len(b64)} bytes)")

    # Step 2: Decode → set clipboard → PASTE → cleanup
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
    # Cleanup
    adb(["shell", "rm", "-f", clip_b64, clip_txt], device=device)
    return False


def input_text_method(device: str, text: str) -> bool:
    """Type text via adb shell input text."""
    print(f"[INPUT_TEXT] Text: {text!r} ({len(text)} chars)")

    # Single-quote escaping
    escaped = text.replace("'", "\\'")
    rc, out, err = adb(["shell", "input", "text", f"'{escaped}'"], device=device)

    if rc == 0:
        print(f"[INPUT_TEXT] ✅ input text succeeded")
        return True

    print(f"[INPUT_TEXT] ❌ Failed: rc={rc} err={err}")
    return False


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
    parser.add_argument("--method", choices=["clipboard", "input_text", "both"],
                        default="both", help="Text input method")
    parser.add_argument("--verify", action="store_true",
                        help="Verify text appeared on screen (dumps UI XML)")
    parser.add_argument("--tap-first", type=str, default=None,
                        help="Tap at x,y before typing (e.g. '400,200')")
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

    success = False

    if args.method in ("clipboard", "both"):
        success = clipboard_paste(args.device, args.text)

    if not success and args.method in ("input_text", "both"):
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