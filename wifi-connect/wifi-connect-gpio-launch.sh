#!/usr/bin/env python3
import time
import subprocess
import os
import sys


def _read_dt_compatible() -> str:
    """Return /proc/device-tree/compatible as a space-joined lowercase string."""
    try:
        with open('/proc/device-tree/compatible', 'rb') as f:
            raw = f.read().replace(b'\x00', b' ').decode(errors='ignore').strip()
        return raw.lower()
    except Exception:
        return ''


def _is_radxa_board() -> bool:
    """Detect Radxa Cubie boards from the device-tree compatible string."""
    compatible = _read_dt_compatible()
    if not compatible:
        return False
    return (
        'radxa,cubie-a7z' in compatible
        or 'radxa' in compatible
        or 'sun60iw2p1' in compatible
    )


def _maybe_delegate_to_radxa() -> None:
    """On Radxa boards, replace this process with the Radxa GPIO launcher."""
    if not _is_radxa_board():
        return

    here = os.path.dirname(os.path.realpath(__file__))
    radxa_script = os.path.join(here, 'wifi-connect-gpio-launch-radxa.py')
    if not os.path.exists(radxa_script):
        print(f"Radxa board detected, but missing launcher: {radxa_script}")
        sys.exit(1)

    compatible = _read_dt_compatible()
    print(f"Radxa board detected ({compatible}) — delegating to {os.path.basename(radxa_script)}")
    os.execv(sys.executable, [sys.executable, radxa_script, *sys.argv[1:]])


_maybe_delegate_to_radxa()

import RPi.GPIO as GPIO

BUTTON_PIN = 13
LONG_PRESS_SECONDS = 2
WIFI_CONNECT_CMD = [
    '/opt/wifi-connect/wifi-connect',
    '--portal-ssid', 'ClawBerry-Setup',
    '-u', '/opt/wifi-connect/web'
]

def wait_for_long_press():
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    print(f"Waiting for long press on GPIO {BUTTON_PIN}...")
    try:
        # Ensure NetworkManager has WiFi radio enabled at runtime
        try:
            subprocess.run(["nmcli", "radio", "wifi", "on"], check=False)
        except Exception:
            pass
        while True:
            # Wait for button press
            while GPIO.input(BUTTON_PIN):
                time.sleep(0.05)
            # Button pressed, start timing
            start = time.time()
            while not GPIO.input(BUTTON_PIN):
                time.sleep(0.05)
                if time.time() - start >= LONG_PRESS_SECONDS:
                    print("Long press detected. Launching WiFi Connect...")
                    return
            # Button released too soon, ignore
    finally:
        GPIO.cleanup()

def is_service_active(name: str) -> bool:
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", name],
        capture_output=True,
    )
    return result.returncode == 0


def kill_all_dnsmasq():
    """Kill every running dnsmasq process.

    NetworkManager can spawn its own dnsmasq (dns=dnsmasq plugin) that is
    NOT managed by dnsmasq.service.  That process binds to 0.0.0.0:53 and
    prevents wifi-connect's dnsmasq from binding to 192.168.42.1:53, which
    produces the 'Address already in use' error seen in the journal.
    A short sleep after pkill lets the kernel fully release the sockets
    before wifi-connect starts.
    """
    subprocess.run(["pkill", "-x", "dnsmasq"], capture_output=True)
    time.sleep(0.5)


def main():
    wait_for_long_press()

    # Stop dnsmasq.service if it is running
    dnsmasq_was_active = is_service_active("dnsmasq")
    if dnsmasq_was_active:
        print("Stopping dnsmasq.service before wifi-connect...")
        subprocess.run(["sudo", "/usr/bin/systemctl", "stop", "dnsmasq"],
                       capture_output=True)

    # Kill any remaining dnsmasq processes (e.g. NetworkManager's internal
    # dnsmasq plugin) that would prevent wifi-connect from binding 192.168.42.1:53
    print("Killing residual dnsmasq processes...")
    kill_all_dnsmasq()

    try:
        subprocess.run(WIFI_CONNECT_CMD)
    finally:
        # Always restore dnsmasq if we stopped it
        if dnsmasq_was_active:
            print("Restoring dnsmasq...")
            subprocess.run(["sudo", "/usr/bin/systemctl", "start", "dnsmasq"],
                           capture_output=True)


if __name__ == '__main__':
    main()
