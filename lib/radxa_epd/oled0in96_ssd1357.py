"""
Raw driver for the Waveshare 0.96inch RGB OLED Module on Radxa Cubie A7Z
(or any Linux SBC that exposes SPI/GPIO via the 'periphery' library).

Hardware:  SSD1357 controller, 128×128 pixels, 65K colours (RGB565)
Interface: 4-wire SPI — DIN, CLK, CS, DC, RST  (no BUSY pin)
Product:   https://www.waveshare.com/0.96inch-rgb-oled-module.htm

──────────────────────────────────────────────────────────────────────────────
Radxa Cubie A7Z  —  GPIO wiring recommendation
──────────────────────────────────────────────────────────────────────────────
The 1.54" e-ink already occupies SPI1 (/dev/spidev1.0) with GPIO lines
  RST=33 (Pin 11), DC=110 (Pin 26), BUSY=313 (Pin 18).
Use SPI0 and a separate pair of GPIO lines for this OLED:

  OLED pin  →  Radxa 40-pin header              GPIO
  ────────────────────────────────────────────────────────────────────
  VCC       →  Pin  1  (3.3 V) or Pin  2 (5 V)  —
  GND       →  Pin  6  (GND)                     —
  DIN       →  Pin 19  (SPI1_MOSI)               —  (hardware SPI)
  CLK       →  Pin 23  (SPI1_SCLK)               —  (hardware SPI)
  CS        →  Pin 24  (SPI1_CS0)                —  (hardware SPI)
  DC        →  Pin 22  (gpiochip1 line 5)        default
  RST       →  Pin 15  (gpiochip1 line 7)        default

  ⚠ Run  `gpioinfo gpiochip1 | grep -n ''`  to confirm line numbers
    on YOUR board revision.  Override via constructor kwargs.

SPI device:  /dev/spidev1.0
SPI speed:   2 MHz  (conservative; increase to 10 MHz if signal is clean)
"""

import time
import logging
import glob
import struct
import fcntl

logger = logging.getLogger(__name__)

# GPIO_GET_CHIPINFO_IOCTL = _IOR(0xB4, 0x01, struct gpiochip_info)
# struct gpiochip_info { char name[32]; char label[32]; __u32 lines; }  → 68 bytes
_GPIO_GET_CHIPINFO_IOCTL = 0x8044B401


def _chip_ngpio(chip_path: str) -> int:
    """Return the number of GPIO lines on *chip_path* via kernel ioctl."""
    with open(chip_path, 'rb') as f:
        buf = fcntl.ioctl(f, _GPIO_GET_CHIPINFO_IOCTL, b'\x00' * 68)
    return struct.unpack_from('<I', buf, 64)[0]


def _resolve_gpio(abs_line: int, preferred_chip: str = "/dev/gpiochip0"):
    """Map an absolute GPIO line number to (chip_path, offset_within_chip).

    On Raspberry Pi / single-chip boards the mapping is 1:1 (gpiochip0,
    abs_line).  On Allwinner / Rockchip SBCs each GPIO bank is a separate
    gpiochip; the absolute line number must be split across chips.

    Uses GPIO_GET_CHIPINFO_IOCTL to read ngpio directly from each chip
    device — no sysfs path assumptions, works on all kernel versions.

    Falls back to (preferred_chip, abs_line) if anything goes wrong.
    """
    try:
        chips = sorted(
            glob.glob("/dev/gpiochip*"),
            key=lambda p: int(p.replace("/dev/gpiochip", "")),
        )
        remaining = abs_line
        for chip_path in chips:
            try:
                ngpio = _chip_ngpio(chip_path)
            except Exception:
                continue
            if remaining < ngpio:
                if chip_path != preferred_chip or remaining != abs_line:
                    logger.info(
                        "GPIO line %d resolved to %s offset %d",
                        abs_line, chip_path, remaining,
                    )
                return chip_path, remaining
            remaining -= ngpio
    except Exception as exc:
        logger.debug("_resolve_gpio auto-detect failed: %s", exc)

    return preferred_chip, abs_line


# ── Display geometry ──────────────────────────────────────────────────────────
# The SSD1357 GRAM is 128×128.  The full panel is used.
OLED_WIDTH  = 128   # columns
OLED_HEIGHT = 128   # rows

# ── Default hardware pins (Radxa Cubie A7Z; override via constructor kwargs) ──
# RST → Pin 15  (gpiochip1, line 7)
# DC  → Pin 22  (gpiochip1, line 5)
_DEFAULT_RST_LINE  = 7             # line within gpiochip1
_DEFAULT_DC_LINE   = 5             # line within gpiochip1
_DEFAULT_SPI_DEV   = "/dev/spidev1.0"
_DEFAULT_SPI_SPEED = 2_000_000     # 2 MHz (conservative; matches confirmed-working test)
_DEFAULT_GPIOCHIP  = "/dev/gpiochip1"


class OLED_0in96_SSD1357:
    """Low-level SSD1357 driver for the 0.96\" RGB OLED (128×128) on Radxa / Linux SBC.

    Requires the ``periphery`` Python package (pip install python-periphery).

    All methods that send data to the display expect or return raw bytes;
    higher-level image conversion lives in oled_adapter.py.
    """

    def __init__(
        self,
        rst_line:  int = _DEFAULT_RST_LINE,
        dc_line:   int = _DEFAULT_DC_LINE,
        spi_dev:   str = _DEFAULT_SPI_DEV,
        spi_speed: int = _DEFAULT_SPI_SPEED,
        gpiochip:  str = _DEFAULT_GPIOCHIP,
        # Explicit chip overrides — when set, bypass _resolve_gpio entirely.
        # Use these when the GPIO lines live on a specific chip (e.g. gpiochip1)
        # and auto-resolution would incorrectly map them to gpiochip0.
        rst_chip:  str = None,
        dc_chip:   str = None,
    ):
        from periphery import GPIO, SPI  # deferred — not available on Pi/host

        # Use explicit chip paths when provided; otherwise auto-resolve.
        if rst_chip is not None:
            _rst_chip, _rst_off = rst_chip, rst_line
        else:
            _rst_chip, _rst_off = _resolve_gpio(rst_line, gpiochip)

        if dc_chip is not None:
            _dc_chip, _dc_off = dc_chip, dc_line
        else:
            _dc_chip, _dc_off = _resolve_gpio(dc_line, gpiochip)

        logger.info(
            "Radxa OLED SSD1357: SPI=%s  RST=%s[%d]  DC=%s[%d]",
            spi_dev, _rst_chip, _rst_off, _dc_chip, _dc_off,
        )
        self.spi       = SPI(spi_dev, 0, spi_speed)
        self.rst_gpio  = GPIO(_rst_chip, _rst_off, "out")
        self.dc_gpio   = GPIO(_dc_chip,  _dc_off,  "out")

        self.width  = OLED_WIDTH
        self.height = OLED_HEIGHT

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _cmd(self, command: int) -> None:
        """Send a single command byte (DC=LOW)."""
        self.dc_gpio.write(False)
        self.spi.transfer([command])

    def _dat(self, data: int) -> None:
        """Send a single data byte (DC=HIGH)."""
        self.dc_gpio.write(True)
        self.spi.transfer([data])

    def _dat_bulk(self, data: bytes, chunk: int = 4096) -> None:
        """Send a byte buffer as SPI transactions, split at kernel chunk limit."""
        self.dc_gpio.write(True)
        for i in range(0, len(data), chunk):
            self.spi.transfer(list(data[i:i + chunk]))

    def _hw_reset(self) -> None:
        """Hardware reset pulse: HIGH → LOW (1 s minimum) → HIGH.

        The long LOW phase ensures the SSD1357 fully discharges its internal
        capacitors on cold boot.  A short pulse (≤10 ms) leaves the controller
        in an undefined state causing blank / noisy output.
        """
        self.rst_gpio.write(True);  time.sleep(0.1)
        self.rst_gpio.write(False); time.sleep(1.0)   # ≥1 s — do NOT shorten
        self.rst_gpio.write(True);  time.sleep(0.5)

    # ── Public API ────────────────────────────────────────────────────────────

    def init(self) -> None:
        """Full SSD1357 initialisation sequence.

        Must be called once after power-on (and after sleep() to wake up).
        Register values are derived from the confirmed-working test sequence
        for the Waveshare 0.96\" RGB OLED on Radxa Cubie A7Z.
        """
        self._hw_reset()

        self._cmd(0xFD); self._dat(0x12)   # Unlock all commands
        self._cmd(0xAE)                     # Display OFF

        self._cmd(0xCA); self._dat(0x7F)   # Multiplex ratio = 128 (all rows active)

        # Re-map & colour depth (0xA0):
        #   bit 0   = 0 → column address increments left→right
        #   bit 1   = 1 → column address remapped (SEG 127→0, physical flip)
        #   bit 2   = 0 → RGB colour order  (NOT BGR — 0x76 caused colour swap)
        #   bit 4   = 1 → COM scan direction reversed
        #   bit 5   = 1 → COM split odd/even enabled
        #   bit 7:6 = 01 → 65K colours (RGB565, 16 bits/pixel)
        # Result: 0b_01_1_1_0_0_1_0 = 0x72
        # To flip image vertically, change bit4: 0x72 ↔ 0x62
        self._cmd(0xA0); self._dat(0x72)

        # Full 128×128 GRAM window (must match OLED_WIDTH × OLED_HEIGHT)
        self._cmd(0x15); self._dat(0x00); self._dat(0x7F)   # columns 0–127
        self._cmd(0x75); self._dat(0x00); self._dat(0x7F)   # rows    0–127

        self._cmd(0xA2); self._dat(0x00)   # Display offset   = 0 (no row shift)
        self._cmd(0xA1); self._dat(0x00)   # Display start line = 0

        # Contrast: equal R/G/B for a neutral white point
        self._cmd(0xC1); self._dat(0x8F); self._dat(0x8F); self._dat(0x8F)
        self._cmd(0xC7); self._dat(0x0F)   # Master contrast = max

        self._cmd(0xAF)    # Display ON

        logger.debug("Radxa OLED SSD1357: init complete")

    def set_window(self, x0: int = 0, y0: int = 0,
                   x1: int = OLED_WIDTH - 1, y1: int = OLED_HEIGHT - 1) -> None:
        """Set the active GRAM drawing window (column + row address ranges).

        Defaults cover the full 128×128 panel.
        """
        self._cmd(0x15); self._dat(x0); self._dat(x1)   # columns
        self._cmd(0x75); self._dat(y0); self._dat(y1)   # rows

    def write_ram_cmd(self) -> None:
        """Issue 'write to RAM' command; follow with pixel data via _dat_bulk."""
        self._cmd(0x5C)

    def display_image_rgb565(self, buf: bytes) -> None:
        """Push a pre-encoded RGB565 frame buffer to the OLED.

        ``buf`` must be exactly ``width × height × 2`` bytes (big-endian
        RGB565, row-major, top-left origin).  For this 128×128 panel that
        is 128 × 128 × 2 = 32768 bytes.
        """
        self.set_window(0, 0, self.width - 1, self.height - 1)
        self.write_ram_cmd()
        self._dat_bulk(buf)

    def display_image(self, pil_image) -> None:
        """Convert a PIL RGB image (128×128) and push it to the display.

        The image is resized to 128×128 if needed, then converted to RGB565.
        Pixel byte order: big-endian per SSD1357 spec (high byte first).
        """
        img = pil_image.convert('RGB')
        if img.size != (self.width, self.height):
            img = img.resize((self.width, self.height))

        buf = bytearray(self.width * self.height * 2)
        idx = 0
        for pixel in img.getdata():
            r, g, b = pixel
            # RGB565: RRRRRGGGGGGBBBBB (big-endian)
            rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            buf[idx]     = (rgb565 >> 8) & 0xFF   # high byte
            buf[idx + 1] = rgb565 & 0xFF           # low byte
            idx += 2
        self.display_image_rgb565(bytes(buf))

    def clear(self, r: int = 0, g: int = 0, b: int = 0) -> None:
        """Fill the entire display with a solid colour (default black)."""
        rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        hi, lo = (rgb565 >> 8) & 0xFF, rgb565 & 0xFF
        buf = bytes([hi, lo]) * (self.width * self.height)
        self.set_window()
        self.write_ram_cmd()
        self._dat_bulk(buf)

    def sleep(self) -> None:
        """Turn the display panel off (low-power standby).

        SPI/GPIO handles remain open so ``init()`` can wake the display
        without re-allocating hardware resources.
        """
        self._cmd(0xAE)    # Display OFF
        time.sleep(0.05)

    def Dev_exit(self) -> None:
        """Display off + close all hardware handles (called at service shutdown)."""
        try:
            self._cmd(0xAE)
            time.sleep(0.05)
        except Exception:
            pass
        self._release()

    def _release(self) -> None:
        """Close all periphery handles (safe to call multiple times)."""
        for handle in (self.spi, self.rst_gpio, self.dc_gpio):
            try:
                handle.close()
            except Exception:
                pass
