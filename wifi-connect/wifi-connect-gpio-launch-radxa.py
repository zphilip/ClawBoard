#!/usr/bin/env python3
"""
wifi-connect-gpio-launch-radxa.py
==================================
Radxa-native GPIO button launcher for wifi-connect.

Uses the ``periphery`` library (same as the OLED driver) instead of RPi.GPIO,
so it works on Radxa Cubie A7A, A7Z, and any other Linux SBC that exposes GPIO
via the character device interface (/dev/gpiochipX).

Hardware wiring
---------------
Button one leg   →  GPIO input pin (see board defaults below)
Button other leg →  GND (e.g. Pin 30 or Pin 39)

Confirmed defaults per board:
    Radxa Cubie A7A : Pin 7   = /dev/gpiochip0 line 32  (labelled PIN_7)
    Radxa Cubie A7Z : Pin 33  = /dev/gpiochip1 line 35  (labelled PIN_33)

Override with environment variables if your board/wiring differs:
    RADXA_BTN_CHIP=/dev/gpiochip0  RADXA_BTN_LINE=32 \\
    sudo python3 wifi-connect-gpio-launch-radxa.py

Optional flags:
    --scan | --list     Print all free GPIO lines and exit.
"""

import os
import sys
import time
import subprocess

# ── Pin configuration (auto-detected, overridable via env) ─────────────────────
from board_config import detect_board, get_button_pin, open_button, list_pins

_board = detect_board()
_default_chip, _default_line, _default_label = get_button_pin(_board)

BUTTON_CHIP = os.environ.get('RADXA_BTN_CHIP', _default_chip)
BUTTON_LINE = int(os.environ.get('RADXA_BTN_LINE', str(_default_line)))

LONG_PRESS_SECONDS = 2

WIFI_CONNECT_CMD = [
    '/opt/wifi-connect/wifi-connect',
    '--portal-ssid', 'ClawBerry-Setup',
    '-u', '/opt/wifi-connect/web',
]

POLL_INTERVAL = 0.05   # seconds between GPIO reads


# ── GPIO helpers ───────────────────────────────────────────────────────────────

def _open_button():
    """Open the button GPIO line as input (pull-up preferred)."""
    try:
        return open_button(BUTTON_CHIP, BUTTON_LINE)
    except Exception as exc:
        print(f"\nERROR: Cannot open {BUTTON_CHIP} line {BUTTON_LINE}: {exc}")
        print(f"\nDetected board: {_board}")
        print(f"Default pin for this board: {_default_label} → {_default_chip} line {_default_line}")
        print(f"\nYour current config: {BUTTON_CHIP} line {BUTTON_LINE}")
        print(f"\nTo scan all free GPIO lines:")
        print(f"    sudo python3 {os.path.basename(__file__)} --list")
        print(f"\nTo test a specific pin:")
        print(f"    sudo python3 gpio-button-test.py --chip {BUTTON_CHIP} --line {BUTTON_LINE}")
        print(f"\nTo override:")
        print(f"    RADXA_BTN_CHIP=/dev/gpiochipX RADXA_BTN_LINE=N sudo python3 {os.path.basename(__file__)}")
        sys.exit(1)


# ── Long-press detection ──────────────────────────────────────────────────────

def wait_for_long_press():
    """Block until a long press (≥ LONG_PRESS_SECONDS) is detected.

    Wiring is active-LOW: released=HIGH, pressed=LOW.
    """
    print(f"Board          : {_board}")
    print(f"Button pin     : {BUTTON_CHIP} line {BUTTON_LINE}")
    print(f"Long-press min : {LONG_PRESS_SECONDS}s")
    print(f"Waiting for long press (hold button ≥{LONG_PRESS_SECONDS}s)…")

    btn = _open_button()
    try:
        baseline = btn.read()
        if not baseline:
            print(
                f"\nERROR: {BUTTON_CHIP} line {BUTTON_LINE} is LOW at startup.\n"
                f"Expected released state is HIGH for active-LOW wiring.\n"
                f"\nPossible causes:\n"
                f"  1. Button is stuck / held down\n"
                f"  2. Pin has a hardware pull-down (check --list output)\n"
                f"  3. Wrong pin — try --list to find a pin that reads HIGH\n"
                f"\nOverride with:\n"
                f"  RADXA_BTN_CHIP=/dev/gpiochipX RADXA_BTN_LINE=N \\\n"
                f"  sudo python3 {os.path.basename(__file__)}"
            )
            sys.exit(1)

        print(f"  Polarity: active-LOW (resting=HIGH, pressed=LOW)")

        def is_pressed():
            return not btn.read()

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
    if '--scan' in sys.argv or '--list' in sys.argv:
        list_pins()
        sys.exit(0)
    main()
