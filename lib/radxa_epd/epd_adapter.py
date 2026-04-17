"""
Adapter: makes EPD_1in54_V2 (Radxa periphery-based) look like a
waveshare_epd.epd2in13_V4.EPD instance so that clawberry_display.py
can use it without any modification.

Interface contract satisfied by this adapter
(all attributes/methods consumed by clawberry_display.py):

  epd.width   → raw pixel width  of the panel
  epd.height  → raw pixel height of the panel
  epd.getbuffer(pil_image) → bytes
  epd.init()
  epd.init_fast()
  epd.display(buf)
  epd.display_fast(buf)
  epd.sleep()
  epd.Dev_exit()          (called by _shutdown())

The 1.54" panel is square (200×200).  clawberry_display.py accesses the
display as landscape via  W, H = epd.height, epd.width  so the adapter
exposes:
  .width  = 200   (short side — becomes H in landscape)
  .height = 200   (long  side — becomes W in landscape)

Because the panel is square those are identical, but the convention is
preserved so the drawing code works correctly if the panel geometry changes.
"""

import logging
import sys
import os

logger = logging.getLogger(__name__)

# Ensure lib/ directory is in sys.path so radxa_epd can be found
_HERE = os.path.dirname(os.path.realpath(__file__))
_LIB  = os.path.dirname(_HERE)   # lib/
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from radxa_epd.epd1in54_v2 import EPD_1in54_V2


class EPD:
    """Waveshare-compatible wrapper around EPD_1in54_V2 for Radxa boards.

    Constructor accepts the same keyword arguments as EPD_1in54_V2 so
    pin assignments can be overridden from the detection code if needed.
    """

    def __init__(self, **kwargs):
        self._epd = EPD_1in54_V2(**kwargs)
        # Expose geometry in the same layout as epd2in13_V4.EPD:
        #   .width  → portrait width  (short axis)
        #   .height → portrait height (long  axis)
        # For a square panel both are 200; kept explicit for clarity.
        self.width  = self._epd.width    # 200
        self.height = self._epd.height   # 200

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def init(self) -> None:
        self._epd.init()

    def init_fast(self) -> None:
        self._epd.init_fast()

    def sleep(self) -> None:
        self._epd.sleep()

    def Dev_exit(self) -> None:
        self._epd.Dev_exit()

    # ── Rendering ─────────────────────────────────────────────────────────────

    def getbuffer(self, image) -> bytes:
        """Convert a PIL image to the packed 1-bit buffer for this display."""
        return self._epd.getbuffer(image)

    def display(self, image_buffer: bytes) -> None:
        self._epd.display(image_buffer)

    def display_fast(self, image_buffer: bytes) -> None:
        self._epd.display_fast(image_buffer)

    def clear(self, color: int = 0xFF) -> None:
        self._epd.clear(color)
