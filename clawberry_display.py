import os
import sys
import time
import signal
import logging
import subprocess
from PIL import Image, ImageDraw, ImageFont

# ── Driver path setup ─────────────────────────────────────────────────────
os.environ['GPIOZERO_PIN_FACTORY'] = 'rpigpio'
current_dir = os.path.dirname(os.path.realpath(__file__))
libdir = os.path.join(current_dir, 'lib')
if os.path.exists(libdir):
    sys.path.append(libdir)

from waveshare_epd import epd2in13_V4

logging.basicConfig(level=logging.INFO)

# ── Handoff file written by clawberry_paircode.py ─────────────────────────
PAIRCODE_FILE    = '/tmp/clawberry_paircode.txt'
DISPLAY_SECONDS  = 120          # how long to show pair code before resuming

_FONT_BOLD = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
_FONT_REG  = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'

# ── Global EPD handle for clean shutdown ─────────────────────────────────
epd = None

def _shutdown(signum=None, frame=None):
    logging.info("Shutdown signal %s — releasing display hardware...", signum)
    if epd is not None:
        try:
            epd.Dev_exit()
        except Exception:
            try:
                epd.module_exit()
            except Exception as e:
                logging.warning("Could not release hardware: %s", e)
    sys.exit(0)

signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT,  _shutdown)


# ── Helpers ───────────────────────────────────────────────────────────────
def _load_font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()

def get_ip_address(ifname):
    try:
        cmd = f"ip -4 addr show {ifname} | grep -oP '(?<=inet\\s)\\d+(\\.\\d+){{3}}'"
        return subprocess.check_output(cmd, shell=True).decode().strip()
    except:
        return None

def get_service_status(service_name):
    try:
        status = subprocess.check_output(
            f"systemctl is-active {service_name}", shell=True
        ).decode().strip()
        return "Running" if status == "active" else "Stopped"
    except:
        return "Unknown"


# ── Screens ───────────────────────────────────────────────────────────────
def draw_monitor(epd):
    """Render the normal status screen."""
    W, H = epd.height, epd.width
    image = Image.new('1', (W, H), 255)
    draw  = ImageDraw.Draw(image)

    f_title = _load_font(_FONT_BOLD, 28)
    f_small = _load_font(_FONT_REG,  16)

    draw.text((10, 2), "ClawBerry Monitor", font=f_title, fill=0)
    draw.line((10, 32, W - 10, 32), fill=0)

    w_ip = get_ip_address('wlan0') or "Disconnected"
    u_ip = get_ip_address('usb0')  or "Not detected"
    draw.text((10, 38), f"WiFi: {w_ip}", font=f_small, fill=0)
    draw.text((10, 56), f"USB:  {u_ip}", font=f_small, fill=0)
    draw.line((10, 76, W - 10, 76), fill=0)

    s1 = get_service_status("zeroclaw")
    s2 = get_service_status("picoclaw")
    draw.text((10, 82),  f"zeroclaw: {s1}", font=f_small, fill=0)
    draw.text((10, 100), f"picoclaw: {s2}", font=f_small, fill=0)

    epd.init()
    epd.display(epd.getbuffer(image))
    epd.sleep()


def draw_paircode(epd, code):
    """Render the pair code screen."""
    W, H = epd.height, epd.width
    image = Image.new('1', (W, H), 255)
    draw  = ImageDraw.Draw(image)

    f_title = _load_font(_FONT_BOLD, 16)
    f_hint  = _load_font(_FONT_REG,  13)

    draw.text((8, 4), "ZeroClaw Pair Code", font=f_title, fill=0)
    draw.line((8, 23, W - 8, 23), fill=0)

    # Auto-size the code to fit
    for fsize in (56, 48, 40, 32):
        f_code = _load_font(_FONT_BOLD, fsize)
        bbox = draw.textbbox((0, 0), code, font=f_code)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if tw <= W * 0.9:
            break

    draw.text(((W - tw) // 2, 26 + (H - 26 - th) // 2), code, font=f_code, fill=0)

    hint = "scan / type in app"
    hbbox = draw.textbbox((0, 0), hint, font=f_hint)
    draw.text(((W - (hbbox[2] - hbbox[0])) // 2, H - 16), hint, font=f_hint, fill=0)

    epd.init()
    epd.display(epd.getbuffer(image))
    epd.sleep()


# ── Main loop ─────────────────────────────────────────────────────────────
epd = epd2in13_V4.EPD()
logging.info("ClawBerry display service starting...")

while True:
    # ── Check for a pending pair code request ─────────────────────────────
    if os.path.exists(PAIRCODE_FILE):
        try:
            with open(PAIRCODE_FILE) as f:
                code = f.read().strip()
            os.remove(PAIRCODE_FILE)
            if code:
                logging.info("Pair code request: '%s' — showing for %ds", code, DISPLAY_SECONDS)
                draw_paircode(epd, code)
                # Stay on pair code screen for DISPLAY_SECONDS
                time.sleep(DISPLAY_SECONDS)
                # Fall through to normal refresh below
        except Exception as e:
            logging.warning("Error handling paircode file: %s", e)
            try:
                os.remove(PAIRCODE_FILE)
            except Exception:
                pass

    # ── Normal status screen ──────────────────────────────────────────────
    logging.info("Refreshing monitor screen...")
    draw_monitor(epd)
    logging.info("Waiting for next update...")
    time.sleep(60)
