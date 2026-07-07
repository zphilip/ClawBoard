"""
Adapter: makes LCD_2inch4 (Radxa periphery-based ILI9341) look like a
Waveshare LCD_1inch69 instance so that clawberry_display.py can use it
without any modification.

Interface contract satisfied (consumed by clawberry_display.py):

  lcd.width        → portrait width of the panel  (240)
  lcd.height       → portrait height of the panel (320)
  lcd.Init()       → full ILI9341 initialisation
  lcd.clear()      → fill display with white
  lcd.ShowImage(pil_image) → push an RGB PIL image to the panel
  lcd.bl_DutyCycle(duty)   → set backlight 0–100%
  lcd.module_exit()        → shutdown + close hardware (called by _shutdown())

Canvas convention used by clawberry_display.py:
  _LCD_LANDSCAPE = True  → landscape 320×240 (default)
  _LCD_LANDSCAPE = False → portrait  240×320
  _lcd_dims(disp) returns (disp.height, disp.width) in landscape mode.

The 2.4" LCD is 240×320 in portrait, so landscape yields 320×240 — the same
aspect ratio as the 1.69" LCD (280×240 landscape), just slightly wider.
"""

import logging
import sys
import os

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.realpath(__file__))
_LIB  = os.path.dirname(_HERE)    # lib/
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from radxa_epd.lcd2in4 import LCD_2inch4


class LCDRadxa24:
    """Waveshare-compatible wrapper around LCD_2inch4 for Radxa boards.

    Constructor accepts the same keyword arguments as LCD_2inch4 so
    pin assignments can be overridden from the detection code if needed.

    Example override::

        from radxa_epd.lcd_adapter import LCDRadxa24
        lcd = LCDRadxa24(rst_line=33, dc_line=110, bl_line=12)
        lcd.Init()
    """

    def __init__(self, **kwargs):
        self._lcd = LCD_2inch4(**kwargs)
        # Expose portrait geometry that clawberry_display.py expects.
        # In landscape mode, _lcd_dims() returns (height, width) = (320, 240).
        self.width  = self._lcd.width    # 240
        self.height = self._lcd.height   # 320

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def Init(self) -> None:
        """Full ILI9341 initialisation (called once at startup)."""
        self._lcd.init()

    def clear(self) -> None:
        """Fill display with white."""
        self._lcd.clear()

    def module_exit(self) -> None:
        """Display off + close hardware (called by clawberry_display._shutdown)."""
        self._lcd.Dev_exit()

    # ── Aliases expected by _shutdown() in clawberry_display.py ──────────────

    def sleep(self) -> None:
        self._lcd.sleep()

    def Dev_exit(self) -> None:
        self._lcd.Dev_exit()

    # ── Backlight ─────────────────────────────────────────────────────────────

    def bl_DutyCycle(self, duty: int) -> None:
        """Set backlight duty cycle 0–100 (percent)."""
        self._lcd.bl_DutyCycle(duty)

    def bl_Frequency(self, freq: int) -> None:
        """Set backlight PWM frequency in Hz."""
        self._lcd.bl_Frequency(freq)

    # ── Rendering ─────────────────────────────────────────────────────────────

    def ShowImage(self, pil_image) -> None:
        """Push a PIL RGB image to the LCD panel.

        Accepts either landscape (320×240) or portrait (240×320) and delegates
        orientation handling to the low-level driver.
        """
        self._lcd.display_image(pil_image)