#!/usr/bin/env python3
"""
Tests the LCDRadxa24 adapter/driver classes (exactly as clawberry_display uses them).
Isolates whether the bug is in the driver or in the rendering code.

Usage:
    sudo python3 test_lcd24_adapter.py
"""

import sys, os, time

# Add lib to path like clawberry_display.py does
_HERE = os.path.dirname(os.path.realpath(__file__))  # .../ClawBoard/lib/radxa_epd
_LIB  = os.path.dirname(_HERE)                         # .../ClawBoard/lib
sys.path.insert(0, _LIB)

from PIL import Image, ImageDraw, ImageFont

FONT_BOLD = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
FONT_REG  = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'

def load_font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def test_adapter():
    """Use LCDRadxa24 adapter directly — same as clawberry_display does."""
    from radxa_epd.lcd_adapter import LCDRadxa24

    print("Creating LCDRadxa24...")
    lcd = LCDRadxa24()
    print(f"  width={lcd.width}, height={lcd.height}")

    print("Init...")
    lcd.Init()

    print("Clear (white)...")
    lcd.clear()
    time.sleep(0.5)

    # ── Test 1: Portrait 240×320 with text ─────────────────────────────
    print("Test 1: Portrait text (240×320, MADCTL=0x08 in driver)")
    W, H = lcd.width, lcd.height  # 240, 320
    img = Image.new('RGB', (W, H), (20, 20, 40))
    draw = ImageDraw.Draw(img)

    f_big = load_font(FONT_BOLD, 48)
    f_med = load_font(FONT_REG, 20)

    draw.rectangle((0, 0, W, 50), fill=(30, 30, 120))
    draw.text((10, 5), 'PORTRAIT', font=f_big, fill=(255, 255, 255))
    draw.text((10, 80), 'ABCDEFGH', font=f_med, fill=(255, 0, 0))
    draw.text((10, 110), '12345678', font=f_med, fill=(0, 255, 0))
    draw.text((10, 140), 'hello!', font=f_med, fill=(0, 0, 255))

    lcd.ShowImage(img)
    print("  Check: dark bg, header 'PORTRAIT', red 'ABCDEFGH', green '12345678', blue 'hello!'")
    time.sleep(5)

    # ── Test 2: Landscape 320×240 ─────────────────────────────────────
    print("Test 2: Landscape text (320×240, MADCTL=0x78 in driver)")
    W, H = lcd.height, lcd.width  # 320, 240 (landscape)
    img = Image.new('RGB', (W, H), (245, 245, 245))
    draw = ImageDraw.Draw(img)

    f_big2 = load_font(FONT_BOLD, 36)
    f_med2 = load_font(FONT_REG, 17)

    draw.rectangle((0, 0, W, 44), fill=(30, 30, 120))
    draw.text((10, 6), 'LANDSCAPE', font=f_big2, fill=(255, 255, 255))
    draw.text((10, 60), '192.168.1.100', font=f_med2, fill=(40, 40, 40))
    draw.text((10, 84), 'ZeroClaw: Running', font=f_med2, fill=(40, 167, 69))
    draw.text((10, 108), 'PicoClaw: Stopped', font=f_med2, fill=(220, 53, 69))

    lcd.ShowImage(img)
    print("  Check: light bg, header 'LANDSCAPE', IP and service status")
    time.sleep(5)

    # ── Test 3: Bypass adapter, use raw driver ────────────────────────
    print("Test 3: Raw LCD_2inch4.display_image() (bypass adapter)")
    from radxa_epd.lcd2in4 import LCD_2inch4

    # WARNING: this will conflict with the adapter's hardware handles
    # Let's just reuse the adapter's internal driver
    raw = lcd._lcd

    W, H = raw.height, raw.width  # 320, 240
    img = Image.new('RGB', (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    f_test = load_font(FONT_BOLD, 36)
    draw.text((10, 50), 'RAW DRIVER', font=f_test, fill=(255, 0, 0))
    draw.text((10, 100), 'landscape', font=f_test, fill=(0, 0, 255))

    raw.display_image(img)
    print("  Check: white bg, red 'RAW DRIVER', blue 'landscape'")
    time.sleep(5)

    print("\nAll tests done.")
    lcd.module_exit()


if __name__ == "__main__":
    test_adapter()