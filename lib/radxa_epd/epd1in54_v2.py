"""
Raw driver for the 1.54" e-paper display on Radxa Cubie A7Z (or similar boards
that expose SPI/GPIO via the Linux periphery library instead of RPi.GPIO).

Pin mapping (defaults match the test wiring; override via constructor kwargs):
  RST  → gpiochip0 line 33   (Pin 11)
  DC   → gpiochip0 line 110  (Pin 26)
  BUSY → gpiochip0 line 313  (Pin 18)
  SPI  → /dev/spidev1.0 at 4 MHz

All timings and register sequences are taken from the Waveshare EPD 1.54" V2
datasheet / reference firmware.
"""

import time
import logging

logger = logging.getLogger(__name__)

# ── Display geometry ──────────────────────────────────────────────────────────
EPD_WIDTH  = 200
EPD_HEIGHT = 200

# ── Default hardware pins ─────────────────────────────────────────────────────
_DEFAULT_RST_LINE  = 33    # GPIO line number on /dev/gpiochip0
_DEFAULT_DC_LINE   = 110
_DEFAULT_BUSY_LINE = 313
_DEFAULT_SPI_DEV   = "/dev/spidev1.0"
_DEFAULT_SPI_SPEED = 4_000_000
_DEFAULT_GPIOCHIP  = "/dev/gpiochip0"


class EPD_1in54_V2:
    """Low-level driver for the 1.54\" e-paper V2 display on Radxa / Linux SBC.

    Requires the ``periphery`` Python package (pip install python-periphery).
    """

    def __init__(
        self,
        rst_line:   int = _DEFAULT_RST_LINE,
        dc_line:    int = _DEFAULT_DC_LINE,
        busy_line:  int = _DEFAULT_BUSY_LINE,
        spi_dev:    str = _DEFAULT_SPI_DEV,
        spi_speed:  int = _DEFAULT_SPI_SPEED,
        gpiochip:   str = _DEFAULT_GPIOCHIP,
    ):
        from periphery import GPIO, SPI  # deferred — not available on Pi

        logger.info(
            "Radxa EPD 1.54\" V2: SPI=%s  RST=%d  DC=%d  BUSY=%d",
            spi_dev, rst_line, dc_line, busy_line,
        )
        self.spi        = SPI(spi_dev, 0, spi_speed)
        self.reset_gpio = GPIO(gpiochip, rst_line, "out")
        self.dc_gpio    = GPIO(gpiochip, dc_line,  "out")
        self.busy_gpio  = GPIO(gpiochip, busy_line, "in")

        self.width  = EPD_WIDTH
        self.height = EPD_HEIGHT

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _write_pin(self, gpio, value: bool) -> None:
        gpio.write(value)

    def _send_command(self, command: int) -> None:
        self._write_pin(self.dc_gpio, False)
        self.spi.transfer([command])

    def _send_data(self, data: int) -> None:
        self._write_pin(self.dc_gpio, True)
        self.spi.transfer([data])

    def _send_data_bulk(self, data: bytes) -> None:
        """Send a byte sequence as a single SPI transaction (faster than looping)."""
        self._write_pin(self.dc_gpio, True)
        # periphery SPI.transfer accepts a list of ints
        self.spi.transfer(list(data))

    def _wait_busy(self, timeout: float = 5.0) -> None:
        """Block until BUSY pin goes LOW (display ready), or timeout."""
        start = time.time()
        while self.busy_gpio.read():   # HIGH == busy
            time.sleep(0.01)
            if time.time() - start > timeout:
                logger.warning("Radxa EPD: BUSY timeout after %.1f s", timeout)
                break

    def _hw_reset(self) -> None:
        self._write_pin(self.reset_gpio, True);  time.sleep(0.1)
        self._write_pin(self.reset_gpio, False); time.sleep(0.01)
        self._write_pin(self.reset_gpio, True);  time.sleep(0.1)

    # ── Public API ────────────────────────────────────────────────────────────

    def init(self) -> None:
        """Full initialisation sequence (use after power-on or sleep)."""
        self._hw_reset()
        self._wait_busy()

        self._send_command(0x12)   # SW reset
        self._wait_busy()

        # Driver output control: 199 gates, 0 offset, GD/SM/TB=0
        self._send_command(0x01)
        self._send_data(0xC7); self._send_data(0x00); self._send_data(0x00)

        # Data entry mode: Y increment, X increment
        self._send_command(0x11); self._send_data(0x03)

        # Set RAM X address range: 0x00 – 0x18 (cols 0–199 / 8 = 25 bytes)
        self._send_command(0x44); self._send_data(0x00); self._send_data(0x18)

        # Set RAM Y address range: 0x0000 – 0x00C7 (rows 0–199)
        self._send_command(0x45)
        self._send_data(0x00); self._send_data(0x00)
        self._send_data(0xC7); self._send_data(0x00)

        # Border waveform: follow LUT (VBD = 0x01)
        self._send_command(0x3C); self._send_data(0x01)

        # Temperature sensor: use internal sensor
        self._send_command(0x18); self._send_data(0x80)

        # Load waveform LUT from OTP
        self._send_command(0x22); self._send_data(0xB1)
        self._send_command(0x20)
        self._wait_busy()

        logger.debug("Radxa EPD 1.54\" V2: init complete")

    def init_fast(self) -> None:
        """Fast-refresh init (re-uses the same sequence; display handles LUT internally)."""
        self.init()

    def getbuffer(self, image) -> bytes:
        """Convert a PIL image to the packed 1-bit buffer expected by display()."""
        img = image.convert('1')
        w, h = img.size
        # Ensure the image matches the display geometry
        if (w, h) != (self.width, self.height):
            img = img.resize((self.width, self.height))

        buf = bytearray(b'\xFF' * (self.width * self.height // 8))
        for y in range(self.height):
            for x in range(self.width):
                if img.getpixel((x, y)) == 0:   # black pixel
                    buf[(x >> 3) + y * (self.width >> 3)] &= ~(0x80 >> (x & 7))
        return bytes(buf)

    def display(self, image_buffer: bytes) -> None:
        """Push a packed 1-bit buffer to RAM and trigger a full refresh."""
        # Set cursor to (0, 0)
        self._send_command(0x4E); self._send_data(0x00)
        self._send_command(0x4F); self._send_data(0x00); self._send_data(0x00)

        self._send_command(0x24)
        self._send_data_bulk(image_buffer)

        # Master activation with full refresh sequence
        self._send_command(0x22); self._send_data(0xF7)
        self._send_command(0x20)
        self._wait_busy()

    def display_fast(self, image_buffer: bytes) -> None:
        """Fast-refresh path — falls back to full refresh (hardware limitation)."""
        self.display(image_buffer)

    def clear(self, color: int = 0xFF) -> None:
        """Fill the display with a solid colour (0xFF = white, 0x00 = black)."""
        buf = bytes([color]) * (self.width * self.height // 8)
        self.display(buf)

    def sleep(self) -> None:
        """Put the display controller into deep sleep and release hardware resources."""
        self._send_command(0x10); self._send_data(0x01)
        time.sleep(0.1)
        self._release()

    def Dev_exit(self) -> None:
        """Alias for sleep() — matches the interface used by clawberry_display.py."""
        self.sleep()

    def _release(self) -> None:
        """Close all periphery handles (safe to call multiple times)."""
        for handle in (self.spi, self.reset_gpio, self.dc_gpio, self.busy_gpio):
            try:
                handle.close()
            except Exception:
                pass
