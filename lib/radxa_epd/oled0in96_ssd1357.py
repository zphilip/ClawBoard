"""
Raw driver for the Waveshare 0.96inch RGB OLED Module on Radxa Cubie A7Z
(or any Linux SBC that exposes SPI/GPIO via the 'periphery' library).

Hardware:  SSD1357 controller, 64×128 pixels (portrait), 65K colours (RGB565)
Interface: 4-wire SPI — DIN, CLK, CS, DC, RST  (no BUSY pin)
Product:   https://www.waveshare.com/0.96inch-rgb-oled-module.htm

──────────────────────────────────────────────────────────────────────────────
Radxa Cubie A7Z  —  GPIO wiring recommendation
──────────────────────────────────────────────────────────────────────────────
The 1.54" e-ink already occupies SPI1 (/dev/spidev1.0) with GPIO lines
  RST=33 (Pin 11), DC=110 (Pin 26), BUSY=313 (Pin 18).
Use SPI0 and a separate pair of GPIO lines for this OLED:

  OLED pin  →  Radxa 40-pin header              GPIO line (gpiochip0)
  ────────────────────────────────────────────────────────────────────
  VCC       →  Pin  1  (3.3 V) or Pin  2 (5 V)  —
  GND       →  Pin  6  (GND)                     —
  DIN       →  Pin 19  (SPI0_MOSI)               —  (hardware SPI)
  CLK       →  Pin 23  (SPI0_SCLK)               —  (hardware SPI)
  CS        →  Pin 24  (SPI0_CS0)                —  (hardware SPI)  *or* any GPIO
  DC        →  Pin 22  (GPIO6_B4)                line 212  (default)
  RST       →  Pin 13  (GPIO4_B5)                line 141  (default)

  ⚠ Run  `gpioinfo gpiochip0 | grep -n ''`  to confirm line numbers
    on YOUR board revision.  Override via constructor kwargs.

SPI device:  /dev/spidev0.0  (SPI bus 0, chip-select 0)
SPI speed:   10 MHz  (SSD1357 max = 10 MHz @ 3.3 V)
"""

import time
import logging
import glob
import os

logger = logging.getLogger(__name__)


def _resolve_gpio(abs_line: int, preferred_chip: str = "/dev/gpiochip0"):
    """Map an absolute GPIO line number to (chip_path, offset_within_chip).

    On Raspberry Pi / single-chip boards the mapping is 1:1 (gpiochip0,
    abs_line).  On Allwinner / Rockchip SBCs each GPIO bank is a separate
    gpiochip; the absolute line number must be split across chips.

    Algorithm:
      1. Enumerate /dev/gpiochip* in numeric order.
      2. Read each chip's line count from
         /sys/bus/gpio/devices/gpiochip*/ngpio  (or via periphery).
      3. Subtract cumulatively until we find the chip that owns abs_line.

    Falls back to (preferred_chip, abs_line) if anything goes wrong.
    """
    try:
        # Collect (chip_path, ngpio) sorted by chip index
        chips = []
        for chip in sorted(glob.glob("/dev/gpiochip*"),
                           key=lambda p: int(p.replace("/dev/gpiochip", ""))):
            ngpio = None
            # Try sysfs first (no need to open the device)
            idx = chip.replace("/dev/gpiochip", "")
            for sysfs in glob.glob(f"/sys/bus/gpio/devices/gpiochip{idx}/ngpio"):
                try:
                    ngpio = int(open(sysfs).read().strip())
                    break
                except Exception:
                    pass
            if ngpio is None:
                # Fall back: open the chip with periphery and ask
                try:
                    from periphery import GPIO
                    _g = GPIO(chip, 0, "in")
                    ngpio = _g.chip_size()
                    _g.close()
                except Exception:
                    pass
            if ngpio is not None:
                chips.append((chip, ngpio))

        remaining = abs_line
        for chip_path, ngpio in chips:
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
OLED_WIDTH  = 64    # columns (portrait)
OLED_HEIGHT = 128   # rows    (portrait)

# ── Default hardware pins (Radxa Cubie A7Z; override via constructor kwargs) ──
_DEFAULT_RST_LINE  = 141           # GPIO line on /dev/gpiochip0  → Pin 13
_DEFAULT_DC_LINE   = 212           # GPIO line on /dev/gpiochip0  → Pin 22
_DEFAULT_SPI_DEV   = "/dev/spidev1.0"
_DEFAULT_SPI_SPEED = 10_000_000    # 10 MHz
_DEFAULT_GPIOCHIP  = "/dev/gpiochip0"


class OLED_0in96_SSD1357:
    """Low-level SSD1357 driver for the 0.96\" RGB OLED on Radxa / Linux SBC.

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
    ):
        from periphery import GPIO, SPI  # deferred — not available on Pi/host

        rst_chip, rst_off = _resolve_gpio(rst_line, gpiochip)
        dc_chip,  dc_off  = _resolve_gpio(dc_line,  gpiochip)
        logger.info(
            "Radxa OLED SSD1357: SPI=%s  RST=%s[%d]  DC=%s[%d]",
            spi_dev, rst_chip, rst_off, dc_chip, dc_off,
        )
        self.spi       = SPI(spi_dev, 0, spi_speed)
        self.rst_gpio  = GPIO(rst_chip, rst_off, "out")
        self.dc_gpio   = GPIO(dc_chip,  dc_off,  "out")

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
        """Hardware reset pulse: HIGH → LOW (≥3 µs) → HIGH."""
        self.rst_gpio.write(True);  time.sleep(0.1)
        self.rst_gpio.write(False); time.sleep(0.01)
        self.rst_gpio.write(True);  time.sleep(0.1)

    # ── Public API ────────────────────────────────────────────────────────────

    def init(self) -> None:
        """Full SSD1357 initialisation sequence.

        Must be called once after power-on (and after sleep() to wake up).
        Register values match Waveshare's reference firmware for the
        0.96\" RGB OLED Module.
        """
        self._hw_reset()

        self._cmd(0xFD); self._dat(0x12)   # Unlock all commands
        self._cmd(0xAE)                     # Display OFF

        self._cmd(0xCA); self._dat(0x7F)   # Multiplex ratio = 128 (all rows)
        self._cmd(0xA2); self._dat(0x00)   # Display offset  = 0
        self._cmd(0xA1); self._dat(0x00)   # Display start line = 0

        # Re-map & colour depth:
        #   bit 0   = 0 → column address increment left→right
        #   bit 1   = 1 → column address remapped (physical flip for module)
        #   bit 2   = 1 → BGR colour order (module is wired BGR)
        #   bit 4   = 1 → COM scan direction reversed
        #   bit 5   = 1 → COM split odd/even enabled
        #   bit 7:6 = 01 → 65K colours (RGB565, 16 bits/pixel)
        self._cmd(0xA0); self._dat(0x76)

        self._cmd(0xB5); self._dat(0x00)   # GPIO pins: HiZ (disabled)
        self._cmd(0xB3); self._dat(0xF1)   # Clock: divider=2, oscillator freq=0xF
        self._cmd(0xB1); self._dat(0x32)   # Phase length: phase1=5, phase2=3 DCLKs
        self._cmd(0xBB); self._dat(0x17)   # Pre-charge voltage
        self._cmd(0xB4); self._dat(0xA0); self._dat(0xFD)  # Seg low voltage
        self._cmd(0xBE); self._dat(0x05)   # VCOMH voltage
        self._cmd(0xBC); self._dat(0x05)   # Pre-charge 2
        self._cmd(0xB6); self._dat(0x01)   # Second pre-charge period

        # Contrast: R=200, G=128, B=200  (balanced white point)
        self._cmd(0xC1); self._dat(0xC8); self._dat(0x80); self._dat(0xC8)
        self._cmd(0xC7); self._dat(0x0F)   # Master contrast = max

        self._cmd(0xA4)    # Entire display: normal (not all-on)
        self._cmd(0xA6)    # Display mode: not inverted
        self._cmd(0xAF)    # Display ON

        logger.debug("Radxa OLED SSD1357: init complete")

    def set_window(self, x0: int = 0, y0: int = 0,
                   x1: int = OLED_WIDTH - 1, y1: int = OLED_HEIGHT - 1) -> None:
        """Set the active drawing window (column + row address ranges)."""
        self._cmd(0x15); self._dat(x0); self._dat(x1)   # columns
        self._cmd(0x75); self._dat(y0); self._dat(y1)   # rows

    def write_ram_cmd(self) -> None:
        """Issue 'write to RAM' command; follow with pixel data via _dat_bulk."""
        self._cmd(0x5C)

    def display_image_rgb565(self, buf: bytes) -> None:
        """Push a pre-encoded RGB565 frame buffer to the OLED.

        ``buf`` must be exactly ``width × height × 2`` bytes (little-endian
        RGB565, row-major, top-left origin in portrait orientation).
        """
        self.set_window(0, 0, self.width - 1, self.height - 1)
        self.write_ram_cmd()
        self._dat_bulk(buf)

    def display_image(self, pil_image) -> None:
        """Convert a PIL RGB image (64×128 portrait) and push it to the display.

        The image is resized to 64×128 if needed, then converted to RGB565.
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
