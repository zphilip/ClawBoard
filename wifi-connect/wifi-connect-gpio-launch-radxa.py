#!/usr/bin/env python3
"""
wifi-connect-gpio-launch-radxa.py
==================================
Radxa-native replacement for wifi-connect-gpio-launch.sh.

Uses the ``periphery`` library (same as the OLED driver) instead of RPi.GPIO,
so it works on Radxa Cubie A7Z and any other Linux SBC that exposes GPIO via
the character device interface (/dev/gpiochipX).

Hardware wiring
---------------
  Button one leg  →  Pin 33  (GPIO input, pulled up internally)
  Button other leg →  Pin 39  (GND)

Pin 33 on the Radxa Cubie A7Z 40-pin header is typically gpiochip1 line 3.
Run the following on the board to confirm:
    gpioinfo | grep -n ''
    # or interactively:
    gpiodetect && gpioinfo gpiochip1

Override defaults with environment variables:
    RADXA_BTN_CHIP=/dev/gpiochip1  RADXA_BTN_LINE=3 \\
    python3 wifi-connect-gpio-launch-radxa.py
"""

import os
import sys
import time
import subprocess
import glob
import fcntl
import struct

# ── Pin configuration ─────────────────────────────────────────────────────────
# Pin 33 on Radxa Cubie A7Z 40-pin header.
# Confirmed by gpio-button-test.py --scan-press: gpiochip1 line 10.
# Override with env vars if your board differs:
#   RADXA_BTN_CHIP=/dev/gpiochip1  RADXA_BTN_LINE=10  sudo python3 ...
BUTTON_CHIP = os.environ.get('RADXA_BTN_CHIP', '/dev/gpiochip1')
BUTTON_LINE = int(os.environ.get('RADXA_BTN_LINE', '10'))

LONG_PRESS_SECONDS = 2

WIFI_CONNECT_CMD = [
    '/opt/wifi-connect/wifi-connect',
    '--portal-ssid', 'ClawBerry-Setup',
    '-u', '/opt/wifi-connect/web',
]

POLL_INTERVAL = 0.05   # seconds between GPIO reads


# ── GPIO scan helper ──────────────────────────────────────────────────────────

# ioctl constants for GPIO character device v1 (works on all kernel versions)
_GPIO_GET_CHIPINFO_IOCTL  = 0x8044B401   # _IOR(0xB4, 0x01, 68)  gpiochip_info
_GPIO_GET_LINEINFO_IOCTL  = 0xC048B402   # _IOWR(0xB4, 0x02, 72) gpioline_info  ← _IOWR not _IOR


def _chip_info(chip_path):
    """Return (name, label, ngpio) for a gpiochip device."""
    with open(chip_path, 'rb') as f:
        buf = fcntl.ioctl(f, _GPIO_GET_CHIPINFO_IOCTL, b'\x00' * 68)
    name  = buf[0:32].rstrip(b'\x00').decode(errors='replace')
    label = buf[32:64].rstrip(b'\x00').decode(errors='replace')
    ngpio = struct.unpack_from('<I', buf, 64)[0]
    return name, label, ngpio


def _line_info(chip_path, offset):
    """Return (line_name, consumer, flags) for one GPIO line."""
    req = struct.pack('<I', offset) + b'\x00' * 68  # gpioline_info
    with open(chip_path, 'rb') as f:
        buf = fcntl.ioctl(f, _GPIO_GET_LINEINFO_IOCTL, req)
    flags    = struct.unpack_from('<I', buf, 0)[0]
    name     = buf[4:36].rstrip(b'\x00').decode(errors='replace')
    consumer = buf[36:68].rstrip(b'\x00').decode(errors='replace')
    return name, consumer, flags


_GPIOLINE_FLAG_IS_OUT   = 1 << 0
_GPIOLINE_FLAG_ACTIVE_LOW = 1 << 1
_GPIOLINE_FLAG_OPEN_DRAIN = 1 << 2
_GPIOLINE_FLAG_OPEN_SOURCE = 1 << 3
_GPIOLINE_FLAG_KERNEL   = 1 << 4   # claimed by kernel driver


def scan_gpio():
    """Print all GPIO chips and lines, highlighting free input lines."""
    chips = sorted(glob.glob('/dev/gpiochip*'),
                   key=lambda p: int(p.replace('/dev/gpiochip', '')))
    print(f"\n{'CHIP':<16} {'LINE':>4}  {'NAME':<28} {'CONSUMER':<20} STATUS")
    print('-' * 80)
    for chip_path in chips:
        try:
            cname, clabel, ngpio = _chip_info(chip_path)
            print(f"\n{chip_path}  —  {cname} / {clabel}  ({ngpio} lines)")
            for offset in range(ngpio):
                try:
                    lname, consumer, flags = _line_info(chip_path, offset)
                    busy   = bool(consumer) or bool(flags & _GPIOLINE_FLAG_KERNEL)
                    is_out = bool(flags & _GPIOLINE_FLAG_IS_OUT)
                    status = ('OUT ' if is_out else 'in  ')
                    status += f'[{consumer}]' if consumer else ('[kernel]' if flags & _GPIOLINE_FLAG_KERNEL else 'FREE ←')
                    print(f"  {'':14} {offset:4}  {lname:<28} {consumer:<20} {status}")
                except Exception:
                    pass
        except Exception as exc:
            print(f"  {chip_path}: {exc}")
    print()


# ── GPIO helpers (periphery) ──────────────────────────────────────────────────

def _open_button():
    """Open the button GPIO line as input with internal pull-up."""
    from periphery import GPIO
    try:
        return GPIO(BUTTON_CHIP, BUTTON_LINE, 'in', bias='pull_up')
    except TypeError:
        # Older periphery versions don't support the bias kwarg
        return GPIO(BUTTON_CHIP, BUTTON_LINE, 'in')
    except Exception as exc:
        print(f"\nERROR: Cannot open {BUTTON_CHIP} line {BUTTON_LINE}: {exc}")
        print("\nRun with --scan to list all GPIO lines and find the free one for Pin 33:")
        print(f"    sudo python3 {os.path.basename(__file__)} --scan")
        print("\nThen re-run with the correct chip/line:")
        print(f"    RADXA_BTN_CHIP=/dev/gpiochipX RADXA_BTN_LINE=N sudo python3 {os.path.basename(__file__)}")
        sys.exit(1)


# _STARTUP_VERIFY_SECONDS and _is_pressed are superseded by polarity
# auto-detection inside wait_for_long_press(); kept as no-ops for compat.
_STARTUP_VERIFY_SECONDS = 0.3


def wait_for_long_press():
    """Block until a long press (≥ LONG_PRESS_SECONDS) is detected.

    Polarity is auto-detected from the baseline (resting) state:
      baseline LOW  → active-HIGH wiring (pull-down / floating-low)
      baseline HIGH → active-LOW  wiring (pull-up)
    """
    print(f"Waiting for long press on {BUTTON_CHIP} line {BUTTON_LINE} "
          f"(≥{LONG_PRESS_SECONDS}s)…")
    btn = _open_button()
    try:
        # ── Auto-detect polarity ──────────────────────────────────────────
        baseline  = btn.read()
        active_high = not baseline   # pressed = opposite of resting state
        polarity  = 'active-HIGH (resting=LOW)' if active_high else 'active-LOW (resting=HIGH)'
        print(f"  Polarity auto-detected: {polarity}")

        def is_pressed():
            return btn.read() if active_high else not btn.read()

        # Ensure NetworkManager has WiFi radio enabled
        try:
            subprocess.run(["nmcli", "radio", "wifi", "on"], check=False,
                           capture_output=True)
        except Exception:
            pass

        while True:
            # Wait for button to be pressed
            while not is_pressed():
                time.sleep(POLL_INTERVAL)

            # Button pressed — start timing
            press_start = time.time()
            while is_pressed():
                time.sleep(POLL_INTERVAL)
                if time.time() - press_start >= LONG_PRESS_SECONDS:
                    print("Long press detected. Launching WiFi Connect…")
                    return

            # Released too soon — ignore and wait again
            elapsed = time.time() - press_start
            print(f"Short press ({elapsed:.1f}s) — ignored, keep holding for {LONG_PRESS_SECONDS}s")
    finally:
        btn.close()


# ── Service helpers ───────────────────────────────────────────────────────────

def is_service_active(name: str) -> bool:
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", name],
        capture_output=True,
    )
    return result.returncode == 0


def kill_all_dnsmasq():
    """Kill every running dnsmasq process.

    NetworkManager can spawn its own dnsmasq (dns=dnsmasq plugin) that binds
    to 0.0.0.0:53 and prevents wifi-connect's dnsmasq from binding to
    192.168.42.1:53 ('Address already in use').  A short sleep lets the
    kernel fully release the sockets before wifi-connect starts.
    """
    subprocess.run(["pkill", "-x", "dnsmasq"], capture_output=True)
    time.sleep(0.5)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    wait_for_long_press()

    dnsmasq_was_active = is_service_active("dnsmasq")
    if dnsmasq_was_active:
        print("Stopping dnsmasq.service before wifi-connect…")
        subprocess.run(["sudo", "/usr/bin/systemctl", "stop", "dnsmasq"],
                       capture_output=True)

    print("Killing residual dnsmasq processes…")
    kill_all_dnsmasq()

    try:
        subprocess.run(WIFI_CONNECT_CMD)
    finally:
        if dnsmasq_was_active:
            print("Restoring dnsmasq…")
            subprocess.run(["sudo", "/usr/bin/systemctl", "start", "dnsmasq"],
                           capture_output=True)


if __name__ == '__main__':
    if '--scan' in sys.argv:
        scan_gpio()
        sys.exit(0)
    main()
