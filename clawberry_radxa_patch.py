"""
clawberry_radxa_patch.py
========================
Monkey-patch shim for running clawberry_display.py on a Radxa board with a
1.54" e-paper display (EPD_1in54_V2, periphery-based SPI/GPIO).

HOW IT WORKS
------------
clawberry_display.py imports the e-ink driver as:

    from waveshare_epd import epd2in13_V4 as _em

This module replaces that import in sys.modules BEFORE clawberry_display is
loaded, so the detection code transparently receives the Radxa adapter.

USAGE
-----
Import this module at the very top of your launcher script, before anything
else touches waveshare_epd:

    # clawberry_display_radxa.py  (Radxa launcher — see below)
    import clawberry_radxa_patch   # ← must be first
    import clawberry_display       # normal entry point

Or run via the provided launcher:

    python3 clawberry_display_radxa.py

PIN OVERRIDES
-------------
Export environment variables before launching to override the defaults:

    RADXA_EPD_RST=33 RADXA_EPD_DC=110 RADXA_EPD_BUSY=313 \\
    RADXA_EPD_SPI=/dev/spidev1.0 RADXA_EPD_CHIP=/dev/gpiochip0 \\
    python3 clawberry_display_radxa.py
"""

import os
import sys
import types
import logging

logger = logging.getLogger(__name__)

# ── Locate lib/ and radxa_epd ────────────────────────────────────────────────
_HERE    = os.path.dirname(os.path.realpath(__file__))
_LIB_DIR = os.path.join(_HERE, 'lib')
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

# ── Read optional pin overrides from environment ──────────────────────────────
def _env_int(key, default):
    try:
        return int(os.environ[key])
    except (KeyError, ValueError):
        return default

def _env_str(key, default):
    return os.environ.get(key, default)

_EPD_KWARGS = dict(
    rst_line  = _env_int('RADXA_EPD_RST',   33),
    dc_line   = _env_int('RADXA_EPD_DC',    110),
    busy_line = _env_int('RADXA_EPD_BUSY',  313),
    spi_dev   = _env_str('RADXA_EPD_SPI',   '/dev/spidev1.0'),
    gpiochip  = _env_str('RADXA_EPD_CHIP',  '/dev/gpiochip0'),
)

# ── Import the adapter ────────────────────────────────────────────────────────
from radxa_epd.epd_adapter import EPD as _RadxaEPD


# ── Build a fake epd2in13_V4 module ──────────────────────────────────────────
class _PatchedEPD(_RadxaEPD):
    """EPD subclass that injects the configured pin kwargs automatically."""
    def __init__(self):
        super().__init__(**_EPD_KWARGS)


_fake_module = types.ModuleType('waveshare_epd.epd2in13_V4')
_fake_module.EPD   = _PatchedEPD
_fake_module.EPD_WIDTH  = 200
_fake_module.EPD_HEIGHT = 200

# Ensure the parent package exists in sys.modules too
if 'waveshare_epd' not in sys.modules:
    _pkg = types.ModuleType('waveshare_epd')
    _pkg.__path__ = []
    sys.modules['waveshare_epd'] = _pkg

sys.modules['waveshare_epd.epd2in13_V4'] = _fake_module

# Also make it importable as "from waveshare_epd import epd2in13_V4"
sys.modules['waveshare_epd'].epd2in13_V4 = _fake_module  # type: ignore

logger.info(
    "clawberry_radxa_patch: display_type=eink_radxa_1_54, "
    "waveshare_epd.epd2in13_V4 → Radxa EPD_1in54_V2 "
    "(RST=%d DC=%d BUSY=%d SPI=%s chip=%s)",
    _EPD_KWARGS['rst_line'], _EPD_KWARGS['dc_line'], _EPD_KWARGS['busy_line'],
    _EPD_KWARGS['spi_dev'], _EPD_KWARGS['gpiochip'],
)
