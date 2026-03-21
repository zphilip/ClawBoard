#!/usr/bin/env python3
"""
Display a pairing code on the Waveshare 2.13" e-ink display.

Usage (CLI):  python3 clawberry_paircode.py <code>
Usage (API):  from clawberry_paircode import show_paircode; show_paircode("752167")
"""

import os
import sys
import logging

os.environ.setdefault('GPIOZERO_PIN_FACTORY', 'rpigpio')

current_dir = os.path.dirname(os.path.realpath(__file__))
libdir = os.path.join(current_dir, 'lib')
if os.path.exists(libdir):
    sys.path.append(libdir)

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("ERROR: Pillow not installed. Run: pip install Pillow", file=sys.stderr)
    sys.exit(1)

logging.basicConfig(level=logging.INFO)


_FONT_BOLD = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
_FONT_REG  = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'


def _load_font(path: str, size: int):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def show_paircode(code: str) -> None:
    """Render *code* prominently on the 2.13″ e-ink display, then sleep it."""
    try:
        from waveshare_epd import epd2in13_V4
    except ImportError:
        logging.warning("waveshare_epd not available — skipping display update.")
        return

    epd = epd2in13_V4.EPD()
    epd.init()

    # Landscape canvas: width = epd.height (250 px), height = epd.width (122 px)
    W, H = epd.height, epd.width
    image = Image.new('1', (W, H), 255)
    draw  = ImageDraw.Draw(image)

    f_title = _load_font(_FONT_BOLD, 16)
    f_hint  = _load_font(_FONT_REG,  13)

    # ── Title bar ──────────────────────────────────────────────────────────
    draw.text((8, 4), "ZeroClaw Pair Code", font=f_title, fill=0)
    draw.line((8, 23, W - 8, 23), fill=0)

    # ── Large code, centred in remaining space ─────────────────────────────
    # Pick the largest font that fits within 90 % of the canvas width
    for fsize in (56, 48, 40, 32):
        f_code = _load_font(_FONT_BOLD, fsize)
        bbox   = draw.textbbox((0, 0), code, font=f_code)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if tw <= W * 0.9:
            break

    cx = (W - tw) // 2
    cy = 26 + (H - 26 - th) // 2
    draw.text((cx, cy), code, font=f_code, fill=0)

    # ── Footer hint ────────────────────────────────────────────────────────
    hint = "scan / type in app"
    hbbox = draw.textbbox((0, 0), hint, font=f_hint)
    draw.text(((W - (hbbox[2] - hbbox[0])) // 2, H - 16), hint, font=f_hint, fill=0)

    epd.display(epd.getbuffer(image))
    epd.sleep()
    logging.info("Pair code '%s' shown on e-ink display.", code)


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(f"Usage: {os.path.basename(__file__)} <pair-code>")
        sys.exit(1)
    show_paircode(sys.argv[1])
