#!/usr/bin/env python3
"""
Test script for the 2.4" LCD (ILI9341) on Radxa Cubie A7Z.

Phase 1: solid colour cycle (red, green, blue, white, black)
Phase 2: rendered UI elements — text, shapes, IPs, service status

Pin mapping:
  RST  → gpiochip0 line 33   (Pin 11)
  DC   → gpiochip0 line 110  (Pin 26)
  BL   → gpiochip1 line 6    (Pin 13)
  SPI  → /dev/spidev1.0

Usage:
    sudo python3 test_lcd24.py
"""

import time
import numpy as np
from periphery import GPIO, SPI
from PIL import Image, ImageDraw, ImageFont

# ── Pin definitions ───────────────────────────────────────────────────────────
RST_CHIP, RST_LINE = "/dev/gpiochip0", 33
DC_CHIP,  DC_LINE  = "/dev/gpiochip0", 110
BL_CHIP,  BL_LINE  = "/dev/gpiochip1", 6
SPI_DEV   = "/dev/spidev1.0"
SPI_SPEED = 40_000_000

LCD_W = 240   # portrait width
LCD_H = 320   # portrait height

# ── Fonts ─────────────────────────────────────────────────────────────────────
FONT_BOLD = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
FONT_REG  = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'


def load_font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def lcd_test():
    print("--- 2.4\" LCD ILI9341 Test (RST:33, DC:110, BL:13) ---")

    rst = None
    dc = None
    bl = None
    spi = None

    try:
        # 1. Init hardware
        rst = GPIO(RST_CHIP, RST_LINE, "out")
        dc  = GPIO(DC_CHIP,  DC_LINE,  "out")
        bl  = GPIO(BL_CHIP,  BL_LINE,  "out")
        spi = SPI(SPI_DEV, 0, SPI_SPEED)
        bl.write(True)

        def write_cmd(c):
            dc.write(False)
            spi.transfer([c])

        def write_data(d):
            dc.write(True)
            if isinstance(d, int):
                spi.transfer([d])
            else:
                for i in range(0, len(d), 4096):
                    spi.transfer(d[i:i + 4096])

        def write_data_list(data_list):
            dc.write(True)
            for i in range(0, len(data_list), 4096):
                spi.transfer(data_list[i:i + 4096])

        # 2. Hardware reset
        print("Hardware reset...")
        rst.write(True);  time.sleep(0.01)
        rst.write(False); time.sleep(0.01)
        rst.write(True);  time.sleep(0.01)

        # 3. ILI9341 init sequence
        print("Initializing ILI9341...")
        write_cmd(0x11); time.sleep(0.12)

        write_cmd(0xCF); write_data(0x00); write_data(0xC1); write_data(0x30)
        write_cmd(0xED); write_data(0x64); write_data(0x03); write_data(0x12); write_data(0x81)
        write_cmd(0xE8); write_data(0x85); write_data(0x00); write_data(0x79)
        write_cmd(0xCB); write_data(0x39); write_data(0x2C); write_data(0x00); write_data(0x34); write_data(0x02)
        write_cmd(0xF7); write_data(0x20)
        write_cmd(0xEA); write_data(0x00); write_data(0x00)
        write_cmd(0xC0); write_data(0x1D)
        write_cmd(0xC1); write_data(0x12)
        write_cmd(0xC5); write_data(0x33); write_data(0x3F)
        write_cmd(0xC7); write_data(0x92)
        write_cmd(0x3A); write_data(0x55)
        write_cmd(0x36); write_data(0x08)
        write_cmd(0xB1); write_data(0x00); write_data(0x12)
        write_cmd(0xB6); write_data(0x0A); write_data(0xA2)
        write_cmd(0x44); write_data(0x02)
        write_cmd(0xF2); write_data(0x00)
        write_cmd(0x26); write_data(0x01)

        write_cmd(0xE0)
        for b in [0x0F,0x22,0x1C,0x1B,0x08,0x0F,0x48,0xB8,0x34,0x05,0x0C,0x09,0x0F,0x07,0x00]:
            write_data(b)
        write_cmd(0xE1)
        for b in [0x00,0x23,0x24,0x07,0x10,0x07,0x38,0x47,0x4B,0x0A,0x13,0x06,0x30,0x38,0x0F]:
            write_data(b)

        write_cmd(0x29)
        print("Init complete.")

        def set_window(xs, ys, xe, ye):
            write_cmd(0x2A)
            write_data(xs >> 8); write_data(xs & 0xFF)
            write_data((xe - 1) >> 8); write_data((xe - 1) & 0xFF)
            write_cmd(0x2B)
            write_data(ys >> 8); write_data(ys & 0xFF)
            write_data((ye - 1) >> 8); write_data((ye - 1) & 0xFF)
            write_cmd(0x2C)

        def fill_screen(rgb565):
            hi, lo = (rgb565 >> 8) & 0xFF, rgb565 & 0xFF
            set_window(0, 0, LCD_W, LCD_H)
            buf = [hi, lo] * (LCD_W * LCD_H)
            write_data_list(buf)

        def show_image(image, landscape=True):
            """Push a PIL image to the display. Handles RGB888→RGB565 conversion."""
            imwidth, imheight = image.size
            img = np.asarray(image)
            pix = np.zeros((imheight, imwidth, 2), dtype=np.uint8)
            pix[..., [0]] = np.add(
                np.bitwise_and(img[..., [0]], 0xF8),
                np.right_shift(img[..., [1]], 5),
            )
            pix[..., [1]] = np.add(
                np.bitwise_and(np.left_shift(img[..., [1]], 3), 0xE0),
                np.right_shift(img[..., [2]], 3),
            )
            pix = pix.flatten().tolist()

            if landscape and imwidth == LCD_H and imheight == LCD_W:
                # 320×240 landscape → rotate to fit 240×320 portrait panel
                write_cmd(0x36); write_data(0x78)
                set_window(0, 0, LCD_W, LCD_H)
            else:
                # Portrait
                write_cmd(0x36); write_data(0x08)
                set_window(0, 0, LCD_W, LCD_H)

            write_data_list(pix)

        # ── Phase 1: solid colour cycle ───────────────────────────────────────
        colours = [
            (0xF800, "Red"),
            (0x07E0, "Green"),
            (0x001F, "Blue"),
            (0xFFFF, "White"),
            (0x0000, "Black"),
        ]
        for rgb565, name in colours:
            print(f"  {name} (0x{rgb565:04X})...")
            fill_screen(rgb565)
            time.sleep(1.0)

        # ── Phase 2: rendered UI test ─────────────────────────────────────────
        print("Rendering UI test (landscape 320×240)...")

        # Draw a landscape canvas (320×240) — same as clawberry_display
        W, H = LCD_H, LCD_W   # 320 × 240 landscape
        C_BG      = (245, 245, 245)
        C_HDR     = (30, 30, 120)
        C_WHITE   = (255, 255, 255)
        C_DARK    = (40, 40, 40)
        C_GREEN   = (40, 167, 69)
        C_RED     = (220, 53, 69)
        C_GREY    = (160, 160, 160)

        image = Image.new('RGB', (W, H), C_BG)
        draw  = ImageDraw.Draw(image)

        f_hdr  = load_font(FONT_BOLD, 18)
        f_body = load_font(FONT_REG,  13)
        f_sm   = load_font(FONT_REG,  12)

        # Header bar
        draw.rectangle((0, 0, W, 40), fill=C_HDR)
        draw.text((10, 9), 'ClawBerry', font=f_hdr, fill=C_WHITE)

        # QR placeholder
        qr_size = 128
        qr_x, qr_y = 6, 40 + (H - 40 - qr_size) // 2
        draw.rectangle((qr_x, qr_y, qr_x + qr_size, qr_y + qr_size),
                       outline=C_GREY, width=2)
        draw.text((qr_x + 30, qr_y + qr_size // 2 - 7),
                  '[QR Code]', font=f_body, fill=C_GREY)

        # IP addresses
        ix = qr_x + qr_size + 8
        iy = 48
        for label, ip, col in [
            ('WiFi', '192.168.1.100', (80, 80, 200)),
            ('ETH',  '10.0.0.50',     (200, 120, 40)),
            ('USB',  '192.168.2.1',   (40, 150, 80)),
        ]:
            draw.rectangle((ix, iy, ix + 40, iy + 18), fill=col)
            draw.text((ix + 2, iy + 2), label, font=f_sm, fill=C_WHITE)
            draw.text((ix + 46, iy + 2), ip, font=f_sm, fill=C_DARK)
            iy += 22

        # Divider
        draw.line((ix, iy, W - 6, iy), fill=(200, 200, 200), width=1)
        iy += 7

        # Service status
        for svc, status in [
            ('ZeroClaw', 'Running'),
            ('PicoClaw', 'Stopped'),
        ]:
            col = C_GREEN if status == 'Running' else C_RED
            draw.ellipse((ix, iy + 3, ix + 11, iy + 14), fill=col)
            draw.text((ix + 16, iy + 1), f'{svc}: {status}', font=f_sm, fill=C_DARK)
            iy += 20

        # Footer
        draw.line((0, H - 20, W, H - 20), fill=C_GREY, width=1)
        draw.text((6, H - 18), 'LCD 2.4" ILI9341 — Radxa Cubie A7Z', font=f_sm, fill=C_GREY)

        show_image(image, landscape=True)
        print("UI test displayed. Showing for 10 seconds...")
        time.sleep(10)

        print("Test complete. Backlight off in 3s...")
        time.sleep(3)
        bl.write(False)

    except Exception as e:
        print(f"Test failed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if bl:
            try: bl.close()
            except Exception: pass
        for h in (rst, dc, spi):
            if h:
                try: h.close()
                except Exception: pass
        print("Hardware released.")


if __name__ == "__main__":
    lcd_test()