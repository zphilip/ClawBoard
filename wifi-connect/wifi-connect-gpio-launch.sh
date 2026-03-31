#!/usr/bin/env python3
import RPi.GPIO as GPIO
import time
import subprocess
import os

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

def main():
    wait_for_long_press()
    os.execv(WIFI_CONNECT_CMD[0], WIFI_CONNECT_CMD)

if __name__ == '__main__':
    main()
