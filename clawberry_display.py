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
MONITOR_FORCE_REFRESH_SECONDS = 3600  # force a full monitor redraw after this many idle seconds (ghost-busting)
POLL_SECONDS = 1

_FONT_BOLD = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
_FONT_REG  = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'

# ── Global EPD handle for clean shutdown ─────────────────────────────────
epd = None
_full_refresh_counter = 0
_FULL_REFRESH_EVERY   = 10   # force a full refresh every N renders to clear ghosting


def _epd_render(epd, image, force_full=False):
    """Push *image* to the display with minimal flicker.

    Uses ``init_Fast`` / ``displayFast`` (no black→white wipe) by default.
    Every ``_FULL_REFRESH_EVERY`` calls — or when ``force_full=True`` — a
    full refresh is done to clear accumulated ghosting.
    """
    global _full_refresh_counter
    _full_refresh_counter += 1
    do_full = force_full or (_full_refresh_counter % _FULL_REFRESH_EVERY == 0)

    buf = epd.getbuffer(image)

    if do_full:
        logging.debug("Full refresh (counter=%d)", _full_refresh_counter)
        epd.init()
        epd.display(buf)
    else:
        try:
            epd.init_Fast()
            epd.displayFast(buf)
        except AttributeError:
            # Driver version doesn’t expose init_Fast / displayFast — fall back
            logging.debug("Fast mode unavailable, using full refresh")
            epd.init()
            epd.display(buf)

    epd.sleep()


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
        out = subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL).decode().strip()
        return out if out else None
    except Exception:
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


def _generate_qr_image(text, size=110):
    """Generate a QR image for *text*.
    Tries the local ``qrcode`` library first (no internet required),
    then falls back to the QuickChart cloud API."""
    try:
        import qrcode as _qrcode
        qr = _qrcode.QRCode(
            version=None,
            error_correction=_qrcode.constants.ERROR_CORRECT_L,
            box_size=3,
            border=2,
        )
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(fill_color='black', back_color='white').convert('1')
        return img.resize((size, size), Image.NEAREST)
    except ImportError:
        pass
    # Remote fallback
    return _fetch_qr_image(text, size)


# ── Screens ───────────────────────────────────────────────────────────────
def draw_monitor(epd):
    """Render the normal status screen.

    Left column: QR code for http://<primary_ip>:8080 (110×110 px).
    Right column: title, IP addresses for every active interface
                  (wlan0 / eth0 / usb0), service statuses.
    """
    W, H = epd.height, epd.width   # 250 × 122 in landscape
    image = Image.new('1', (W, H), 255)
    draw  = ImageDraw.Draw(image)

    f_title = _load_font(_FONT_BOLD, 12)
    f_label = _load_font(_FONT_BOLD, 10)
    f_ip    = _load_font(_FONT_REG,  10)
    f_tiny  = _load_font(_FONT_REG,   9)

    # ── Gather IPs — None means interface is absent / has no address ─────
    # Priority for QR: WiFi → Ethernet → Bluetooth PAN → USB gadget
    w_ip = get_ip_address('wlan0')  # None when not connected
    e_ip = get_ip_address('eth0')   # None when not connected
    b_ip = get_ip_address('bnep0')  # None when BT tethering is off
    u_ip = get_ip_address('usb0')   # None when USB gadget not active

    # First non-None IP wins — this is what the QR points to
    # Priority: WiFi → ETH → USB → BT (bnep0)
    primary_ip = w_ip or e_ip or u_ip or b_ip

    # ── QR code — left side, vertically centred ───────────────────────────
    QR_SIZE = 110
    QR_X    = 2
    QR_Y    = (H - QR_SIZE) // 2

    if primary_ip:
        qr_url = f'http://{primary_ip}:8080'
        try:
            qr_img = _generate_qr_image(qr_url, size=QR_SIZE)
            image.paste(qr_img, (QR_X, QR_Y))
        except Exception as exc:
            logging.warning("QR generation failed: %s", exc)
            draw.rectangle((QR_X, QR_Y, QR_X + QR_SIZE, QR_Y + QR_SIZE), outline=0, width=1)
            draw.text((QR_X + 14, QR_Y + 44), "QR err", font=f_ip, fill=0)
    else:
        draw.rectangle((QR_X, QR_Y, QR_X + QR_SIZE, QR_Y + QR_SIZE), outline=0, width=1)
        draw.text((QR_X + 18, QR_Y + 44), "No IP", font=f_ip, fill=0)

    # ── Right panel ───────────────────────────────────────────────────────
    tx = QR_X + QR_SIZE + 5
    y  = 2

    draw.text((tx, y), "ClawBerry", font=f_title, fill=0)
    y += 14
    draw.line((tx, y, W - 2, y), fill=0)
    y += 4

    # Only show rows for interfaces that actually have an IP
    any_ip = False
    for iface_label, ip in (('WiFi', w_ip), ('ETH', e_ip), ('USB', u_ip), ('BT', b_ip)):
        if ip:   # ip is None when not connected — skip
            draw.text((tx,      y), f"{iface_label}:", font=f_label, fill=0)
            draw.text((tx + 28, y), ip,                font=f_ip,    fill=0)
            y += 12
            any_ip = True
    if not any_ip:
        draw.text((tx, y), "No network", font=f_ip, fill=0)
        y += 12

    y += 3
    draw.line((tx, y, W - 2, y), fill=0)
    y += 4

    s_zc = get_service_status("zeroclaw")
    s_pc = get_service_status("picoclaw")
    draw.text((tx, y), f"ZC: {s_zc}", font=f_tiny, fill=0); y += 12
    draw.text((tx, y), f"PC: {s_pc}", font=f_tiny, fill=0)

    _epd_render(epd, image)


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

    _epd_render(epd, image)


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

    _epd_render(epd, image, force_full=True)


# ── State helpers ────────────────────────────────────────────────────────
def _get_current_state():
    """Snapshot all display-relevant system state for change detection."""
    return {
        'wlan0':    get_ip_address('wlan0'),
        'eth0':     get_ip_address('eth0'),
        'bnep0':    get_ip_address('bnep0'),
        'usb0':     get_ip_address('usb0'),
        'zeroclaw': get_service_status('zeroclaw'),
        'picoclaw': get_service_status('picoclaw'),
    }


def _file_mtime():
    """Return mtime of DISPLAY_REQUEST_FILE, or None if the file is absent."""
    try:
        return os.path.getmtime(DISPLAY_REQUEST_FILE)
    except OSError:
        return None


def _draw_request_screen(epd, payload):
    """Render the temporary screen for *payload* — does NOT block/sleep."""
    kind = payload.get('kind', 'paircode')
    if kind == 'pico_qr':
        url   = str(payload.get('url',   '')).strip()
        token = str(payload.get('token', '')).strip()
        if url:
            draw_picoclaw_qr(epd, url, token)
    else:
        code = str(payload.get('code', '')).strip()
        if code:
            draw_paircode(epd, code)


# ── Main loop ─────────────────────────────────────────────────────────────
# Rendering is change-driven:
#   • clawberry_paircode.txt created/updated → immediate temporary screen
#   • IP address or service status change    → immediate monitor refresh
#   • MONITOR_FORCE_REFRESH_SECONDS elapsed  → periodic ghost-busting refresh
epd = epd2in13_V4.EPD()
logging.info("ClawBerry display service starting — change-driven rendering active.")

last_state        = _get_current_state()
draw_monitor(epd)
last_monitor_draw = time.monotonic()
last_file_mtime   = _file_mtime()   # capture mtime of any pre-existing request file
hold_until        = 0.0             # monotonic time until temp screen must not be overwritten

while True:
    time.sleep(POLL_SECONDS)
    now = time.monotonic()

    # ── 1. Check for new / updated pair-code or pico-QR request file ─────
    cur_mtime = _file_mtime()
    if cur_mtime is not None and cur_mtime != last_file_mtime:
        # File appeared or was rewritten — process it immediately
        payload = _read_display_request()   # reads + deletes the file
        last_file_mtime = None              # file is now gone
        if payload:
            seconds    = int(payload.get('seconds', DISPLAY_SECONDS) or DISPLAY_SECONDS)
            hold_until = now + seconds
            kind       = payload.get('kind', 'paircode')
            logging.info("Display request (%s) — holding for %d s", kind, seconds)
            _draw_request_screen(epd, payload)
            continue                        # skip state check this cycle
    else:
        last_file_mtime = cur_mtime         # keep in sync (tracks None when absent)

    # ── 2. While inside the hold window, don't overwrite the temp screen ──
    if now < hold_until:
        continue

    # ── 3. Check for network / service state changes ──────────────────────
    current_state = _get_current_state()
    age           = now - last_monitor_draw
    state_changed = current_state != last_state
    force_refresh = age >= MONITOR_FORCE_REFRESH_SECONDS

    if state_changed or force_refresh:
        if state_changed:
            changed = [k for k in current_state if current_state[k] != last_state.get(k)]
            logging.info("State change detected (%s) — updating display", ', '.join(changed))
        else:
            logging.info("Periodic forced refresh (%.0f s since last draw)", age)
        draw_monitor(epd)
        last_state        = current_state
        last_monitor_draw = now
