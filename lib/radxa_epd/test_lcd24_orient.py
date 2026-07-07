#!/usr/bin/env python3
"""
Quick MADCTL orientation test — cycles through values to find correct setting.
Displays an asymmetric arrow pattern so mirror/flip is obvious.

Usage:
    sudo python3 test_lcd24_orient.py
"""

import sys, os, time
import numpy as np

_HERE = os.path.dirname(os.path.realpath(__file__))
_LIB  = os.path.dirname(_HERE)
sys.path.insert(0, _LIB)

from periphery import GPIO, SPI
from PIL import Image, ImageDraw, ImageFont

RST_CHIP, RST_LINE = "/dev/gpiochip0", 33
DC_CHIP,  DC_LINE  = "/dev/gpiochip0", 110
BL_CHIP,  BL_LINE  = "/dev/gpiochip1", 6
SPI_DEV   = "/dev/spidev1.0"
SPI_SPEED = 40_000_000

FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'


def init_lcd(rst, dc, bl, spi):
    bl.write(True)

    def wcmd(c):
        dc.write(False); spi.transfer([c])

    def wdata(d):
        dc.write(True)
        if isinstance(d, int):
            spi.transfer([d])
        else:
            for i in range(0, len(d), 4096):
                spi.transfer(d[i:i + 4096])

    rst.write(True); time.sleep(0.01)
    rst.write(False); time.sleep(0.01)
    rst.write(True); time.sleep(0.01)

    wcmd(0x11); time.sleep(0.12)
    wcmd(0xCF); wdata(0x00); wdata(0xC1); wdata(0x30)
    wcmd(0xED); wdata(0x64); wdata(0x03); wdata(0x12); wdata(0x81)
    wcmd(0xE8); wdata(0x85); wdata(0x00); wdata(0x79)
    wcmd(0xCB); wdata(0x39); wdata(0x2C); wdata(0x00); wdata(0x34); wdata(0x02)
    wcmd(0xF7); wdata(0x20)
    wcmd(0xEA); wdata(0x00); wdata(0x00)
    wcmd(0xC0); wdata(0x1D)
    wcmd(0xC1); wdata(0x12)
    wcmd(0xC5); wdata(0x33); wdata(0x3F)
    wcmd(0xC7); wdata(0x92)
    wcmd(0x3A); wdata(0x55)
    wcmd(0x36); wdata(0x08)
    wcmd(0xB1); wdata(0x00); wdata(0x12)
    wcmd(0xB6); wdata(0x0A); wdata(0xA2)
    wcmd(0x44); wdata(0x02)
    wcmd(0xF2); wdata(0x00)
    wcmd(0x26); wdata(0x01)
    wcmd(0xE0)
    for b in [0x0F,0x22,0x1C,0x1B,0x08,0x0F,0x48,0xB8,0x34,0x05,0x0C,0x09,0x0F,0x07,0x00]:
        wdata(b)
    wcmd(0xE1)
    for b in [0x00,0x23,0x24,0x07,0x10,0x07,0x38,0x47,0x4B,0x0A,0x13,0x06,0x30,0x38,0x0F]:
        wdata(b)
    wcmd(0x29)

    return wcmd, wdata


def set_win(wcmd, wdata, xs, ys, xe, ye):
    wcmd(0x2A)
    wdata(xs >> 8); wdata(xs & 0xFF)
    wdata((xe - 1) >> 8); wdata((xe - 1) & 0xFF)
    wcmd(0x2B)
    wdata(ys >> 8); wdata(ys & 0xFF)
    wdata((ye - 1) >> 8); wdata((ye - 1) & 0xFF)
    wcmd(0x2C)


def send_landscape(wcmd, wdata, image, madctl):
    """Send a 320×240 landscape image with the given MADCTL."""
    mv = (madctl >> 5) & 1

    if mv:
        # MV=1: X/Y swapped by controller.
        # Image is 320×240 → GRAM expects 240 cols × 320 rows.
        # We transpose data to X-major order.
        img = np.asarray(image)   # shape (240, 320, 3)
        pix = np.zeros((240, 320, 2), dtype=np.uint8)
        pix[..., [0]] = np.add(np.bitwise_and(img[..., [0]], 0xF8), np.right_shift(img[..., [1]], 5))
        pix[..., [1]] = np.add(np.bitwise_and(np.left_shift(img[..., [1]], 3), 0xE0), np.right_shift(img[..., [2]], 3))
        pix = np.transpose(pix, (1, 0, 2)).flatten().tolist()
        wcmd(0x36); wdata(madctl)
        set_win(wcmd, wdata, 0, 0, 240, 320)
    else:
        # MV=0: no axis swap. Send as-is with window matching image.
        img = np.asarray(image)   # shape (240, 320, 3)
        pix = np.zeros((240, 320, 2), dtype=np.uint8)
        pix[..., [0]] = np.add(np.bitwise_and(img[..., [0]], 0xF8), np.right_shift(img[..., [1]], 5))
        pix[..., [1]] = np.add(np.bitwise_and(np.left_shift(img[..., [1]], 3), 0xE0), np.right_shift(img[..., [2]], 3))
        pix = pix.flatten().tolist()
        wcmd(0x36); wdata(madctl)
        set_win(wcmd, wdata, 0, 0, 320, 240)

    wcmd(0x2C)
    wdata(pix)


def make_pattern():
    """Draw an asymmetric test pattern that reveals mirror/flip."""
    W, H = 320, 240
    img = Image.new('RGB', (W, H), (30, 30, 30))
    draw = ImageDraw.Draw(img)

    try:
        f_big = ImageFont.truetype(FONT, 30)
        f_med = ImageFont.truetype(FONT, 18)
    except OSError:
        f_big = ImageFont.load_default()
        f_med = ImageFont.load_default()

    # Colored corner markers
    draw.rectangle((0, 0, 30, 30), fill=(255, 0, 0))          # TL: RED
    draw.rectangle((W - 30, 0, W, 30), fill=(0, 255, 0))      # TR: GREEN
    draw.rectangle((0, H - 30, 30, H), fill=(0, 0, 255))      # BL: BLUE
    draw.rectangle((W - 30, H - 30, W, H), fill=(255, 255, 0))# BR: YELLOW

    # Asymmetric arrow shape (points right)
    cx, cy = W // 2, H // 2
    # Arrow shaft
    draw.rectangle((cx - 60, cy - 15, cx + 20, cy + 15), fill=(255, 255, 255))
    # Arrow head (points RIGHT)
    draw.polygon([(cx + 20, cy - 40), (cx + 70, cy), (cx + 20, cy + 40)],
                 fill=(255, 255, 255))
    # Small dot on LEFT side
    draw.ellipse((cx - 80, cy - 5, cx - 70, cy + 5), fill=(255, 0, 0))

    # Text
    draw.text((10, 40), "TOP-LEFT", font=f_med, fill=(255, 255, 255))
    draw.text((W - 120, 40), "TOP-RIGHT", font=f_med, fill=(255, 255, 255))
    draw.text((10, H - 30), "BOT-LEFT", font=f_med, fill=(255, 255, 255))
    draw.text((W - 130, H - 30), "BOT-RIGHT", font=f_med, fill=(255, 255, 255))

    return img


def main():
    print("=== MADCTL Orientation Finder ===")
    print("Expected: RED=TL, GREEN=TR, BLUE=BL, YELLOW=BR, arrow points RIGHT\n")

    rst = GPIO(RST_CHIP, RST_LINE, "out")
    dc  = GPIO(DC_CHIP,  DC_LINE,  "out")
    bl  = GPIO(BL_CHIP,  BL_LINE,  "out")
    spi = SPI(SPI_DEV, 0, SPI_SPEED)

    try:
        wcmd, wdata = init_lcd(rst, dc, bl, spi)
        pattern = make_pattern()

        # Test key MADCTL values
        tests = [
            # MV=0 (portrait, no transpose in data)
            (0x08, "0x08 MV=0 MX=0 MY=0 — portrait native"),
            (0x48, "0x48 MV=0 MX=1 MY=0 — portrait MX"),
            (0x88, "0x88 MV=0 MX=0 MY=1 — portrait MY"),
            (0xC8, "0xC8 MV=0 MX=1 MY=1 — portrait MX+MY"),

            # MV=1 (landscape, with transpose in data)
            (0x28, "0x28 MV=1 MX=0 MY=0 — landscape (no mirror)"),
            (0x38, "0x38 MV=1 MX=0 MY=0 ML=1"),
            (0x68, "0x68 MV=1 MX=1 MY=0 — landscape MX"),
            (0x78, "0x78 MV=1 MX=1 MY=0 ML=1 — original Waveshare"),
            (0xA8, "0xA8 MV=1 MX=0 MY=1 — landscape MY"),
            (0xE8, "0xE8 MV=1 MX=1 MY=1 — landscape MX+MY"),
        ]

        for madctl, desc in tests:
            print(f"Testing {desc}...")
            send_landscape(wcmd, wdata, pattern, madctl)
            time.sleep(3)

        print("\nWhich value shows: RED=TL, GREEN=TR, BLUE=BL, YELLOW=BR, arrow→RIGHT ?")

    except Exception as e:
        print(f"FAILED: {e}")
        import traceback
        traceback.print_exc()
    finally:
        bl.write(False)
        for h in (bl, rst, dc, spi):
            try: h.close()
            except Exception: pass
        print("Done.")


if __name__ == "__main__":
    main()