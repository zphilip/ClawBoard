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
  64×128 portrait PIL image.

GRAM layout:
  The SSD1357 GRAM is 128×128.  ShowImage() centres the 64×128 portrait
  image horizontally (32 px black padding left + right) to fill all 128
  columns.  This avoids half-screen blank or noise artefacts.
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
        # Inject explicit chip paths unless the caller already overrides them.
        # This bypasses _resolve_gpio which incorrectly maps gpiochip1 lines
        # to gpiochip0 when gpiochip0 has a large enough line count.
        kwargs.setdefault('rst_chip', '/dev/gpiochip1')
        kwargs.setdefault('dc_chip',  '/dev/gpiochip1')
        self._oled = OLED_0in96_SSD1357(**kwargs)
        # Expose portrait canvas geometry that clawberry_display.py expects.
        # The physical GRAM is 128×128, but we tell the display layer that
        # the usable portrait area is 64×128.  ShowImage() centres the
        # 64×128 image horizontally in the 128×128 GRAM automatically.
        self.width  = 64    # portrait canvas width  (not physical GRAM width)
        self.height = 128   # portrait canvas height = physical GRAM height

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

        The SSD1357 GRAM is 128×128.  The 64-wide portrait image is centred
        horizontally: 32 columns of black padding on each side fill the
        remaining 64 columns so the full GRAM is written cleanly.
        """
        from PIL import Image as _Image
        img = pil_image.convert('RGB')
        if img.size != (64, 128):
            img = img.resize((64, 128))
        # Build full 128×128 frame: black background, image centred at x=32
        frame = _Image.new('RGB', (128, 128), (0, 0, 0))
        frame.paste(img, (32, 0))
        self._oled.display_image(frame)

    def getbuffer(self, pil_image) -> bytes:
        """Return an RGB565 byte buffer for *pil_image* centred in 128×128 GRAM.

        Not used in the Radxa OLED path — ShowImage() is called directly —
        but retained so calling code that does ``disp.ShowImage(disp.getbuffer(img))``
        still works.  Returns 128×128×2 = 32768 bytes.
        """
        from PIL import Image as _Image
        img = pil_image.convert('RGB')
        if img.size != (64, 128):
            img = img.resize((64, 128))
        frame = _Image.new('RGB', (128, 128), (0, 0, 0))
        frame.paste(img, (32, 0))
        buf = bytearray(128 * 128 * 2)
        idx = 0
        for r, g, b in frame.getdata():
            rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            buf[idx]     = (rgb565 >> 8) & 0xFF
            buf[idx + 1] =  rgb565       & 0xFF
            idx += 2
        return bytes(buf)
