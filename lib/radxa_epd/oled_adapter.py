"""
Adapter: makes OLED_0in96_SSD1357 (Radxa periphery-based) look like
the waveshare OLED_0in96_rgb interface used by clawberry_display.py,
so the display service can use it without modification.

Interface contract satisfied (consumed by clawberry_display.py):

  oled.width       → raw portrait width  of the panel  (64)
  oled.height      → raw portrait height of the panel  (128)
  oled.ShowImage(pil_image)   → accepts a 64×128 RGB PIL image (portrait)
  oled.getbuffer(pil_image)   → (unused by Radxa path; kept for compat)
  oled.Init()                 → full initialisation
  oled.clear()                → black fill
  oled.module_exit()          → shutdown + close hardware (called by _shutdown())

Canvas convention used by clawberry_display.py:
  _OLED_W = 128  (landscape canvas width)
  _OLED_H =  64  (landscape canvas height)
  _oled_show() rotates the 128×64 landscape canvas 270° → 64×128 portrait
  then calls  ShowImage(rotated_image)  — so ShowImage always receives a
  64×128 portrait PIL image, which is what display_image() expects.
"""

import logging
import sys
import os

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.realpath(__file__))
_LIB  = os.path.dirname(_HERE)    # lib/
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from radxa_epd.oled0in96_ssd1357 import OLED_0in96_SSD1357


class OLEDRadxa:
    """Waveshare-compatible wrapper around OLED_0in96_SSD1357 for Radxa boards.

    Constructor accepts the same keyword arguments as OLED_0in96_SSD1357 so
    pin assignments can be overridden from the detection code if needed.

    Example override (e.g. if your DC wire is on a different GPIO line)::

        from radxa_epd.oled_adapter import OLEDRadxa
        oled = OLEDRadxa(dc_line=116, rst_line=14)
        oled.Init()
    """

    def __init__(self, **kwargs):
        self._oled = OLED_0in96_SSD1357(**kwargs)
        # Expose portrait geometry.
        # clawberry_display.py draws on a 128×64 landscape canvas then
        # rotates 270° before calling ShowImage(), yielding a 64×128
        # portrait image — matching these dimensions exactly.
        self.width  = self._oled.width    # 64   (portrait width)
        self.height = self._oled.height   # 128  (portrait height)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def Init(self) -> None:
        """Full SSD1357 initialisation (called once at startup)."""
        self._oled.init()

    def clear(self) -> None:
        """Fill display with black."""
        self._oled.clear()

    def module_exit(self) -> None:
        """Display off + close hardware (called by clawberry_display._shutdown)."""
        self._oled.Dev_exit()

    # ── Aliases expected by _shutdown() in clawberry_display.py ──────────────

    def sleep(self) -> None:
        self._oled.sleep()

    def Dev_exit(self) -> None:
        self._oled.Dev_exit()

    # ── Rendering ─────────────────────────────────────────────────────────────

    def ShowImage(self, pil_image) -> None:
        """Push a PIL RGB image (64×128 portrait) to the OLED panel.

        Internally converts to RGB565 and transfers via SPI.
        """
        self._oled.display_image(pil_image)

    def getbuffer(self, pil_image) -> bytes:
        """Return an RGB565 byte buffer for *pil_image* (compatibility shim).

        Not used in the Radxa OLED path — ShowImage() is called directly —
        but retained so calling code that does ``disp.ShowImage(disp.getbuffer(img))``
        still works.
        """
        import struct
        img = pil_image.convert('RGB')
        if img.size != (self.width, self.height):
            img = img.resize((self.width, self.height))
        buf = bytearray(self.width * self.height * 2)
        idx = 0
        for r, g, b in img.getdata():
            rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            buf[idx]     = (rgb565 >> 8) & 0xFF
            buf[idx + 1] =  rgb565       & 0xFF
            idx += 2
        return bytes(buf)
