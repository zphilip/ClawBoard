#!/usr/bin/env python3
"""
Tests different numpy flip combinations after swapaxes to find correct orientation.
Each test shows a unique label so you can identify which one is correct.

Usage: sudo python3 test_lcd24_flip.py
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
SPI_DEV = "/dev/spidev1.0"
SPI_SPEED = 40_000_000
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'


def init_lcd(rst, dc, bl, spi):
    bl.write(True)
    def wcmd(c): dc.write(False); spi.transfer([c])
    def wdata(d):
        dc.write(True)
        if isinstance(d, int): spi.transfer([d])
        else:
            for i in range(0, len(d), 4096): spi.transfer(d[i:i + 4096])

    rst.write(True); time.sleep(0.01)
    rst.write(False); time.sleep(0.01)
    rst.write(True); time.sleep(0.01)

    wcmd(0x11); time.sleep(0.12)
    wcmd(0xCF); wdata(0x00); wdata(0xC1); wdata(0x30)
    wcmd(0xED); wdata(0x64); wdata(0x03); wdata(0x12); wdata(0x81)
    wcmd(0xE8); wdata(0x85); wdata(0x00); wdata(0x79)
    wcmd(0xCB); wdata(0x39); wdata(0x2C); wdata(0x00); wdata(0x34); wdata(0x02)
    wcmd(0xF7); wdata(0x20); wcmd(0xEA); wdata(0x00); wdata(0x00)
    wcmd(0xC0); wdata(0x1D); wcmd(0xC1); wdata(0x12)
    wcmd(0xC5); wdata(0x33); wdata(0x3F); wcmd(0xC7); wdata(0x92)
    wcmd(0x3A); wdata(0x55); wcmd(0x36); wdata(0x08)
    wcmd(0xB1); wdata(0x00); wdata(0x12); wcmd(0xB6); wdata(0x0A); wdata(0xA2)
    wcmd(0x44); wdata(0x02); wcmd(0xF2); wdata(0x00); wcmd(0x26); wdata(0x01)
    wcmd(0xE0)
    for b in [0x0F,0x22,0x1C,0x1B,0x08,0x0F,0x48,0xB8,0x34,0x05,0x0C,0x09,0x0F,0x07,0x00]: wdata(b)
    wcmd(0xE1)
    for b in [0x00,0x23,0x24,0x07,0x10,0x07,0x38,0x47,0x4B,0x0A,0x13,0x06,0x30,0x38,0x0F]: wdata(b)
    wcmd(0x29)
    return wcmd, wdata


def set_win(wcmd, wdata, xs, ys, xe, ye):
    wcmd(0x2A); wdata(xs>>8); wdata(xs&0xFF); wdata((xe-1)>>8); wdata((xe-1)&0xFF)
    wcmd(0x2B); wdata(ys>>8); wdata(ys&0xFF); wdata((ye-1)>>8); wdata((ye-1)&0xFF)
    wcmd(0x2C)


def send_transformed(wcmd, wdata, image, flip_x, flip_y, label):
    """Send a 320×240 landscape image with specified flips after swapaxes."""
    img = np.asarray(image)
    pix = np.zeros((240, 320, 2), dtype=np.uint8)
    pix[..., [0]] = np.add(np.bitwise_and(img[..., [0]], 0xF8), np.right_shift(img[..., [1]], 5))
    pix[..., [1]] = np.add(np.bitwise_and(np.left_shift(img[..., [1]], 3), 0xE0), np.right_shift(img[..., [2]], 3))

    # swapaxes: (240, 320, 2) → (320, 240, 2)
    pix = np.swapaxes(pix, 0, 1)

    if flip_x:
        pix = np.flip(pix, axis=0)  # flip X (row) order
    if flip_y:
        pix = np.flip(pix, axis=1)  # flip Y (col) order within each row

    pix = pix.flatten().tolist()

    wcmd(0x36); wdata(0x08)
    set_win(wcmd, wdata, 0, 0, 240, 320)
    wcmd(0x2C); wdata(pix)
    print(f"  {label}: flip_x={flip_x}, flip_y={flip_y}")


def make_image(label):
    W, H = 320, 240
    img = Image.new('RGB', (W, H), (245, 245, 245))
    draw = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype(FONT, 28)
    except OSError:
        f = ImageFont.load_default()

    # Header
    draw.rectangle((0, 0, W, 40), fill=(30, 30, 120))
    draw.text((10, 5), label, font=f, fill=(255, 255, 255))

    # Asymmetric markers to detect flip
    draw.rectangle((0, H-30, 30, H), fill=(255, 0, 0))        # BL: RED
    draw.rectangle((W-30, H-30, W, H), fill=(0, 255, 0))      # BR: GREEN

    # Arrow pointing RIGHT
    cx, cy = W//2, H//2
    draw.polygon([(cx+40, cy-30), (cx+80, cy), (cx+40, cy+30)], fill=(0,0,0))
    draw.rectangle((cx-60, cy-10, cx+40, cy+10), fill=(0,0,0))

    draw.text((10, 60), "192.168.1.100", font=f, fill=(40, 40, 40))
    draw.text((10, 100), "ZeroClaw: OK", font=f, fill=(0, 160, 0))
    return img


def main():
    print("=== Flip Orientation Test ===")
    print("Correct: RED bottom-left, GREEN bottom-right, arrow→RIGHT, text readable\n")

    rst = GPIO(RST_CHIP, RST_LINE, "out")
    dc  = GPIO(DC_CHIP,  DC_LINE,  "out")
    bl  = GPIO(BL_CHIP,  BL_LINE,  "out")
    spi = SPI(SPI_DEV, 0, SPI_SPEED)

    try:
        wcmd, wdata = init_lcd(rst, dc, bl, spi)

        tests = [
            (False, False, "1: no flip"),
            (True,  False, "2: flip X only"),
            (False, True,  "3: flip Y only"),
            (True,  True,  "4: flip BOTH (180)"),
        ]

        for fx, fy, label in tests:
            img = make_image(label)
            send_transformed(wcmd, wdata, img, fx, fy, label)
            time.sleep(4)

        print("\nWhich number shows correct orientation?")
    except Exception as e:
        print(f"FAILED: {e}")
        import traceback; traceback.print_exc()
    finally:
        bl.write(False)
        for h in (bl, rst, dc, spi):
            try: h.close()
            except Exception: pass
        print("Done.")

if __name__ == "__main__":
    main()