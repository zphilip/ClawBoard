#!/usr/bin/env python3
"""
Signal clawberry-display service to show temporary content on the 2.13" e-ink display.

The service polls DISPLAY_REQUEST_FILE; when it finds one it displays the
requested content for the supplied number of seconds, then resumes monitoring.

Usage (CLI):  python3 clawberry_paircode.py <code>
Usage (API):  from clawberry_paircode import request_paircode_display,
                         request_picoclaw_qr_display
"""

import json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO)

_HERE = os.path.dirname(os.path.realpath(__file__))
DISPLAY_REQUEST_FILE = os.path.join(_HERE, 'config', 'clawberry_paircode.txt')
os.makedirs(os.path.dirname(DISPLAY_REQUEST_FILE), exist_ok=True)


def _write_request(payload: dict) -> None:
    with open(DISPLAY_REQUEST_FILE, 'w') as f:
        json.dump(payload, f)


def request_paircode_display(code: str) -> None:
    """Queue a ZeroClaw pair code for display service rendering."""
    payload = {
        'kind': 'paircode',
        'code': code.strip(),
        'seconds': 20,
    }
    try:
        _write_request(payload)
        logging.info("Pair code '%s' queued for display (wrote %s)", code, DISPLAY_REQUEST_FILE)
    except Exception as e:
        logging.error("Could not write paircode file: %s", e)
        raise


def request_picoclaw_qr_display(url: str, token: str = '') -> None:
    """Queue a PicoClaw pairing QR for the display service."""
    payload = {
        'kind': 'pico_qr',
        'url': url.strip(),
        'token': token.strip(),
        'seconds': 20,
    }
    try:
        _write_request(payload)
        logging.info("PicoClaw QR queued for display (wrote %s)", DISPLAY_REQUEST_FILE)
    except Exception as e:
        logging.error("Could not write pico QR request: %s", e)
        raise


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(f"Usage: {os.path.basename(__file__)} <pair-code>")
        sys.exit(1)
    request_paircode_display(sys.argv[1])
