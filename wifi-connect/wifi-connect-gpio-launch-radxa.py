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

# ── Pin configuration ─────────────────────────────────────────────────────────
# Pin 33 on Radxa Cubie A7Z 40-pin header → gpiochip1, line 3
# Override with env vars if your board revision differs.
BUTTON_CHIP = os.environ.get('RADXA_BTN_CHIP', '/dev/gpiochip1')
BUTTON_LINE = int(os.environ.get('RADXA_BTN_LINE', '3'))

LONG_PRESS_SECONDS = 2

WIFI_CONNECT_CMD = [
    '/opt/wifi-connect/wifi-connect',
    '--portal-ssid', 'ClawBerry-Setup',
    '-u', '/opt/wifi-connect/web',
]

POLL_INTERVAL = 0.05   # seconds between GPIO reads


# ── GPIO helpers (periphery) ──────────────────────────────────────────────────

def _open_button():
    """Open the button GPIO line as input with internal pull-up."""
    from periphery import GPIO
    # 'in' with bias 'pull_up' — button connects pin to GND so pressed = LOW
    try:
        return GPIO(BUTTON_CHIP, BUTTON_LINE, 'in', bias='pull_up')
    except TypeError:
        # Older periphery versions don't support the bias kwarg;
        # rely on external pull-up or board default.
        return GPIO(BUTTON_CHIP, BUTTON_LINE, 'in')


def _is_pressed(gpio) -> bool:
    """Return True when the button is pressed (active-LOW)."""
    return not gpio.read()


def wait_for_long_press():
    """Block until a long press (≥ LONG_PRESS_SECONDS) is detected."""
    print(f"Waiting for long press on {BUTTON_CHIP} line {BUTTON_LINE} "
          f"(≥{LONG_PRESS_SECONDS}s)…")
    btn = _open_button()
    try:
        # Ensure NetworkManager has WiFi radio enabled
        try:
            subprocess.run(["nmcli", "radio", "wifi", "on"], check=False,
                           capture_output=True)
        except Exception:
            pass

        while True:
            # Wait for button to be pressed
            while not _is_pressed(btn):
                time.sleep(POLL_INTERVAL)

            # Button pressed — start timing
            press_start = time.time()
            while _is_pressed(btn):
                time.sleep(POLL_INTERVAL)
                if time.time() - press_start >= LONG_PRESS_SECONDS:
                    print("Long press detected. Launching WiFi Connect…")
                    return

            # Released too soon — ignore and wait again
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
    main()
