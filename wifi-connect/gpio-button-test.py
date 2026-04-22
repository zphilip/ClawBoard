#!/usr/bin/env python3
"""
gpio-button-test.py
===================
Interactive GPIO button tester for Radxa / Linux SBC (uses periphery).

Usage
-----
# Test a specific chip + line:
    sudo python3 gpio-button-test.py --chip /dev/gpiochip0 --line 97

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
import glob
import fcntl
import struct
import argparse

# ── ioctl helpers (same as wifi-connect-gpio-launch-radxa.py) ────────────────
_GPIO_GET_CHIPINFO_IOCTL = 0x8044B401   # _IOR(0xB4, 0x01, 68)  gpiochip_info
_GPIO_GET_LINEINFO_IOCTL = 0xC048B402   # _IOWR(0xB4, 0x02, 72) gpioline_info  ← _IOWR not _IOR
_GPIOLINE_FLAG_IS_OUT    = 1 << 0
_GPIOLINE_FLAG_KERNEL    = 1 << 4


def _chip_info(chip_path):
    with open(chip_path, 'rb') as f:
        buf = fcntl.ioctl(f, _GPIO_GET_CHIPINFO_IOCTL, b'\x00' * 68)
    name  = buf[0:32].rstrip(b'\x00').decode(errors='replace')
    label = buf[32:64].rstrip(b'\x00').decode(errors='replace')
    ngpio = struct.unpack_from('<I', buf, 64)[0]
    return name, label, ngpio


def _line_info(chip_path, offset):
    req = struct.pack('<I', offset) + b'\x00' * 68
    with open(chip_path, 'rb') as f:
        buf = fcntl.ioctl(f, _GPIO_GET_LINEINFO_IOCTL, req)
    flags    = struct.unpack_from('<I', buf, 0)[0]
    name     = buf[4:36].rstrip(b'\x00').decode(errors='replace')
    consumer = buf[36:68].rstrip(b'\x00').decode(errors='replace')
    return name, consumer, flags


def _free_lines():
    """Yield (chip_path, offset, line_name) for every unclaimed input line."""
    chips = sorted(glob.glob('/dev/gpiochip*'),
                   key=lambda p: int(p.replace('/dev/gpiochip', '')))
    for chip_path in chips:
        try:
            _, _, ngpio = _chip_info(chip_path)
            for offset in range(ngpio):
                try:
                    lname, consumer, flags = _line_info(chip_path, offset)
                    is_out    = bool(flags & _GPIOLINE_FLAG_IS_OUT)
                    is_kernel = bool(flags & _GPIOLINE_FLAG_KERNEL)
                    if not is_out and not is_kernel and not consumer:
                        yield chip_path, offset, lname
                except Exception:
                    pass
        except Exception:
            pass


# ── Single-line test ──────────────────────────────────────────────────────────

def _open_gpio(chip, line):
    from periphery import GPIO
    try:
        return GPIO(chip, line, 'in', bias='pull_up')
    except TypeError:
        return GPIO(chip, line, 'in')


def test_single(chip, line):
    """Monitor one GPIO line and report press / release events."""
    print(f"\nTesting {chip} line {line}")
    print("Connect button between this pin and GND.  Ctrl-C to quit.\n")

    try:
        gpio = _open_gpio(chip, line)
    except Exception as exc:
        print(f"ERROR: cannot open {chip} line {line}: {exc}")
        sys.exit(1)

    initial = gpio.read()
    print(f"Initial state: {'HIGH (released)' if initial else 'LOW  (pressed or floating)'}")
    if not initial:
        print("WARNING: line reads LOW at startup — may be wrong line or no pull-up.\n")

    prev       = initial
    press_time = None

    try:
        while True:
            val = gpio.read()
            if val != prev:
                now = time.time()
                if not val:   # HIGH → LOW : pressed
                    press_time = now
                    print(f"  ▼ PRESSED  at {_ts()}")
                else:         # LOW → HIGH : released
                    if press_time is not None:
                        duration = now - press_time
                        kind = "LONG press" if duration >= 2.0 else "short press"
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
    from periphery import GPIO

    print("Collecting all free GPIO lines…")
    candidates = []
    for chip_path, offset, lname in _free_lines():
        try:
            g = _open_gpio(chip_path, offset)
            baseline = g.read()
            candidates.append((chip_path, offset, lname, g, baseline))
        except Exception:
            pass

    if not candidates:
        print("No free GPIO lines found.")
        sys.exit(1)

    print(f"Monitoring {len(candidates)} free lines.")
    print("Press the button NOW (and release) — the matching line will be reported.\n")
    print(f"  {'CHIP':<18} {'LINE':>4}  {'NAME':<28} BASELINE")
    for chip_path, offset, lname, g, baseline in candidates:
        state = 'HIGH' if baseline else 'LOW '
        print(f"  {chip_path:<18} {offset:4}  {lname:<28} {state}")
    print()

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
        description='GPIO button tester for Radxa / Linux SBC')
    parser.add_argument('--chip',       default=os.environ.get('RADXA_BTN_CHIP', '/dev/gpiochip0'),
                        help='GPIO chip device (default: /dev/gpiochip0)')
    parser.add_argument('--line', type=int,
                        default=int(os.environ.get('RADXA_BTN_LINE', '97')),
                        help='GPIO line offset within the chip (default: 97)')
    parser.add_argument('--scan-press', action='store_true',
                        help='Auto-detect button line by watching all free lines')
    args = parser.parse_args()

    if args.scan_press:
        scan_press()
    else:
        test_single(args.chip, args.line)


if __name__ == '__main__':
    main()
