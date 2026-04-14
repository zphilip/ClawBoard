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
# Do NOT set GPIOZERO_PIN_FACTORY here at module level.
# The e-ink driver (waveshare epdconfig) uses RPi.GPIO directly. If gpiozero
# is already loaded with rpigpio factory, it holds GPIO lines open and
# RPi.GPIO.setup() fails with 'GPIO busy'. The LCD probe sets it lazily.
os.environ.pop('GPIOZERO_PIN_FACTORY', None)   # clear any inherited value
current_dir = os.path.dirname(os.path.realpath(__file__))
libdir = os.path.join(current_dir, 'lib')
if os.path.exists(libdir):
    sys.path.append(libdir)

# Display drivers are imported lazily inside _detect_display().
# Module-level references are set once detection succeeds.
_eink_mod = None   # waveshare_epd.epd2in13_V4 when e-ink is active
_lcd_mod  = None   # LCD_1inch69 module when LCD is active

logging.basicConfig(level=logging.INFO)

# ── 1.69" LCD hardware pin defaults (Waveshare HAT) ──────────────────────
_LCD_RST    = 27
_LCD_DC     = 25
_LCD_BL     = 18
_LCD_TP_INT = 4
_LCD_TP_RST = 17

# ── Handoff file written by clawberry_paircode.py ─────────────────────────
_HERE            = os.path.dirname(os.path.realpath(__file__))
DISPLAY_REQUEST_FILE      = os.path.join(_HERE, 'config', 'clawberry_paircode.txt')
DISPLAY_TYPE_OVERRIDE_FILE = os.path.join(_HERE, 'config', 'display_type.txt')
# Override file: create config/display_type.txt containing 'eink' or 'lcd'
# to skip auto-detection and force a specific driver.
# Fallback hold duration used ONLY when the payload written by clawberry_paircode.py
# has no 'seconds' field. The primary source of truth is always the 'seconds' value
# inside clawberry_paircode.txt — change it there, not here.
_FALLBACK_HOLD_SECONDS        = 30
MONITOR_FORCE_REFRESH_SECONDS = 3600  # force a full monitor redraw after this many idle seconds (ghost-busting)
POLL_SECONDS = 1

_FONT_BOLD = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
_FONT_REG  = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'

# ── Global display handle & type ────────────────────────────────────────
_disp         = None   # active display object (EPD or LCD_1inch69)
_display_type = None   # 'eink' | 'lcd'
_qr_cache     = {}     # {(url, size): PIL.Image} — avoid regenerating unchanged QR codes


def _epd_render(epd, image, force_full=False):
    """Push *image* to the display with minimal flicker.

    Uses ``init_fast`` / ``display_fast`` (no black→white wipe) by default.
    Pass ``force_full=True`` to do a full black→white refresh (ghost-busting);
    this is only done on the hourly periodic render or explicit QR screens.
    """
    buf = epd.getbuffer(image)

    if force_full:
        logging.debug("Full e-ink refresh (ghost-busting)")
        epd.init()
        epd.display(buf)
    else:
        try:
            epd.init_fast()
            epd.display_fast(buf)
        except AttributeError:
            # Driver version doesn't expose init_fast / display_fast — fall back
            logging.debug("Fast mode unavailable, using full refresh")
            epd.init()
            epd.display(buf)

    epd.sleep()


def _shutdown(signum=None, frame=None):
    logging.info("Shutdown signal %s — releasing display hardware...", signum)
    if _disp is not None:
        if _display_type == 'lcd':
            try:
                _disp.module_exit()
            except Exception as e:
                logging.warning("LCD module_exit failed: %s", e)
        else:  # eink
            released = False
            for _attr in ('Dev_exit',):
                fn = getattr(_disp, _attr, None)
                if fn:
                    try:
                        fn(); released = True; break
                    except Exception:
                        pass
            if not released:
                cfg = getattr(_eink_mod, 'epdconfig', None) or getattr(_disp, 'epdconfig', None)
                fn  = getattr(cfg, 'module_exit', None) if cfg else None
                if fn:
                    try:
                        fn(); released = True
                    except Exception as e:
                        logging.warning("epdconfig.module_exit failed: %s", e)
            if not released:
                try:
                    _disp.sleep()
                except Exception as e:
                    logging.warning("Could not release hardware: %s", e)
    sys.exit(0)

signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT,  _shutdown)


def _detect_display():
    """Probe hardware and initialise the first supported display found.

    Detection order (can be overridden by config/display_type.txt):
      1. LCD  1.69\"  (LCD_1inch69)  — tried first; touch controller init
                                        fails reliably when hardware absent
      2. E-ink 2.13\" (epd2in13_V4)  — SPI-only, always succeeds on a Pi
                                        even without display connected

    To force a type, create config/display_type.txt with content 'lcd' or 'eink'.
    Sets the module globals ``_disp``, ``_display_type``, ``_eink_mod`` /
    ``_lcd_mod`` and returns the active display object.
    """
    global _disp, _display_type, _eink_mod, _lcd_mod

    # ── 0. Check for a manual override file ───────────────────────────────
    _forced = None
    try:
        with open(DISPLAY_TYPE_OVERRIDE_FILE) as _f:
            _forced = _f.read().strip().lower()
        logging.info('Display type override: %r (from %s)', _forced, DISPLAY_TYPE_OVERRIDE_FILE)
    except FileNotFoundError:
        pass
    except Exception as _e:
        logging.warning('Could not read display_type.txt: %s', _e)

    # ── 1. Try LCD 1.69\" ────────────────────────────────────────────────
    if _forced != 'eink':
        import importlib as _il
        # LCD_1inch69.py uses relative imports (from . import ...) so it MUST
        # be loaded as part of a package.  We add the *parent* of the package
        # directory to sys.path, then import via importlib.import_module().
        # Each tuple: (parent_dir_for_sys_path, package_dir_name)
        _lcd_candidates = [
            ('/opt/clawboard/lib', 'waveshare_1in69'),
        ]
        _lm = None
        for _parent, _pkg in _lcd_candidates:
            _pkg_dir = os.path.join(_parent, _pkg)
            if not os.path.isdir(_pkg_dir):
                continue
            # Auto-create __init__.py so Python treats the dir as a package
            _init_py = os.path.join(_pkg_dir, '__init__.py')
            if not os.path.exists(_init_py):
                try:
                    open(_init_py, 'w').close()
                    logging.info('Created %s', _init_py)
                except Exception as _ie:
                    logging.warning('Could not create __init__.py: %s', _ie)
            if _parent not in sys.path:
                sys.path.insert(0, _parent)
            try:
                _lm = _il.import_module(f'{_pkg}.LCD_1inch69')
                logging.info('Loaded LCD module from %s/%s', _parent, _pkg)
                break
            except Exception as _ie:
                logging.info('Could not load %s.LCD_1inch69: %s', _pkg, _ie)
        if _lm is not None:
            try:
                _obj = _lm.LCD_1inch69(
                    rst=_LCD_RST, dc=_LCD_DC, bl=_LCD_BL,
                    tp_int=_LCD_TP_INT, tp_rst=_LCD_TP_RST, bl_freq=1000
                )
                _obj.Init()
                _obj.clear()
                _obj.bl_DutyCycle(80)
                _lcd_mod      = _lm
                _disp         = _obj
                _display_type = 'lcd'
                logging.info('Display detected: LCD 1.69\" (LCD_1inch69)')
                return _obj
            except Exception as e:
                logging.info('LCD 1.69\" not available: %s', e)

    # ── 2. Try e-ink 2.13\" ──────────────────────────────────────────────
    if _forced != 'lcd':
        try:
            from waveshare_epd import epd2in13_V4 as _em
            _obj = _em.EPD()
            _obj.init()
            _obj.sleep()
            _eink_mod     = _em
            _disp         = _obj
            _display_type = 'eink'
            logging.info('Display detected: e-ink 2.13\" (epd2in13_V4)')
            return _obj
        except Exception as e:
            logging.info('E-ink 2.13\" not available: %s', e)

    raise RuntimeError(
        'No supported display detected '
        '(tried LCD 1.69\" and e-ink 2.13\"). '
        f'Override file: {DISPLAY_TYPE_OVERRIDE_FILE}'
    )


# ── Helpers ───────────────────────────────────────────────────────────────────
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
        result = subprocess.run(
            ['systemctl', 'is-active', service_name],
            capture_output=True, text=True
        )
        status = result.stdout.strip()
        if status == 'active':
            return 'Running'
        elif status in ('inactive', 'failed', 'deactivating', 'activating'):
            return 'Stopped'
        else:
            return status.capitalize() if status else 'Unknown'
    except Exception:
        return 'Unknown'


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
            return {'kind': 'paircode', 'code': raw, 'seconds': _FALLBACK_HOLD_SECONDS}
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
    """Generate a QR image for *text*, with caching to avoid redundant work.
    Tries the local ``qrcode`` library first (no internet required),
    then falls back to the QuickChart cloud API."""
    key = (text, size)
    if key in _qr_cache:
        return _qr_cache[key]
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
        img = img.resize((size, size), Image.NEAREST)
    except ImportError:
        # Remote fallback
        img = _fetch_qr_image(text, size)
    _qr_cache[key] = img
    return img


# ── Screens ───────────────────────────────────────────────────────────────
def draw_monitor(epd, force_full=False):
    """Render the normal status screen.

    Left column: QR code for http://<primary_ip>:8080 (110×110 px).
    Right column: title, IP addresses for every active interface
                  (wlan0 / eth0 / usb0), service statuses.
    """
    W, H = epd.height, epd.width   # 250 × 122 in landscape
    image = Image.new('1', (W, H), 255)
    draw  = ImageDraw.Draw(image)

    f_title = _load_font(_FONT_BOLD, 14)
    f_label = _load_font(_FONT_BOLD, 12)
    f_ip    = _load_font(_FONT_REG,  12)
    f_tiny  = _load_font(_FONT_REG,  11)

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
    y += 17
    draw.line((tx, y, W - 2, y), fill=0)
    y += 4

    # Only show rows for interfaces that actually have an IP
    any_ip = False
    for iface_label, ip in (('WiFi', w_ip), ('ETH', e_ip), ('USB', u_ip), ('BT', b_ip)):
        if ip:   # ip is None when not connected — skip
            draw.text((tx,      y), f"{iface_label}:", font=f_label, fill=0)
            draw.text((tx + 32, y), ip,                font=f_ip,    fill=0)
            y += 14
            any_ip = True
    if not any_ip:
        draw.text((tx, y), "No network", font=f_ip, fill=0)
        y += 14

    y += 3
    draw.line((tx, y, W - 2, y), fill=0)
    y += 4

    s_zc = get_service_status("zeroclaw")
    s_pc = get_service_status("picoclaw")
    draw.text((tx, y), f"ZC: {s_zc}", font=f_tiny, fill=0); y += 13
    draw.text((tx, y), f"PC: {s_pc}", font=f_tiny, fill=0)

    _epd_render(epd, image, force_full=force_full)


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


# ── LCD 1.69" Screens (240×280, portrait, RGB colour) ──────────────────────
_C_BG      = (245, 246, 250)   # off-white page background
_C_HDR_ZC  = ( 30, 100, 200)   # ZeroClaw blue header
_C_HDR_PC  = (120,   0, 180)   # PicoClaw purple header
_C_WHITE   = (255, 255, 255)
_C_GREEN   = ( 30, 160,  60)
_C_RED     = (210,  30,  30)
_C_GREY    = (130, 130, 130)
_C_DARK    = ( 30,  30,  30)
_IFACE_COL = {
    'WiFi': (  0, 150,  80),
    'ETH':  (  0, 100, 200),
    'USB':  (160,  80,   0),
    'BT':   (120,   0, 180),
}


def draw_monitor_lcd(disp):
    """Render the normal status screen for the 1.69\" LCD (240×280 portrait)."""
    W, H  = disp.width, disp.height   # 240 × 280
    image = Image.new('RGB', (W, H), _C_BG)
    draw  = ImageDraw.Draw(image)

    f_hdr   = _load_font(_FONT_BOLD, 18)
    f_label = _load_font(_FONT_BOLD, 13)
    f_body  = _load_font(_FONT_REG,  13)
    f_small = _load_font(_FONT_REG,  12)

    # Header bar
    draw.rectangle((0, 0, W, 40), fill=_C_HDR_ZC)
    draw.text((10, 9), 'ClawBerry', font=f_hdr, fill=_C_WHITE)

    # Gather IPs
    w_ip = get_ip_address('wlan0')
    e_ip = get_ip_address('eth0')
    b_ip = get_ip_address('bnep0')
    u_ip = get_ip_address('usb0')
    primary_ip = w_ip or e_ip or u_ip or b_ip

    # QR code centred below header
    QR_SIZE = 130
    QR_X    = (W - QR_SIZE) // 2
    QR_Y    = 48
    if primary_ip:
        qr_url = f'http://{primary_ip}:8080'
        try:
            qr_img = _generate_qr_image(qr_url, size=QR_SIZE).convert('RGB')
            image.paste(qr_img, (QR_X, QR_Y))
        except Exception as exc:
            logging.warning('QR generation failed: %s', exc)
            draw.rectangle((QR_X, QR_Y, QR_X + QR_SIZE, QR_Y + QR_SIZE),
                           outline=_C_GREY, width=1)
            draw.text((QR_X + 22, QR_Y + 55), 'QR err', font=f_body, fill=_C_RED)
    else:
        draw.rectangle((QR_X, QR_Y, QR_X + QR_SIZE, QR_Y + QR_SIZE),
                       outline=_C_GREY, width=1)
        draw.text((QR_X + 30, QR_Y + 55), 'No IP', font=f_body, fill=_C_GREY)

    # Interface rows
    y = QR_Y + QR_SIZE + 10
    any_ip = False
    for iface_label, ip in (('WiFi', w_ip), ('ETH', e_ip), ('USB', u_ip), ('BT', b_ip)):
        if ip:
            col = _IFACE_COL.get(iface_label, (80, 80, 80))
            draw.rectangle((6, y, 46, y + 18), fill=col)
            draw.text((8,  y + 2), iface_label, font=f_small, fill=_C_WHITE)
            draw.text((52, y + 2), ip,          font=f_small, fill=_C_DARK)
            y += 22
            any_ip = True
    if not any_ip:
        draw.text((10, y), 'No network', font=f_body, fill=_C_RED)
        y += 22

    # Divider
    y += 4
    draw.line((6, y, W - 6, y), fill=(200, 200, 200), width=1)
    y += 7

    # Service status rows
    for svc, status in (('ZeroClaw', get_service_status('zeroclaw')),
                        ('PicoClaw', get_service_status('picoclaw'))):
        col = _C_GREEN if status == 'Running' else _C_RED
        draw.ellipse((8, y + 3, 19, y + 14), fill=col)
        draw.text((25, y + 1), f'{svc}: {status}', font=f_small, fill=_C_DARK)
        y += 20

    disp.ShowImage(image)


def draw_paircode_lcd(disp, code):
    """Render the ZeroClaw pair-code screen on the 1.69\" LCD."""
    W, H  = disp.width, disp.height
    image = Image.new('RGB', (W, H), _C_BG)
    draw  = ImageDraw.Draw(image)

    f_hdr  = _load_font(_FONT_BOLD, 18)
    f_hint = _load_font(_FONT_REG,  14)

    draw.rectangle((0, 0, W, 40), fill=_C_HDR_ZC)
    draw.text((10, 9), 'ZeroClaw Pair Code', font=f_hdr, fill=_C_WHITE)

    # Auto-size code to fit width
    for fsize in (80, 66, 52, 40):
        f_code = _load_font(_FONT_BOLD, fsize)
        bbox   = draw.textbbox((0, 0), code, font=f_code)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if tw <= W * 0.92:
            break

    cy = 44 + (H - 44 - 30 - th) // 2
    draw.text(((W - tw) // 2, cy), code, font=f_code, fill=_C_DARK)

    hint = 'scan / type in app'
    hbbox = draw.textbbox((0, 0), hint, font=f_hint)
    draw.text(((W - (hbbox[2] - hbbox[0])) // 2, H - 28),
              hint, font=f_hint, fill=_C_GREY)

    disp.ShowImage(image)


def draw_picoclaw_qr_lcd(disp, url, token=''):
    """Render the PicoClaw pairing QR on the 1.69\" LCD."""
    W, H  = disp.width, disp.height
    image = Image.new('RGB', (W, H), _C_BG)
    draw  = ImageDraw.Draw(image)

    f_hdr   = _load_font(_FONT_BOLD, 18)
    f_small = _load_font(_FONT_REG,  12)

    draw.rectangle((0, 0, W, 40), fill=_C_HDR_PC)
    draw.text((10, 9), 'PicoClaw Pair QR', font=f_hdr, fill=_C_WHITE)

    QR_SIZE = min(H - 90, 170)
    QR_X    = (W - QR_SIZE) // 2
    QR_Y    = 48
    try:
        qr_img = _fetch_qr_image(url, size=QR_SIZE).convert('RGB')
        image.paste(qr_img, (QR_X, QR_Y))
    except Exception as e:
        logging.warning('Could not fetch QR image: %s', e)
        draw.rectangle((QR_X, QR_Y, QR_X + QR_SIZE, QR_Y + QR_SIZE),
                       outline=_C_GREY, width=2)
        draw.text((QR_X + 55, QR_Y + 75), 'QR', font=f_hdr, fill=_C_GREY)

    y = QR_Y + QR_SIZE + 8
    for line in textwrap.wrap(url, width=34)[:2]:
        draw.text((6, y), line, font=f_small, fill=(60, 60, 60))
        y += 16
    if token:
        tok_str = f'token: {token[:18]}...' if len(token) > 18 else f'token: {token}'
        draw.text((6, y), tok_str, font=f_small, fill=_C_GREY)

    disp.ShowImage(image)


# ── Dispatch wrappers (route to eink or LCD based on _display_type) ──────────
def _render_monitor(force_full=False):
    if _display_type == 'lcd':
        draw_monitor_lcd(_disp)
    else:
        draw_monitor(_disp, force_full=force_full)


def _render_paircode(code):
    if _display_type == 'lcd':
        draw_paircode_lcd(_disp, code)
    else:
        draw_paircode(_disp, code)


def _render_picoclaw_qr(url, token=''):
    if _display_type == 'lcd':
        draw_picoclaw_qr_lcd(_disp, url, token)
    else:
        draw_picoclaw_qr(_disp, url, token)


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


def _draw_request_screen(payload):
    """Render the temporary screen for *payload* using the active display."""
    kind = payload.get('kind', 'paircode')
    if kind == 'pico_qr':
        url   = str(payload.get('url',   '')).strip()
        token = str(payload.get('token', '')).strip()
        if url:
            _render_picoclaw_qr(url, token)
    else:
        code = str(payload.get('code', '')).strip()
        if code:
            _render_paircode(code)


# ── Main loop ─────────────────────────────────────────────────────────────
# Rendering is change-driven:
#   • clawberry_paircode.txt created/updated → immediate temporary screen
#   • IP address or service status change    → immediate monitor refresh
#   • MONITOR_FORCE_REFRESH_SECONDS elapsed  → periodic ghost-busting refresh
_detect_display()
logging.info('ClawBerry display service starting — display type: %s', _display_type)

last_state        = _get_current_state()
_render_monitor()
last_monitor_draw = time.monotonic()
last_file_mtime   = _file_mtime()   # capture mtime of any pre-existing request file
hold_until        = 0.0             # monotonic time until temp screen must not be overwritten
was_holding       = False           # True while a temporary screen is being shown

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
            raw_sec    = payload.get('seconds')
            seconds    = int(raw_sec) if raw_sec else _FALLBACK_HOLD_SECONDS
            hold_until = now + seconds
            kind       = payload.get('kind', 'paircode')
            logging.info("Display request (%s) — holding for %d s", kind, seconds)
            _draw_request_screen(payload)
            continue                        # skip state check this cycle
    else:
        last_file_mtime = cur_mtime         # keep in sync (tracks None when absent)

    # ── 2. While inside the hold window, don't overwrite the temp screen ──
    currently_holding = now < hold_until
    just_released     = was_holding and not currently_holding  # hold just expired this tick
    was_holding       = currently_holding

    if currently_holding:
        continue

    # ── 3. Check for network / service state changes ──────────────────────
    current_state = _get_current_state()
    age           = now - last_monitor_draw
    state_changed = current_state != last_state
    force_refresh = age >= MONITOR_FORCE_REFRESH_SECONDS

    if just_released or state_changed or force_refresh:
        if just_released:
            logging.info("Temporary screen hold expired — restoring monitor display")
        elif state_changed:
            changed = [k for k in current_state if current_state[k] != last_state.get(k)]
            logging.info("State change detected (%s) — updating display", ', '.join(changed))
        else:
            logging.info("Periodic forced refresh (%.0f s since last draw)", age)
        _render_monitor(force_full=force_refresh)
        last_state        = current_state
        last_monitor_draw = now
