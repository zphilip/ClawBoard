import os
import sys
import time
import signal
import logging
import subprocess
import json
import textwrap
from io import BytesIO
from urllib.parse import quote
from urllib.request import urlopen
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
_HERE            = os.path.dirname(os.path.realpath(__file__))
DISPLAY_REQUEST_FILE = os.path.join(_HERE, 'config', 'clawberry_paircode.txt')
DISPLAY_SECONDS  = 120          # how long to show temporary content before resuming
MONITOR_REFRESH_SECONDS = 60
POLL_SECONDS = 1

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


def _read_display_request():
    """Read and remove the next pending display request."""
    if not os.path.exists(DISPLAY_REQUEST_FILE):
        return None
    try:
        with open(DISPLAY_REQUEST_FILE) as f:
            raw = f.read().strip()
        os.remove(DISPLAY_REQUEST_FILE)
        if not raw:
            return None
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            return {'kind': 'paircode', 'code': raw, 'seconds': DISPLAY_SECONDS}
    except Exception as e:
        logging.warning("Error handling display request file: %s", e)
        try:
            os.remove(DISPLAY_REQUEST_FILE)
        except Exception:
            pass
    return None


def _fetch_qr_image(text, size=220):
    """Fetch a QR image for *text* using QuickChart."""
    qr_url = f'https://quickchart.io/qr?size={size}&margin=1&text={quote(text, safe="")}'
    with urlopen(qr_url, timeout=15) as r:
        return Image.open(BytesIO(r.read())).convert('1')


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


def draw_picoclaw_qr(epd, url, token=''):
    """Render a PicoClaw pairing QR screen."""
    W, H = epd.height, epd.width
    image = Image.new('1', (W, H), 255)
    draw  = ImageDraw.Draw(image)

    f_title = _load_font(_FONT_BOLD, 15)
    f_small = _load_font(_FONT_REG, 12)
    f_tiny  = _load_font(_FONT_REG, 10)

    draw.text((8, 4), "PicoClaw Pair QR", font=f_title, fill=0)
    draw.line((8, 22, W - 8, 22), fill=0)

    qr_size = min(H - 34, 88)
    try:
        qr_img = _fetch_qr_image(url).resize((qr_size, qr_size))
        image.paste(qr_img, (8, 28))
    except Exception as e:
        logging.warning("Could not fetch QR image: %s", e)
        draw.rectangle((8, 28, 8 + qr_size, 28 + qr_size), outline=0, width=2)
        draw.text((26, 62), "QR", font=f_title, fill=0)

    text_x = 8 + qr_size + 10
    for idx, line in enumerate(textwrap.wrap(url, width=22)[:4]):
        draw.text((text_x, 30 + idx * 13), line, font=f_small, fill=0)

    token_line = f"token: {token[:12]}..." if len(token) > 12 else f"token: {token}"
    draw.text((text_x, H - 20), token_line, font=f_tiny, fill=0)

    epd.init()
    epd.display(epd.getbuffer(image))
    epd.sleep()


def _handle_display_request(epd, payload):
    kind = payload.get('kind', 'paircode')
    seconds = int(payload.get('seconds', DISPLAY_SECONDS) or DISPLAY_SECONDS)

    if kind == 'pico_qr':
        url = str(payload.get('url', '')).strip()
        token = str(payload.get('token', '')).strip()
        if url:
            logging.info("PicoClaw QR request — showing for %ss", seconds)
            draw_picoclaw_qr(epd, url, token)
            time.sleep(seconds)
        return

    code = str(payload.get('code', '')).strip()
    if code:
        logging.info("Pair code request: '%s' — showing for %ss", code, seconds)
        draw_paircode(epd, code)
        time.sleep(seconds)


# ── Main loop ─────────────────────────────────────────────────────────────
epd = epd2in13_V4.EPD()
logging.info("ClawBerry display service starting...")

while True:
    payload = _read_display_request()
    if payload:
        _handle_display_request(epd, payload)
        continue

    # ── Normal status screen ──────────────────────────────────────────────
    logging.info("Refreshing monitor screen...")
    draw_monitor(epd)
    logging.info("Waiting for next update...")
    waited = 0
    while waited < MONITOR_REFRESH_SECONDS:
        time.sleep(POLL_SECONDS)
        waited += POLL_SECONDS
        payload = _read_display_request()
        if payload:
            _handle_display_request(epd, payload)
            break
