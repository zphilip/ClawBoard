#!/usr/bin/env python3
"""
Diagnostic test for 2.4" LCD ILI9341 — draws a numbered grid to reveal
orientation, mirroring, and window-addressing issues.

Usage:
    sudo python3 test_lcd24_debug.py
"""

import time
import numpy as np
from periphery import GPIO, SPI
from PIL import Image, ImageDraw, ImageFont

RST_CHIP, RST_LINE = "/dev/gpiochip0", 33
DC_CHIP,  DC_LINE  = "/dev/gpiochip0", 110
BL_CHIP,  BL_LINE  = "/dev/gpiochip1", 6
SPI_DEV   = "/dev/spidev1.0"
SPI_SPEED = 40_000_000

LCD_W = 240
LCD_H = 320

FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'


def init_display(rst, dc, bl, spi):
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


def set_window(wcmd, wdata, xs, ys, xe, ye):
    wcmd(0x2A)
    wdata(xs >> 8); wdata(xs & 0xFF)
    wdata((xe - 1) >> 8); wdata((xe - 1) & 0xFF)
    wcmd(0x2B)
    wdata(ys >> 8); wdata(ys & 0xFF)
    wdata((ye - 1) >> 8); wdata((ye - 1) & 0xFF)
    wcmd(0x2C)


def send_rgb565_image(wcmd, wdata, image):
    """Send a PIL image as RGB565 to the display (no MADCTL — caller sets it)."""
    imwidth, imheight = image.size
    img = np.asarray(image)
    pix = np.zeros((imheight, imwidth, 2), dtype=np.uint8)
    pix[..., [0]] = np.add(np.bitwise_and(img[..., [0]], 0xF8), np.right_shift(img[..., [1]], 5))
    pix[..., [1]] = np.add(np.bitwise_and(np.left_shift(img[..., [1]], 3), 0xE0), np.right_shift(img[..., [2]], 3))
    pix = pix.flatten().tolist()

    wcmd(0x2C)  # memory write
    wdata(pix)


def draw_grid(W, H, label):
    """Draw a numbered 4×4 grid with labels for orientation debugging."""
    img = Image.new('RGB', (W, H), (40, 40, 40))
    draw = ImageDraw.Draw(img)

    try:
        font_big = ImageFont.truetype(FONT, 20)
        font_mid = ImageFont.truetype(FONT, 14)
    except OSError:
        font_big = ImageFont.load_default()
        font_mid = ImageFont.load_default()

    # Colored bars at edges to identify orientation
    draw.rectangle((0, 0, W, 8), fill=(255, 0, 0))      # Top: RED
    draw.rectangle((0, H - 8, W, H), fill=(0, 0, 255))   # Bottom: BLUE
    draw.rectangle((0, 0, 8, H), fill=(0, 255, 0))       # Left: GREEN
    draw.rectangle((W - 8, 0, W, H), fill=(255, 255, 0)) # Right: YELLOW

    # Corner markers
    for (cx, cy, col, name) in [
        (0, 0, (255, 255, 255), 'TL'),
        (W - 1, 0, (255, 255, 255), 'TR'),
        (0, H - 1, (255, 255, 255), 'BL'),
        (W - 1, H - 1, (255, 255, 255), 'BR'),
    ]:
        draw.ellipse((cx - 12, cy - 12, cx + 12, cy + 12), fill=col)
        draw.text((cx - 8, cy - 6), name, font=font_mid, fill=(0, 0, 0))

    # Numbered grid
    cols, rows = 4, 4
    cw, ch = W // cols, H // rows
    for r in range(rows):
        for c in range(cols):
            x1, y1 = c * cw, r * ch
            x2, y2 = x1 + cw, y1 + ch
            hue = (c * 64 + r * 64) % 256
            # Simple hue-based color
            col = (
                hue,
                (hue + 85) % 256,
                (hue + 170) % 256,
            )
            draw.rectangle((x1 + 2, y1 + 2, x2 - 2, y2 - 2), fill=col)
            num = f"{r},{c}"
            draw.text((x1 + cw // 2 - 16, y1 + ch // 2 - 10),
                      num, font=font_mid, fill=(255, 255, 255))

    # Center label
    draw.text((W // 2 - 60, H // 2 + 24),
              label, font=font_big, fill=(255, 255, 255))

    return img


def main():
    print("=== ILI9341 Diagnostic Grid Test ===")

    rst = GPIO(RST_CHIP, RST_LINE, "out")
    dc  = GPIO(DC_CHIP,  DC_LINE,  "out")
    bl  = GPIO(BL_CHIP,  BL_LINE,  "out")
    spi = SPI(SPI_DEV, 0, SPI_SPEED)

    try:
        wcmd, wdata = init_display(rst, dc, bl, spi)

        # ── Test 1: Portrait 240×320 ─────────────────────────────────────
        print("\nTest 1: Portrait 240×320  MADCTL=0x08")
        img = draw_grid(240, 320, "PORTRAIT")
        wcmd(0x36); wdata(0x08)
        set_window(wcmd, wdata, 0, 0, 240, 320)
        send_rgb565_image(wcmd, wdata, img)
        print("  Check: RED bar at TOP, BLUE at BOTTOM, GREEN at LEFT, YELLOW at RIGHT")
        print("  Cells should read (row,col): (0,0) TL → (3,3) BR")
        time.sleep(5)

        # ── Test 2: Landscape 320×240  MADCTL=0x78 ───────────────────────
        print("\nTest 2: Landscape 320×240  MADCTL=0x78")
        img = draw_grid(320, 240, "LS 0x78")
        wcmd(0x36); wdata(0x78)
        set_window(wcmd, wdata, 0, 0, 240, 320)
        send_rgb565_image(wcmd, wdata, img)
        print("  How does the grid look? Are cells numbered correctly?")
        time.sleep(5)

        # ── Test 3: Landscape  MADCTL=0x68 ───────────────────────────────
        print("\nTest 3: Landscape 320×240  MADCTL=0x68 (alt rotation)")
        img = draw_grid(320, 240, "LS 0x68")
        wcmd(0x36); wdata(0x68)
        set_window(wcmd, wdata, 0, 0, 240, 320)
        send_rgb565_image(wcmd, wdata, img)
        time.sleep(5)

        # ── Test 4: Simple text test ─────────────────────────────────────
        print("\nTest 4: Text rendering check (portrait)")
        img = Image.new('RGB', (240, 320), (0, 0, 0))
        draw = ImageDraw.Draw(img)
        try:
            f64 = ImageFont.truetype(FONT, 64)
            f24 = ImageFont.truetype(FONT, 24)
        except OSError:
            f64 = ImageFont.load_default()
            f24 = ImageFont.load_default()
        draw.text((20, 20), "ABC", font=f64, fill=(255, 255, 255))
        draw.text((20, 100), "123", font=f64, fill=(0, 255, 0))
        draw.text((20, 180), "Hello!", font=f24, fill=(255, 0, 0))
        wcmd(0x36); wdata(0x08)
        set_window(wcmd, wdata, 0, 0, 240, 320)
        send_rgb565_image(wcmd, wdata, img)
        time.sleep(5)

        print("\n=== Tests complete ===")

    except Exception as e:
        print(f"FAILED: {e}")
        import traceback
        traceback.print_exc()
    finally:
        bl.write(False)
        for h in (bl, rst, dc, spi):
            try: h.close()
            except Exception: pass
        print("Hardware released.")


if __name__ == "__main__":
    main()