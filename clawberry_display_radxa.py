#!/usr/bin/env python3
"""
clawberry_display_radxa.py
==========================
Drop-in launcher for clawberry_display.py on Radxa boards (e.g. Cubie A7Z)
using a 1.54" e-paper display wired via SPI + periphery GPIO.

This script:
  1. Applies the monkey-patch so clawberry_display.py transparently uses
     the Radxa EPD_1in54_V2 driver instead of the Waveshare HAT driver.
  2. Forces the display type to 'eink' so the auto-detection in
     clawberry_display.py does not waste time trying LCD / OLED.
  3. Runs clawberry_display's main loop unchanged.

Usage:
    python3 clawberry_display_radxa.py

Pin overrides (optional environment variables):
    RADXA_EPD_RST=33   RADXA_EPD_DC=110  RADXA_EPD_BUSY=313
    RADXA_EPD_SPI=/dev/spidev1.0  RADXA_EPD_CHIP=/dev/gpiochip0

To run as a systemd service, see daemon/clawberry-display-radxa.service.
"""

import os
import sys

# ── Step 1: apply the Radxa patch BEFORE clawberry_display is imported ────────
import clawberry_radxa_patch  # noqa: F401  (side-effect import)

# ── Step 2: force eink mode so detection skips LCD/OLED probes ───────────────
_HERE = os.path.dirname(os.path.realpath(__file__))
_OVERRIDE = os.path.join(_HERE, 'config', 'display_type.txt')
if not os.path.exists(_OVERRIDE):
    try:
        os.makedirs(os.path.join(_HERE, 'config'), exist_ok=True)
        with open(_OVERRIDE, 'w') as _f:
            _f.write('eink_radxa_1_54\n')
        print(f"[radxa launcher] Created {_OVERRIDE} → eink_radxa_1_54")
    except Exception as _e:
        print(f"[radxa launcher] Warning: could not write display_type.txt: {_e}")

# ── Step 3: hand off to the unmodified display service ───────────────────────
# We exec the module code directly so it runs in this process (shares the
# patched sys.modules).  runpy.run_path keeps __file__ accurate for relative
# path resolution inside clawberry_display.py.
import runpy
runpy.run_path(
    os.path.join(_HERE, 'clawberry_display.py'),
    run_name='__main__',
)
