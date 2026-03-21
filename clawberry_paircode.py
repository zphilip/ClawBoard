#!/usr/bin/env python3
"""
Signal clawberry-display service to show a pair code on the 2.13" e-ink display.

The service polls PAIRCODE_FILE; when it finds one it displays the code for
DISPLAY_SECONDS seconds, then resumes normal monitoring.

Usage (CLI):  python3 clawberry_paircode.py <code>
Usage (API):  from clawberry_paircode import request_paircode_display
              request_paircode_display("752167")
"""

import os
import sys
import logging

logging.basicConfig(level=logging.INFO)

# Shared handoff file — service polls this path
_HERE         = os.path.dirname(os.path.realpath(__file__))
PAIRCODE_FILE = os.path.join(_HERE, 'config', 'clawberry_paircode.txt')
os.makedirs(os.path.dirname(PAIRCODE_FILE), exist_ok=True)


def request_paircode_display(code: str) -> None:
    """Write *code* to the handoff file; the display service will pick it up."""
    try:
        with open(PAIRCODE_FILE, 'w') as f:
            f.write(code.strip())
        logging.info("Pair code '%s' queued for display (wrote %s)", code, PAIRCODE_FILE)
    except Exception as e:
        logging.error("Could not write paircode file: %s", e)
        raise


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(f"Usage: {os.path.basename(__file__)} <pair-code>")
        sys.exit(1)
    request_paircode_display(sys.argv[1])
