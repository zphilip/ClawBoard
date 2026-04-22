#!/usr/bin/env python3
import time
import subprocess

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
        try:
            subprocess.run(["nmcli", "radio", "wifi", "on"], check=False)
        except Exception:
            pass
        while True:
            while GPIO.input(BUTTON_PIN):
                time.sleep(0.05)
            start = time.time()
            while not GPIO.input(BUTTON_PIN):
                time.sleep(0.05)
                if time.time() - start >= LONG_PRESS_SECONDS:
                    print("Long press detected. Launching WiFi Connect...")
                    return
    finally:
        GPIO.cleanup()


def is_service_active(name: str) -> bool:
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", name],
        capture_output=True,
    )
    return result.returncode == 0


def kill_all_dnsmasq():
    """Kill every running dnsmasq process."""
    subprocess.run(["pkill", "-x", "dnsmasq"], capture_output=True)
    time.sleep(0.5)


def main():
    wait_for_long_press()

    dnsmasq_was_active = is_service_active("dnsmasq")
    if dnsmasq_was_active:
        print("Stopping dnsmasq.service before wifi-connect...")
        subprocess.run(["sudo", "/usr/bin/systemctl", "stop", "dnsmasq"],
                       capture_output=True)

    print("Killing residual dnsmasq processes...")
    kill_all_dnsmasq()

    try:
        subprocess.run(WIFI_CONNECT_CMD)
    finally:
        if dnsmasq_was_active:
            print("Restoring dnsmasq...")
            subprocess.run(["sudo", "/usr/bin/systemctl", "start", "dnsmasq"],
                           capture_output=True)


if __name__ == '__main__':
    main()
