#!/usr/bin/env python3
"""
Test script for the 2.4" LCD (ILI9341) on Radxa Cubie A7Z.

Pin mapping (same as 1.54" e-paper):
  RST  → gpiochip0 line 33   (Pin 11)
  DC   → gpiochip0 line 110  (Pin 26)
  BL   → gpiochip0 line 13   (PWM backlight)
  SPI  → /dev/spidev1.0

Usage:
    sudo python3 test_lcd24.py
"""

import time
from periphery import GPIO, SPI

# ── Pin definitions ───────────────────────────────────────────────────────────
RST_CHIP, RST_LINE = "/dev/gpiochip0", 33
DC_CHIP,  DC_LINE  = "/dev/gpiochip0", 110
BL_CHIP,  BL_LINE  = "/dev/gpiochip0", 13
SPI_DEV   = "/dev/spidev1.0"
SPI_SPEED = 40_000_000

LCD_WIDTH  = 240
LCD_HEIGHT = 320


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

        # Backlight on (GPIO on/off only — no hardware PWM)
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
            """Write a list of ints as data, chunked for SPI buffer size."""
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

        write_cmd(0x11)   # Sleep out
        time.sleep(0.12)

        write_cmd(0xCF)
        write_data(0x00); write_data(0xC1); write_data(0x30)

        write_cmd(0xED)
        write_data(0x64); write_data(0x03); write_data(0x12); write_data(0x81)

        write_cmd(0xE8)
        write_data(0x85); write_data(0x00); write_data(0x79)

        write_cmd(0xCB)
        write_data(0x39); write_data(0x2C); write_data(0x00); write_data(0x34); write_data(0x02)

        write_cmd(0xF7); write_data(0x20)
        write_cmd(0xEA); write_data(0x00); write_data(0x00)

        write_cmd(0xC0); write_data(0x1D)   # Power control VRH[5:0]
        write_cmd(0xC1); write_data(0x12)   # Power control SAP[2:0]; BT[3:0]
        write_cmd(0xC5); write_data(0x33); write_data(0x3F)  # VCM control
        write_cmd(0xC7); write_data(0x92)   # VCM control

        write_cmd(0x3A); write_data(0x55)   # 16-bit RGB565
        write_cmd(0x36); write_data(0x08)   # MADCTL: portrait

        write_cmd(0xB1); write_data(0x00); write_data(0x12)
        write_cmd(0xB6); write_data(0x0A); write_data(0xA2)
        write_cmd(0x44); write_data(0x02)
        write_cmd(0xF2); write_data(0x00)   # 3Gamma disable
        write_cmd(0x26); write_data(0x01)   # Gamma curve

        # Positive gamma
        write_cmd(0xE0)
        write_data(0x0F); write_data(0x22); write_data(0x1C); write_data(0x1B)
        write_data(0x08); write_data(0x0F); write_data(0x48); write_data(0xB8)
        write_data(0x34); write_data(0x05); write_data(0x0C); write_data(0x09)
        write_data(0x0F); write_data(0x07); write_data(0x00)

        # Negative gamma
        write_cmd(0xE1)
        write_data(0x00); write_data(0x23); write_data(0x24); write_data(0x07)
        write_data(0x10); write_data(0x07); write_data(0x38); write_data(0x47)
        write_data(0x4B); write_data(0x0A); write_data(0x13); write_data(0x06)
        write_data(0x30); write_data(0x38); write_data(0x0F)

        write_cmd(0x29)   # Display on
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
            """Fill entire screen with a 16-bit RGB565 colour."""
            hi, lo = (rgb565 >> 8) & 0xFF, rgb565 & 0xFF
            set_window(0, 0, LCD_WIDTH, LCD_HEIGHT)
            buf = [hi, lo] * (LCD_WIDTH * LCD_HEIGHT)
            write_data_list(buf)

        # 4. Colour cycle test
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
            time.sleep(1.5)

        print("Colour cycle complete. Backlight off in 3s...")
        time.sleep(3)
        bl.write(False)

    except Exception as e:
        print(f"Test failed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if bl:
            try:
                bl.close()
            except Exception:
                pass
        for h in (rst, dc, spi):
            if h:
                try:
                    h.close()
                except Exception:
                    pass
        print("Hardware released.")


if __name__ == "__main__":
    lcd_test()