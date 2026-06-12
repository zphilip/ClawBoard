#!/usr/bin/env python3
"""
gpio-button-test.py
===================
Interactive GPIO button tester for Radxa / Linux SBC (uses periphery).

Usage
-----
# Test a specific chip + line:
    sudo python3 gpio-button-test.py --chip /dev/gpiochip0 --line 97

# Test the auto-detected default button pin for this board:
    sudo python3 gpio-button-test.py

# Scan ALL free lines and auto-detect which one you're pressing:
    sudo python3 gpio-button-test.py --scan-press

The --scan-press mode opens every free GPIO line simultaneously and
reports which one changes state when you press the button.  Useful when
you don't yet know the correct chip/line for a physical pin.

Environment variable overrides (same as the wifi-connect launcher):
    RADXA_BTN_CHIP=/dev/gpiochip0  RADXA_BTN_LINE=97
"""

import os
import sys
import time
import argparse

from board_config import detect_board, get_button_pin, open_button, scan_free_lines

# ── Auto-detected defaults (overridable via env / CLI) ─────────────────────────
_board = detect_board()
_default_chip, _default_line, _default_label = get_button_pin(_board)


# ── Single-line test ──────────────────────────────────────────────────────────

def test_single(chip, line):
    """Monitor one GPIO line and report press / release events."""
    print(f"\nTesting {chip} line {line}")
    print("Connect button between this pin and GND.  Ctrl-C to quit.\n")

    try:
        gpio = open_button(chip, line)
    except Exception as exc:
        print(f"ERROR: cannot open {chip} line {line}: {exc}")
        print(f"\nDetected board: {_board}")
        print(f"Default pin for this board: {_default_label} → {_default_chip} line {_default_line}")
        print(f"\nTo scan all free lines:")
        print(f"    sudo python3 {os.path.basename(__file__)} --scan-press")
        sys.exit(1)

    initial = gpio.read()
    active_high = not initial   # pressed = opposite of resting state
    polarity = 'active-HIGH (resting=LOW)' if active_high else 'active-LOW (resting=HIGH)'
    print(f"Initial state: {'HIGH' if initial else 'LOW'} — {polarity}\n")

    if not initial:
        print("WARNING: Pin reads LOW at rest. If your button connects to GND,")
        print("         no transition will be detected.  Check wiring or choose")
        print("         a pin that reads HIGH (use --scan-press to find one).\n")

    def is_pressed():
        return gpio.read() if active_high else not gpio.read()

    prev       = initial
    press_time = None

    # Show a live reading ticker so you can confirm the line is responding
    last_tick = time.time()

    try:
        while True:
            val = gpio.read()

            # Periodic heartbeat: print current raw value every 2 s so user
            # can confirm the script is alive and see the raw level
            now = time.time()
            if now - last_tick >= 2.0:
                print(f"  [live] {chip} line {line} = {'HIGH' if val else 'LOW '} "
                      f"({'pressed' if is_pressed() else 'released'})")
                last_tick = now

            if val != prev:
                if is_pressed():
                    press_time = now
                    print(f"  ▼ PRESSED  at {_ts()}  (raw={'HIGH' if val else 'LOW'})")
                else:
                    if press_time is not None:
                        duration = now - press_time
                        kind = "LONG press  ✓" if duration >= 2.0 else "short press"
                        print(f"  ▲ RELEASED at {_ts()}  →  {kind} ({duration:.2f}s)")
                        press_time = None
                prev = val
            time.sleep(0.02)
    except KeyboardInterrupt:
        print("\nDone.")
    finally:
        gpio.close()


def _ts():
    return time.strftime('%H:%M:%S')


# ── Scan-press mode: auto-detect which line changes ───────────────────────────

def scan_press():
    """
    Open every free GPIO line, record baseline, then watch for any change.
    Press the button once — whichever line flips is your button line.
    """
    print("Collecting all free GPIO lines…")
    candidates = []
    suspicious = []   # lines that are free but unlikely to work

    for chip_path, offset, lname, consumer, flags in scan_free_lines():
        try:
            g = open_button(chip_path, offset)
            baseline = g.read()
            candidates.append((chip_path, offset, lname, g, baseline))

            # Flag lines that rest LOW — they won't show a HIGH→LOW transition
            if not baseline:
                from board_config import _GPIOLINE_FLAG_PULL_DOWN
                if flags & _GPIOLINE_FLAG_PULL_DOWN:
                    suspicious.append((chip_path, offset, lname))
        except Exception:
            pass

    if not candidates:
        print("No free GPIO lines found.")
        sys.exit(1)

    print(f"Monitoring {len(candidates)} free lines.")
    print("Press the button NOW (and release) — the matching line will be reported.\n")
    print(f"  {'CHIP':<18} {'LINE':>4}  {'NAME':<28} BASELINE  NOTE")
    print(f"  {'─'*18} {'─'*4}  {'─'*28} {'─'*8}  {'─'*4}")
    for chip_path, offset, lname, g, baseline in candidates:
        state = 'HIGH' if baseline else 'LOW '
        note = ''
        if not baseline:
            note = '⚠ rests LOW — may not detect press'
        print(f"  {chip_path:<18} {offset:4}  {lname:<28} {state}      {note}")
    print()

    if suspicious:
        print(f"⚠  {len(suspicious)} line(s) rest LOW (likely hardware pull-down).")
        print(f"   These can't detect an active-LOW press. If your button is wired")
        print(f"   to one of these, consider moving it to a pin that reads HIGH.\n")

    try:
        deadline = time.time() + 30   # give user 30 seconds
        while time.time() < deadline:
            for chip_path, offset, lname, g, baseline in candidates:
                try:
                    val = g.read()
                    if val != baseline:
                        print(f"\n*** LINE CHANGED ***")
                        print(f"  Chip : {chip_path}")
                        print(f"  Line : {offset}")
                        print(f"  Name : {lname or '(unnamed)'}")
                        print(f"  State: {'HIGH' if val else 'LOW'} (was {'HIGH' if baseline else 'LOW'})")
                        policy = 'active-LOW (resting=HIGH → pressed=LOW)' if baseline else \
                                 'active-HIGH (resting=LOW → pressed=HIGH)'
                        print(f"  Polarity: {policy}")
                        print(f"\nUse this line:")
                        print(f"  RADXA_BTN_CHIP={chip_path} RADXA_BTN_LINE={offset} \\")
                        print(f"  sudo python3 wifi-connect-gpio-launch-radxa.py")
                        print(f"\nOr test it properly:")
                        print(f"  sudo python3 gpio-button-test.py --chip {chip_path} --line {offset}")
                        return
                except Exception:
                    pass
            time.sleep(0.02)
        print("Timeout — no line changed within 30 s.")
        if suspicious:
            print("Tip: your button may be on one of the ⚠ lines above.")
            print("     Try wiring it to a pin that reads HIGH instead.")
    except KeyboardInterrupt:
        print("\nCancelled.")
    finally:
        for *_, g, _ in candidates:
            try:
                g.close()
            except Exception:
                pass


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='GPIO button tester for Radxa / Linux SBC',
        epilog=f'Detected board: {_board}. '
               f'Default pin: {_default_label} → {_default_chip} line {_default_line}.')
    parser.add_argument('--chip',
                        default=os.environ.get('RADXA_BTN_CHIP', _default_chip),
                        help=f'GPIO chip device (default: {_default_chip})')
    parser.add_argument('--line', type=int,
                        default=int(os.environ.get('RADXA_BTN_LINE', str(_default_line))),
                        help=f'GPIO line offset (default: {_default_line} = {_default_label})')
    parser.add_argument('--scan-press', action='store_true',
                        help='Auto-detect button line by watching all free lines')
    args = parser.parse_args()

    if args.scan_press:
        scan_press()
    else:
        test_single(args.chip, args.line)


if __name__ == '__main__':
    main()
