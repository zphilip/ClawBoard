"""
Raw driver for the 2.4" LCD display (ILI9341) on Radxa Cubie A7Z (or similar
boards that expose SPI/GPIO via the Linux periphery library instead of RPi.GPIO).

Pin mapping (defaults match the 1.54" e-paper wiring; override via constructor kwargs):
  RST  → gpiochip0 line 33   (Pin 11)
  DC   → gpiochip0 line 110  (Pin 26)
  BL   → gpiochip1 line 6    (Pin 13 — backlight on separate gpiochip)
  SPI  → /dev/spidev1.0 at 40 MHz

The ILI9341 LCD controller is a 240×320 RGB565 TFT panel.  All init sequences
are taken from the Waveshare LCD_2inch4 reference firmware.
"""

import time
import logging

logger = logging.getLogger(__name__)

# ── Display geometry ──────────────────────────────────────────────────────────
LCD_WIDTH  = 240
LCD_HEIGHT = 320

# ── Default hardware pins ─────────────────────────────────────────────────────
_DEFAULT_RST_LINE  = 33     # GPIO line number on /dev/gpiochip0
_DEFAULT_DC_LINE   = 110
_DEFAULT_BL_LINE   = 6      # backlight GPIO line on /dev/gpiochip1 (Pin 13)
_DEFAULT_BL_CHIP   = "/dev/gpiochip1"
_DEFAULT_SPI_DEV   = "/dev/spidev1.0"
_DEFAULT_SPI_SPEED = 40_000_000
_DEFAULT_GPIOCHIP  = "/dev/gpiochip0"


class LCD_2inch4:
    """Low-level driver for the 2.4\" ILI9341 LCD on Radxa / Linux SBC.

    Requires the ``periphery`` Python package (pip install python-periphery).
    """

    def __init__(
        self,
        rst_line:   int = _DEFAULT_RST_LINE,
        dc_line:    int = _DEFAULT_DC_LINE,
        bl_line:    int = _DEFAULT_BL_LINE,
        bl_chip:    str = _DEFAULT_BL_CHIP,
        spi_dev:    str = _DEFAULT_SPI_DEV,
        spi_speed:  int = _DEFAULT_SPI_SPEED,
        gpiochip:   str = _DEFAULT_GPIOCHIP,
    ):
        from periphery import GPIO, SPI  # deferred — not available on Pi

        logger.info(
            "Radxa LCD 2.4\" ILI9341: SPI=%s  RST=%d  DC=%d  BL=%s:%d",
            spi_dev, rst_line, dc_line, bl_chip, bl_line,
        )
        self.spi        = SPI(spi_dev, 0, spi_speed)
        self.reset_gpio = GPIO(gpiochip, rst_line, "out")
        self.dc_gpio    = GPIO(gpiochip, dc_line,  "out")
        self.bl_gpio    = GPIO(bl_chip, bl_line,  "out")

        self.width  = LCD_WIDTH    # 240
        self.height = LCD_HEIGHT   # 320

        # Enable backlight (GPIO on/off — no hardware PWM on this board)
        self._bl_duty = 100
        self.bl_gpio.write(True)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _write_pin(self, gpio, value: bool) -> None:
        gpio.write(value)

    def _send_command(self, command: int) -> None:
        self._write_pin(self.dc_gpio, False)
        self.spi.transfer([command])

    def _send_data(self, data: int) -> None:
        self._write_pin(self.dc_gpio, True)
        self.spi.transfer([data])

    def _send_data_bulk(self, data: bytes, chunk: int = 4096) -> None:
        """Send a byte sequence as SPI transactions, split into chunks.

        The Linux SPI kernel buffer is typically 4096 bytes; sending more in a
        single ioctl raises EMSGSIZE (errno 90).  We split into ≤chunk-byte
        transfers to stay within the limit.
        """
        self._write_pin(self.dc_gpio, True)
        for i in range(0, len(data), chunk):
            self.spi.transfer(list(data[i:i + chunk]))

    def _hw_reset(self) -> None:
        self._write_pin(self.reset_gpio, True);  time.sleep(0.01)
        self._write_pin(self.reset_gpio, False); time.sleep(0.01)
        self._write_pin(self.reset_gpio, True);  time.sleep(0.01)

    # ── Backlight control ─────────────────────────────────────────────────────

    def bl_DutyCycle(self, duty: int) -> None:
        """Set backlight duty cycle 0–100 (percent).

        Since this board lacks hardware PWM, duty > 0 turns the backlight ON
        and duty == 0 turns it OFF.  The stored value is preserved for
        compatibility with callers that expect PWM granularity.
        """
        self._bl_duty = duty
        self.bl_gpio.write(duty > 0)

    def bl_Frequency(self, freq: int) -> None:
        """No-op — no hardware PWM on this board."""

    # ── Public API ────────────────────────────────────────────────────────────

    def init(self) -> None:
        """Full ILI9341 initialisation sequence."""
        self._hw_reset()

        self._send_command(0x11)   # Sleep out
        time.sleep(0.12)

        self._send_command(0xCF)
        self._send_data(0x00)
        self._send_data(0xC1)
        self._send_data(0x30)

        self._send_command(0xED)
        self._send_data(0x64)
        self._send_data(0x03)
        self._send_data(0x12)
        self._send_data(0x81)

        self._send_command(0xE8)
        self._send_data(0x85)
        self._send_data(0x00)
        self._send_data(0x79)

        self._send_command(0xCB)
        self._send_data(0x39)
        self._send_data(0x2C)
        self._send_data(0x00)
        self._send_data(0x34)
        self._send_data(0x02)

        self._send_command(0xF7)
        self._send_data(0x20)

        self._send_command(0xEA)
        self._send_data(0x00)
        self._send_data(0x00)

        self._send_command(0xC0)   # Power control
        self._send_data(0x1D)      # VRH[5:0]

        self._send_command(0xC1)   # Power control
        self._send_data(0x12)      # SAP[2:0]; BT[3:0]

        self._send_command(0xC5)   # VCM control
        self._send_data(0x33)
        self._send_data(0x3F)

        self._send_command(0xC7)   # VCM control
        self._send_data(0x92)

        self._send_command(0x3A)   # Pixel format: 16-bit RGB565
        self._send_data(0x55)

        self._send_command(0x36)   # Memory Access Control
        self._send_data(0x08)

        self._send_command(0xB1)
        self._send_data(0x00)
        self._send_data(0x12)

        self._send_command(0xB6)   # Display Function Control
        self._send_data(0x0A)
        self._send_data(0xA2)

        self._send_command(0x44)
        self._send_data(0x02)

        self._send_command(0xF2)   # 3Gamma Function Disable
        self._send_data(0x00)

        self._send_command(0x26)   # Gamma curve selected
        self._send_data(0x01)

        self._send_command(0xE0)   # Set Gamma (positive)
        self._send_data(0x0F)
        self._send_data(0x22)
        self._send_data(0x1C)
        self._send_data(0x1B)
        self._send_data(0x08)
        self._send_data(0x0F)
        self._send_data(0x48)
        self._send_data(0xB8)
        self._send_data(0x34)
        self._send_data(0x05)
        self._send_data(0x0C)
        self._send_data(0x09)
        self._send_data(0x0F)
        self._send_data(0x07)
        self._send_data(0x00)

        self._send_command(0xE1)   # Set Gamma (negative)
        self._send_data(0x00)
        self._send_data(0x23)
        self._send_data(0x24)
        self._send_data(0x07)
        self._send_data(0x10)
        self._send_data(0x07)
        self._send_data(0x38)
        self._send_data(0x47)
        self._send_data(0x4B)
        self._send_data(0x0A)
        self._send_data(0x13)
        self._send_data(0x06)
        self._send_data(0x30)
        self._send_data(0x38)
        self._send_data(0x0F)

        self._send_command(0x29)   # Display on

        logger.debug("Radxa LCD 2.4\" ILI9341: init complete")

    def SetWindows(self, Xstart: int, Ystart: int, Xend: int, Yend: int) -> None:
        """Set the column/page address window for subsequent writes."""
        # Column address
        self._send_command(0x2A)
        self._send_data(Xstart >> 8)
        self._send_data(Xstart & 0xFF)
        self._send_data((Xend - 1) >> 8)
        self._send_data((Xend - 1) & 0xFF)

        # Page address
        self._send_command(0x2B)
        self._send_data(Ystart >> 8)
        self._send_data(Ystart & 0xFF)
        self._send_data((Yend - 1) >> 8)
        self._send_data((Yend - 1) & 0xFF)

        self._send_command(0x2C)

    def clear(self) -> None:
        """Fill display with white."""
        buf = b'\xFF\xFF' * (self.width * self.height)
        time.sleep(0.02)
        self.SetWindows(0, 0, self.width, self.height)
        self._send_data_bulk(buf)

    def clear_color(self, color: int) -> None:
        """Fill display with a 16-bit RGB565 colour."""
        buf = bytes([color >> 8, color & 0xFF]) * (self.width * self.height)
        time.sleep(0.02)
        self.SetWindows(0, 0, self.width, self.height)
        self._send_data_bulk(buf)

    def display_image(self, pil_image) -> None:
        """Push a PIL RGB image to the display.

        Accepts either portrait (240×320) or landscape (320×240) images and
        automatically sets the MADCTL register for correct orientation.

        When MADCTL swaps X/Y axes (MV=1, landscape mode), the GRAM expects
        data in column-major order (X-major).  We transpose the numpy array
        before flattening so the SPI byte stream matches what the controller
        reads row-by-row through the swapped coordinate window.
        """
        imwidth, imheight = pil_image.size

        # Convert RGB888 → RGB565
        import numpy as np
        img = np.asarray(pil_image)
        pix = np.zeros((imheight, imwidth, 2), dtype=np.uint8)
        pix[..., [0]] = np.add(
            np.bitwise_and(img[..., [0]], 0xF8),
            np.right_shift(img[..., [1]], 5),
        )
        pix[..., [1]] = np.add(
            np.bitwise_and(np.left_shift(img[..., [1]], 3), 0xE0),
            np.right_shift(img[..., [2]], 3),
        )

        if imwidth == self.height and imheight == self.width:
            # Landscape image (320×240) → rotate to fit portrait panel
            # MADCTL: MV=1 (swap X/Y), MX=0 (no column reversal), BGR=1
            self._send_command(0x36)
            self._send_data(0x28)   # MY=0 MX=0 MV=1 ML=0 BGR=1 MH=0
            self.SetWindows(0, 0, self.width, self.height)
            # With MV=1 the GRAM walks X-major — transpose to (320,240,2)
            # so that C-order flatten produces column-major pixel data.
            pix = np.transpose(pix, (1, 0, 2)).flatten().tolist()
        else:
            # Portrait image (240×320) — C-order flatten is correct
            self._send_command(0x36)
            self._send_data(0x08)   # MV=0: no axis swap
            self.SetWindows(0, 0, self.width, self.height)
            pix = pix.flatten().tolist()

        self._send_data_bulk(bytes(pix))

    def sleep(self) -> None:
        """Put the display controller into sleep mode (low power).

        Keeps SPI and GPIO handles open so the display can be woken up again
        with init() without needing to re-open hardware resources.
        """
        self._send_command(0x10)   # Sleep in
        time.sleep(0.01)

    def Dev_exit(self) -> None:
        """Full shutdown: sleep + close all hardware handles.

        Called by clawberry_display.py _shutdown() when the service stops.
        """
        self._send_command(0x10)   # Sleep in
        time.sleep(0.01)
        self._release()

    def _release(self) -> None:
        """Close all periphery handles (safe to call multiple times)."""
        self.bl_gpio.close()
        for handle in (self.spi, self.reset_gpio, self.dc_gpio):
            try:
                handle.close()
            except Exception:
                pass