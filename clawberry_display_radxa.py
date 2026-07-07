#!/usr/bin/env python3
"""
clawberry_display_radxa.py
==========================
Drop-in launcher for clawberry_display.py on Radxa boards (e.g. Cubie A7Z).

Supports three Radxa display types selected by the RADXA_DISPLAY env var:

  RADXA_DISPLAY=eink   (default) — 1.54" e-paper via periphery SPI/GPIO
  RADXA_DISPLAY=oled               — 0.96" RGB OLED SSD1357 via periphery SPI/GPIO
  RADXA_DISPLAY=lcd24              — 2.4" LCD ILI9341 via periphery SPI/GPIO

This script:
  1. Applies the monkey-patch so clawberry_display.py transparently uses
     the Radxa EPD_1in54_V2 driver instead of the Waveshare HAT driver
     (only needed for eink mode; harmless for oled / lcd24 modes).
  2. Writes config/display_type.txt to skip irrelevant auto-detection.
  3. Runs clawberry_display's main loop unchanged.

Usage:
    python3 clawberry_display_radxa.py                    # e-ink mode
    RADXA_DISPLAY=oled python3 clawberry_display_radxa.py # OLED mode
    RADXA_DISPLAY=lcd24 python3 clawberry_display_radxa.py # LCD 2.4" mode

Pin overrides (optional environment variables for e-ink):
    RADXA_EPD_RST=33   RADXA_EPD_DC=110  RADXA_EPD_BUSY=313
    RADXA_EPD_SPI=/dev/spidev1.0  RADXA_EPD_CHIP=/dev/gpiochip0

Pin overrides for LCD 2.4":
    RADXA_LCD_RST=33   RADXA_LCD_DC=110  RADXA_LCD_BL=6
    RADXA_LCD_SPI=/dev/spidev1.0  RADXA_LCD_CHIP=/dev/gpiochip0
    RADXA_LCD_BL_CHIP=/dev/gpiochip1

To run as a systemd service, see daemon/clawberry-display-radxa.service.
"""

import os
import sys

# ── Step 1: apply the Radxa patch BEFORE clawberry_display is imported ────────
import clawberry_radxa_patch  # noqa: F401  (side-effect import)

# ── Step 2: write display_type.txt to skip irrelevant detection ──────────────
_HERE = os.path.dirname(os.path.realpath(__file__))
_OVERRIDE = os.path.join(_HERE, 'config', 'display_type.txt')

_radxa_display = os.environ.get('RADXA_DISPLAY', 'eink').strip().lower()
if _radxa_display == 'oled':
    _override_value = 'oled_radxa_0_96'
elif _radxa_display == 'lcd24':
    _override_value = 'lcd_radxa_2_4'
else:
    _override_value = 'eink_radxa_1_54'

try:
    os.makedirs(os.path.join(_HERE, 'config'), exist_ok=True)
    with open(_OVERRIDE, 'w') as _f:
        _f.write(_override_value + '\n')
    print(f"[radxa launcher] {_OVERRIDE} → {_override_value}")
except Exception as _e:
    print(f"[radxa launcher] Warning: could not write display_type.txt: {_e}")

# ── Step 3: hand off to the unmodified display service ───────────────────────
import runpy
runpy.run_path(
    os.path.join(_HERE, 'clawberry_display.py'),
    run_name='__main__',
)
