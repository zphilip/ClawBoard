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

# Display drivers are imported lazily inside _detect_display().
# Module-level references are set once detection succeeds.
_eink_mod      = None   # waveshare_epd.epd2in13_V4 when e-ink is active
_lcd_mod       = None   # LCD_1inch69 module when LCD is active
_lcd_0in96_mod = None   # waveshare_lcd_rpi.LCD_0inch96 module when 0.96" LCD is active
_oled_mod      = None   # waveshare_OLED.OLED_0in96_rgb module when OLED is active

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
# Override file: create config/display_type.txt containing 'eink', 'lcd',
# 'lcd_0in96', or 'oled' to skip auto-detection and force a specific driver.
# Fallback hold duration used ONLY when the payload written by clawberry_paircode.py
# has no 'seconds' field. The primary source of truth is always the 'seconds' value
# inside clawberry_paircode.txt — change it there, not here.
_FALLBACK_HOLD_SECONDS        = 30
MONITOR_FORCE_REFRESH_SECONDS = 3600  # force a full monitor redraw after this many idle seconds (ghost-busting)
POLL_SECONDS = 1
_OLED_SCROLL_INTERVAL = 0.10   # seconds between OLED scroll frames (~10 FPS)

_FONT_BOLD = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
_FONT_REG  = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'

# ── Global display handle & type ────────────────────────────────────────
_disp         = None   # active display object (EPD, LCD_1inch69, LCD_0inch96, or OLED)
_display_type = None   # 'eink' | 'lcd' | 'lcd_0in96' | 'oled'
_qr_cache     = {}     # {(url, size): PIL.Image} — avoid regenerating unchanged QR codes
_oled_scroll_offset: int = 0   # advances each scroll frame; drives IP text animation


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
        if _display_type in ('lcd', 'lcd_0in96'):
            try:
                _disp.module_exit()
            except Exception as e:
                logging.warning("LCD module_exit failed: %s", e)
        elif _display_type == 'oled':
            try:
                _disp.module_exit()
            except Exception as e:
                logging.warning("OLED module_exit failed: %s", e)
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
      1. LCD  1.69\"  (LCD_1inch69)    — tried first; touch controller init
                                          fails reliably when hardware absent
      2. LCD  0.96\"  (LCD_0inch96)    — Waveshare 0.96" 160×80 LCD HAT
      3. E-ink 2.13\" (epd2in13_V4)   — SPI-only, always succeeds on a Pi
                                          even without display connected
      4. OLED 0.96\"  (OLED_0in96_rgb)

    To force a type, create config/display_type.txt with one of:
      'lcd', 'lcd_0in96', 'eink', 'oled', 'eink_radxa_1_54'
    Sets the module globals ``_disp``, ``_display_type``, ``_eink_mod`` /
    ``_lcd_mod`` / ``_lcd_0in96_mod`` / ``_oled_mod`` and returns the active
    display object.
    """
    global _disp, _display_type, _eink_mod, _lcd_mod, _lcd_0in96_mod, _oled_mod, _LCD_LANDSCAPE

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

    # Resolve orientation suffixes before the type check
    if _forced == 'lcd-landscape':
        _LCD_LANDSCAPE = True
        _forced = 'lcd'
    elif _forced == 'lcd-portrait':
        _LCD_LANDSCAPE = False
        _forced = 'lcd'

    # ── 1. Try LCD 1.69\" ────────────────────────────────────────────────
    if _forced not in ('eink', 'oled', 'eink_radxa_1_54', 'lcd_0in96'):
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

    # ── 1b. Try LCD 0.96" (Waveshare 160×80) ─────────────────────────────
    if _forced not in ('eink', 'oled', 'eink_radxa_1_54', 'lcd'):
        import importlib as _il
        _lcd0_candidates = [
            (os.path.join(current_dir, 'lib'), 'waveshare_lcd_rpi'),
            ('/opt/clawboard/lib',             'waveshare_lcd_rpi'),
        ]
        _lm0 = None
        for _parent, _pkg in _lcd0_candidates:
            _pkg_dir = os.path.join(_parent, _pkg)
            if not os.path.isdir(_pkg_dir):
                continue
            _init_py = os.path.join(_pkg_dir, '__init__.py')
            if not os.path.exists(_init_py):
                try:
                    open(_init_py, 'w').close()
                except Exception as _ie:
                    logging.warning('Could not create __init__.py: %s', _ie)
            if _parent not in sys.path:
                sys.path.insert(0, _parent)
            try:
                _lm0 = _il.import_module(f'{_pkg}.LCD_0inch96')
                logging.info('Loaded LCD_0inch96 module from %s/%s', _parent, _pkg)
                break
            except Exception as _ie:
                logging.info('Could not load %s.LCD_0inch96: %s', _pkg, _ie)
        if _lm0 is not None:
            try:
                _obj = _lm0.LCD_0inch96(
                    rst=_LCD_RST, dc=_LCD_DC, bl=_LCD_BL,
                    bl_freq=1000
                )
                _obj.Init()
                _obj.clear()
                _obj.bl_DutyCycle(80)
                _lcd_0in96_mod = _lm0
                _disp          = _obj
                _display_type  = 'lcd_0in96'
                logging.info('Display detected: LCD 0.96" 160×80 (LCD_0inch96)')
                return _obj
            except Exception as e:
                logging.info('LCD 0.96" not available: %s', e)

    # ── 2a. Radxa 1.54" e-ink (direct driver, no waveshare_epd needed) ──────────────
    if _forced == 'eink_radxa_1_54':
        try:
            from radxa_epd.epd_adapter import EPD as _RadxaEPD
            _obj = _RadxaEPD()
            _obj.init()
            _obj.sleep()
            _eink_mod     = None          # not a waveshare module
            _disp         = _obj
            _display_type = 'eink_radxa_1_54'
            logging.info('Display detected: e-ink Radxa 1.54" (radxa_epd.epd_adapter)')
            return _obj
        except Exception as e:
            logging.info('Radxa 1.54" e-ink not available: %s', e)

    # ── 2b. Try e-ink 2.13" Waveshare ────────────────────────────────────────────
    if _forced not in ('lcd', 'oled', 'eink_radxa_1_54'):
        try:
            from waveshare_epd import epd2in13_V4 as _em
            _obj = _em.EPD()
            _obj.init()
            _obj.sleep()
            _eink_mod     = _em
            _disp         = _obj
            _display_type = 'eink'
            logging.info('Display detected: e-ink 2.13" (epd2in13_V4)')
            return _obj
        except Exception as e:
            logging.info('E-ink 2.13" not available: %s', e)

    # ── 3. Try OLED 0.96" RGB ────────────────────────────────────────────
    if _forced not in ('lcd', 'eink', 'eink_radxa_1_54'):
        try:
            from waveshare_OLED import OLED_0in96_rgb as _om
            _obj = _om.OLED_0in96_rgb()
            _obj.Init()
            _obj.clear()
            _oled_mod     = _om
            _disp         = _obj
            _display_type = 'oled'
            logging.info('Display detected: OLED 0.96" RGB (OLED_0in96_rgb)')
            return _obj
        except Exception as e:
            logging.info('OLED 0.96" not available: %s', e)

    logging.warning(
        'No supported display detected '
        '(tried LCD 1.69", e-ink 2.13", OLED 0.96"). '
        'Running in headless mode (no rendering). '
        'Override file: %s',
        DISPLAY_TYPE_OVERRIDE_FILE,
    )
    _display_type = 'none'
    return None


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

    # Gather IPs
    w_ip = get_ip_address('wlan0')
    e_ip = get_ip_address('eth0')
    b_ip = get_ip_address('bnep0')
    u_ip = get_ip_address('usb0')
    primary_ip = w_ip or e_ip or u_ip or b_ip

    _is_radxa = (_display_type == 'eink_radxa_1_54')

    if _is_radxa:
        # ── Square layout 200×200 Radxa 1.54" ────────────────────────────
        # Title bar  (22 px)
        # Main zone  (100 px): QR left (96×96) | ZC/PC status right
        # Divider
        # IP zone    (remaining): all active IPs stacked

        f_title  = _load_font(_FONT_BOLD, 16)
        f_svc_lbl= _load_font(_FONT_BOLD, 13)
        f_svc_val= _load_font(_FONT_BOLD, 15)
        f_ip_lbl = _load_font(_FONT_BOLD, 13)
        f_ip_val = _load_font(_FONT_BOLD, 12)

        # ── Title bar ────────────────────────────────────────────────────
        TITLE_H = 22
        draw.text((5, 3), "ClawBerry", font=f_title, fill=0)
        draw.line((0, TITLE_H, W, TITLE_H), fill=0)

        # ── QR (left) ────────────────────────────────────────────────────
        QR_SIZE = 96
        QR_X, QR_Y = 2, TITLE_H + 2
        if primary_ip:
            qr_url = f'http://{primary_ip}:8080'
            try:
                qr_img = _generate_qr_image(qr_url, size=QR_SIZE)
                image.paste(qr_img, (QR_X, QR_Y))
            except Exception as exc:
                logging.warning("QR generation failed: %s", exc)
                draw.rectangle((QR_X, QR_Y, QR_X + QR_SIZE, QR_Y + QR_SIZE), outline=0, width=1)
                draw.text((QR_X + 18, QR_Y + 42), "QR err", font=f_ip_val, fill=0)
        else:
            draw.rectangle((QR_X, QR_Y, QR_X + QR_SIZE, QR_Y + QR_SIZE), outline=0, width=1)
            draw.text((QR_X + 22, QR_Y + 42), "No IP", font=f_ip_val, fill=0)

        # ── Service status (right of QR) ──────────────────────────────────
        s_zc = get_service_status("zeroclaw")
        s_pc = get_service_status("picoclaw")
        # Abbreviate service status to 4 chars max
        def _abbr(s):
            return {'Running': 'Run', 'Stopped': 'Stop', 'Unknown': 'Unk'}.get(s, s[:4])

        tx = QR_X + QR_SIZE + 5
        ty = TITLE_H + 10

        draw.text((tx, ty), "ZC:", font=f_svc_lbl, fill=0);  ty += 16
        draw.text((tx, ty), _abbr(s_zc), font=f_svc_val, fill=0); ty += 20
        draw.line((tx, ty, W - 2, ty), fill=0);  ty += 6
        draw.text((tx, ty), "PC:", font=f_svc_lbl, fill=0);  ty += 16
        draw.text((tx, ty), _abbr(s_pc), font=f_svc_val, fill=0)

        # ── Divider below QR / status ─────────────────────────────────────
        DIV_Y = TITLE_H + QR_SIZE + 4
        draw.line((0, DIV_Y, W, DIV_Y), fill=0)

        # ── IP addresses below divider ────────────────────────────────────
        iy = DIV_Y + 4
        any_ip = False
        for iface_lbl, ip in (('WiFi', w_ip), ('Eth', e_ip), ('USB', u_ip), ('BT', b_ip)):
            if ip and iy + 14 <= H:
                draw.text((4,  iy), f"{iface_lbl}:", font=f_ip_lbl, fill=0)
                draw.text((38, iy), ip,               font=f_ip_val, fill=0)
                iy += 15
                any_ip = True
        if not any_ip and iy + 14 <= H:
            draw.text((4, iy), "No network", font=f_ip_val, fill=0)

    else:
        # ── Landscape layout (original, e.g. 250×122 Waveshare 2.13") ────
        _is_radxa = False   # silence any future references
        f_title = _load_font(_FONT_BOLD, 14)
        f_label = _load_font(_FONT_BOLD, 12)
        f_ip    = _load_font(_FONT_REG,  12)
        f_tiny  = _load_font(_FONT_REG,  11)

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

        tx = QR_X + QR_SIZE + 5
        y  = 2
        draw.text((tx, y), "ClawBerry", font=f_title, fill=0)
        y += 17
        draw.line((tx, y, W - 2, y), fill=0)
        y += 4

        any_ip = False
        for iface_label, ip in (('WiFi', w_ip), ('ETH', e_ip), ('USB', u_ip), ('BT', b_ip)):
            if ip:
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

    _is_radxa = (_display_type == 'eink_radxa_1_54')
    title_str  = "ZeroClaw" if _is_radxa else "ZeroClaw Pair Code"
    title_fsize = 15 if _is_radxa else 16
    f_title = _load_font(_FONT_BOLD, title_fsize)
    f_hint  = _load_font(_FONT_REG,  12 if _is_radxa else 13)

    draw.text((8, 4), title_str, font=f_title, fill=0)
    TITLE_H = 22
    draw.line((4, TITLE_H, W - 4, TITLE_H), fill=0)

    # Sub-label
    if _is_radxa:
        f_sub = _load_font(_FONT_REG, 11)
        draw.text((8, TITLE_H + 2), "Pair Code", font=f_sub, fill=0)

    top = TITLE_H + (14 if _is_radxa else 4)

    # Auto-size the code to fit width
    max_sizes = (52, 44, 36, 28) if _is_radxa else (56, 48, 40, 32)
    for fsize in max_sizes:
        f_code = _load_font(_FONT_BOLD, fsize)
        bbox = draw.textbbox((0, 0), code, font=f_code)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if tw <= W * 0.9:
            break

    hint_h = 18
    avail  = H - top - hint_h
    draw.text(((W - tw) // 2, top + (avail - th) // 2), code, font=f_code, fill=0)

    hint = "scan / type in app"
    hbbox = draw.textbbox((0, 0), hint, font=f_hint)
    draw.text(((W - (hbbox[2] - hbbox[0])) // 2, H - hint_h), hint, font=f_hint, fill=0)

    _epd_render(epd, image)


def draw_picoclaw_qr(epd, url, token=''):
    """Render a PicoClaw pairing QR screen."""
    W, H = epd.height, epd.width
    image = Image.new('1', (W, H), 255)
    draw  = ImageDraw.Draw(image)

    _is_radxa = (_display_type == 'eink_radxa_1_54')
    f_title = _load_font(_FONT_BOLD, 14 if _is_radxa else 15)
    f_small = _load_font(_FONT_REG,  10 if _is_radxa else 12)
    f_tiny  = _load_font(_FONT_REG,  9  if _is_radxa else 10)

    draw.text((8, 3), "PicoClaw Pair QR", font=f_title, fill=0)
    TITLE_H = 20
    draw.line((4, TITLE_H, W - 4, TITLE_H), fill=0)

    if _is_radxa:
        # Square: large centred QR, URL + token below
        QR_SIZE = min(W - 16, 140)
        QR_X    = (W - QR_SIZE) // 2
        QR_Y    = TITLE_H + 4
        try:
            qr_img = _generate_qr_image(url, size=QR_SIZE)
            image.paste(qr_img, (QR_X, QR_Y))
        except Exception as e:
            logging.warning("Could not fetch QR image: %s", e)
            draw.rectangle((QR_X, QR_Y, QR_X + QR_SIZE, QR_Y + QR_SIZE), outline=0, width=2)
            draw.text((QR_X + QR_SIZE // 2 - 10, QR_Y + QR_SIZE // 2 - 8), "QR", font=f_title, fill=0)

        ty = QR_Y + QR_SIZE + 4
        draw.line((4, ty, W - 4, ty), fill=0)
        ty += 3
        for line in textwrap.wrap(url, width=28)[:2]:
            draw.text((4, ty), line, font=f_tiny, fill=0)
            ty += 11
        if token and ty < H - 10:
            tok = f"{token[:20]}..." if len(token) > 20 else token
            draw.text((4, H - 11), tok, font=f_tiny, fill=0)
    else:
        # Landscape: QR left, URL right
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

# ── LCD orientation ──────────────────────────────────────────────────────
# True  → landscape 280×240  (default)
# False → portrait  240×280
# Override via config/display_type.txt: write 'lcd-landscape' or 'lcd-portrait'
_LCD_LANDSCAPE = True

def _lcd_dims(disp):
    """Return (W, H) for the active LCD orientation."""
    if _LCD_LANDSCAPE:
        return disp.height, disp.width   # 280 × 240
    return disp.width, disp.height       # 240 × 280


def draw_monitor_lcd(disp):
    """Render the normal status screen for the 1.69\" LCD.
    Landscape (280×240): QR left, info right.
    Portrait  (240×280): QR centred, info stacked below.
    """
    W, H  = _lcd_dims(disp)
    image = Image.new('RGB', (W, H), _C_BG)
    draw  = ImageDraw.Draw(image)

    f_hdr   = _load_font(_FONT_BOLD, 18)
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

    def _draw_qr(qx, qy, qsize):
        if primary_ip:
            qr_url = f'http://{primary_ip}:8080'
            try:
                qr_img = _generate_qr_image(qr_url, size=qsize).convert('RGB')
                image.paste(qr_img, (qx, qy))
            except Exception as exc:
                logging.warning('QR generation failed: %s', exc)
                draw.rectangle((qx, qy, qx + qsize, qy + qsize), outline=_C_GREY, width=1)
                draw.text((qx + 14, qy + qsize // 2 - 7), 'QR err', font=f_body, fill=_C_RED)
        else:
            draw.rectangle((qx, qy, qx + qsize, qy + qsize), outline=_C_GREY, width=1)
            draw.text((qx + 18, qy + qsize // 2 - 7), 'No IP', font=f_body, fill=_C_GREY)

    def _draw_ifaces_and_svcs(ix, iy):
        any_ip = False
        y = iy
        for iface_label, ip in (('WiFi', w_ip), ('ETH', e_ip), ('USB', u_ip), ('BT', b_ip)):
            if ip:
                col = _IFACE_COL.get(iface_label, (80, 80, 80))
                draw.rectangle((ix, y, ix + 40, y + 18), fill=col)
                draw.text((ix + 2,  y + 2), iface_label, font=f_small, fill=_C_WHITE)
                draw.text((ix + 46, y + 2), ip,          font=f_small, fill=_C_DARK)
                y += 22
                any_ip = True
        if not any_ip:
            draw.text((ix, y), 'No network', font=f_body, fill=_C_RED)
            y += 22
        y += 4
        draw.line((ix, y, W - 6, y), fill=(200, 200, 200), width=1)
        y += 7
        for svc, status in (('ZeroClaw', get_service_status('zeroclaw')),
                            ('PicoClaw', get_service_status('picoclaw'))):
            col = _C_GREEN if status == 'Running' else _C_RED
            draw.ellipse((ix, y + 3, ix + 11, y + 14), fill=col)
            draw.text((ix + 16, y + 1), f'{svc}: {status}', font=f_small, fill=_C_DARK)
            y += 20

    if _LCD_LANDSCAPE:
        # Landscape: QR left, info panel right
        QR_SIZE = 128
        QR_X, QR_Y = 6, 40 + (H - 40 - QR_SIZE) // 2
        _draw_qr(QR_X, QR_Y, QR_SIZE)
        _draw_ifaces_and_svcs(QR_X + QR_SIZE + 8, 48)
    else:
        # Portrait: QR centred below header, info below
        QR_SIZE = 120
        QR_X = (W - QR_SIZE) // 2
        QR_Y = 48
        _draw_qr(QR_X, QR_Y, QR_SIZE)
        _draw_ifaces_and_svcs(6, QR_Y + QR_SIZE + 10)

    disp.ShowImage(image)


def draw_paircode_lcd(disp, code):
    """Render the ZeroClaw pair-code screen on the 1.69\" LCD.
    Auto-sizes the code to fill the available width in either orientation.
    """
    W, H  = _lcd_dims(disp)
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
    """Render the PicoClaw pairing QR on the 1.69\" LCD.
    Landscape (280×240): QR left, URL/token right.
    Portrait  (240×280): QR centred, URL/token below.
    """
    W, H  = _lcd_dims(disp)
    image = Image.new('RGB', (W, H), _C_BG)
    draw  = ImageDraw.Draw(image)

    f_hdr   = _load_font(_FONT_BOLD, 18)
    f_small = _load_font(_FONT_REG,  12)
    f_tiny  = _load_font(_FONT_REG,  11)

    draw.rectangle((0, 0, W, 40), fill=_C_HDR_PC)
    draw.text((10, 9), 'PicoClaw Pair QR', font=f_hdr, fill=_C_WHITE)

    def _paste_qr(qx, qy, qsize):
        try:
            qr_img = _fetch_qr_image(url, size=qsize).convert('RGB')
            image.paste(qr_img, (qx, qy))
        except Exception as e:
            logging.warning('Could not fetch QR image: %s', e)
            draw.rectangle((qx, qy, qx + qsize, qy + qsize), outline=_C_GREY, width=2)
            draw.text((qx + qsize // 2 - 12, qy + qsize // 2 - 10), 'QR', font=f_hdr, fill=_C_GREY)

    if _LCD_LANDSCAPE:
        # QR left, URL/token right
        QR_SIZE = min(H - 50, 150)
        QR_X, QR_Y = 6, 40 + (H - 40 - QR_SIZE) // 2
        _paste_qr(QR_X, QR_Y, QR_SIZE)
        tx, ty = QR_X + QR_SIZE + 8, 48
        for line in textwrap.wrap(url, width=16)[:4]:
            draw.text((tx, ty), line, font=f_small, fill=(60, 60, 60))
            ty += 15
        if token:
            tok_str = f'tok:{token[:14]}...' if len(token) > 14 else f'tok:{token}'
            draw.text((tx, H - 20), tok_str, font=f_tiny, fill=_C_GREY)
    else:
        # QR centred, URL/token below
        QR_SIZE = min(W - 20, 180)
        QR_X = (W - QR_SIZE) // 2
        QR_Y = 48
        _paste_qr(QR_X, QR_Y, QR_SIZE)
        ty = QR_Y + QR_SIZE + 8
        for line in textwrap.wrap(url, width=30)[:3]:
            draw.text((6, ty), line, font=f_small, fill=(60, 60, 60))
            ty += 15
        if token:
            tok_str = f'tok:{token[:22]}...' if len(token) > 22 else f'tok:{token}'
            draw.text((6, H - 20), tok_str, font=f_tiny, fill=_C_GREY)

    disp.ShowImage(image)


# ── OLED 0.96" RGB Screens (128×64 landscape, RGB colour) ─────────────────────
# Physical: width=64, height=128 (portrait). We draw on a 128×64 canvas
# then rotate 270° before passing to getbuffer so the image fills the screen.

_OLED_W = 128   # canvas width  (landscape)
_OLED_H =  64   # canvas height (landscape)


def _oled_scrolling_text(image, draw, x, y, text, font, fill, avail_w, row_h=11):
    """Draw *text* at (x, y) clipped to *avail_w* pixels wide.

    If the text fits it is drawn normally.  If it is wider it scrolls
    horizontally using ``_oled_scroll_offset`` — a seamless loop with a
    short blank gap between repetitions.
    """
    global _oled_scroll_offset
    bbox   = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    if text_w <= avail_w:
        draw.text((x, y), text, font=font, fill=fill)
        return
    SCROLL_GAP = 18                          # blank pixels between repeats
    cycle      = text_w + SCROLL_GAP
    off        = _oled_scroll_offset % cycle
    strip_w    = cycle + avail_w             # always avail_w pixels available from off
    strip      = Image.new('RGB', (strip_w, row_h), (0, 0, 0))
    sd         = ImageDraw.Draw(strip)
    sd.text((0,     0), text, font=font, fill=fill)   # first copy
    sd.text((cycle, 0), text, font=font, fill=fill)   # second copy for seamless wrap
    crop = strip.crop((off, 0, off + avail_w, row_h))
    image.paste(crop, (x, y))


def _oled_show(disp, image: 'Image.Image') -> None:
    """Push a 128×64 RGB PIL image to the OLED.

    The OLED physical orientation is portrait (width=64, height=128), so the
    128×64 landscape canvas must be rotated 270° (→ 64×128) before handing
    off to getbuffer / ShowImage.
    """
    rotated = image.rotate(270, expand=True)   # 128×64 → 64×128
    disp.ShowImage(disp.getbuffer(rotated))


def draw_monitor_oled(disp):
    """Render the status screen on the 0.96\" OLED (128×64 landscape)."""
    W, H = _OLED_W, _OLED_H
    image = Image.new('RGB', (W, H), (0, 0, 0))   # black background
    draw  = ImageDraw.Draw(image)

    f_title = _load_font(_FONT_BOLD, 10)
    f_small = _load_font(_FONT_REG,   9)

    # Gather IPs
    w_ip = get_ip_address('wlan0')
    e_ip = get_ip_address('eth0')
    b_ip = get_ip_address('bnep0')
    u_ip = get_ip_address('usb0')
    primary_ip = w_ip or e_ip or u_ip or b_ip

    # ── QR left ─────────────────────────────────────────────────────────
    QR_SIZE = 52
    QR_X, QR_Y = 2, (H - QR_SIZE) // 2
    if primary_ip:
        qr_url = f'http://{primary_ip}:8080'
        try:
            qr_img = _generate_qr_image(qr_url, size=QR_SIZE).convert('RGB')
            image.paste(qr_img, (QR_X, QR_Y))
        except Exception as exc:
            logging.warning('OLED QR generation failed: %s', exc)
            draw.rectangle((QR_X, QR_Y, QR_X + QR_SIZE, QR_Y + QR_SIZE),
                           outline=(255, 255, 255), width=1)
            draw.text((QR_X + 4, QR_Y + 20), 'QR?', font=f_small, fill=(255, 80, 80))
    else:
        draw.rectangle((QR_X, QR_Y, QR_X + QR_SIZE, QR_Y + QR_SIZE),
                       outline=(100, 100, 100), width=1)
        draw.text((QR_X + 6, QR_Y + 20), 'No IP', font=f_small, fill=(150, 150, 150))

    # ── Right panel ──────────────────────────────────────────────────────
    tx = QR_X + QR_SIZE + 4
    y  = 1

    draw.text((tx, y), 'ClawBerry', font=f_title, fill=(80, 160, 255))
    y += 11
    draw.line((tx, y, W - 1, y), fill=(60, 60, 60))
    y += 3

    any_ip = False
    _OLED_IFACE_COL = {
        'WiFi': (  0, 200, 100),
        'ETH':  ( 80, 160, 255),
        'USB':  (255, 160,  40),
        'BT':   (200,  80, 255),
    }
    ip_avail_w = W - 1 - (tx + 27)   # pixels available right of the label badge
    for label, ip in (('WiFi', w_ip), ('ETH', e_ip), ('USB', u_ip), ('BT', b_ip)):
        if ip:
            col = _OLED_IFACE_COL.get(label, (180, 180, 180))
            draw.text((tx,      y), f'{label}:', font=f_small, fill=col)
            _oled_scrolling_text(image, draw, tx + 27, y, ip,
                                 f_small, (220, 220, 220), ip_avail_w)
            y += 11
            any_ip = True
    if not any_ip:
        draw.text((tx, y), 'No network', font=f_small, fill=(255, 80, 80))
        y += 11

    y += 2
    draw.line((tx, y, W - 1, y), fill=(60, 60, 60))
    y += 3

    s_zc = get_service_status('zeroclaw')
    s_pc = get_service_status('picoclaw')
    for svc, status in (('ZC', s_zc), ('PC', s_pc)):
        col = (0, 200, 80) if status == 'Running' else (255, 60, 60)
        draw.ellipse((tx, y + 2, tx + 7, y + 9), fill=col)
        draw.text((tx + 10, y), f'{svc}: {status}', font=f_small, fill=(200, 200, 200))
        y += 11

    _oled_show(disp, image)


def draw_paircode_oled(disp, code):
    """Render the pair-code screen on the 0.96\" OLED."""
    W, H  = _OLED_W, _OLED_H
    image = Image.new('RGB', (W, H), (0, 0, 0))
    draw  = ImageDraw.Draw(image)

    f_hint = _load_font(_FONT_REG, 9)

    draw.text((2, 1), 'Pair Code', font=_load_font(_FONT_BOLD, 10), fill=(80, 160, 255))
    draw.line((2, 13, W - 2, 13), fill=(40, 40, 80))

    # Auto-size code
    for fsize in (36, 28, 22, 16):
        f_code = _load_font(_FONT_BOLD, fsize)
        bbox   = draw.textbbox((0, 0), code, font=f_code)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if tw <= W - 8:
            break

    cy = 15 + (H - 15 - 14 - th) // 2
    draw.text(((W - tw) // 2, cy), code, font=f_code, fill=(255, 220, 80))

    hint = 'scan / type in app'
    hbbox = draw.textbbox((0, 0), hint, font=f_hint)
    draw.text(((W - (hbbox[2] - hbbox[0])) // 2, H - 11),
              hint, font=f_hint, fill=(120, 120, 120))

    _oled_show(disp, image)


def draw_picoclaw_qr_oled(disp, url, token=''):
    """Render a PicoClaw pairing QR on the 0.96\" OLED."""
    W, H  = _OLED_W, _OLED_H
    image = Image.new('RGB', (W, H), (0, 0, 0))
    draw  = ImageDraw.Draw(image)

    f_hdr   = _load_font(_FONT_BOLD, 10)
    f_small = _load_font(_FONT_REG,   9)

    draw.text((2, 1), 'PicoClaw QR', font=f_hdr, fill=(200, 80, 255))
    draw.line((2, 13, W - 2, 13), fill=(60, 20, 80))

    QR_SIZE = 48
    QR_X, QR_Y = 2, 15
    try:
        qr_img = _generate_qr_image(url, size=QR_SIZE).convert('RGB')
        image.paste(qr_img, (QR_X, QR_Y))
    except Exception as e:
        logging.warning('OLED QR fetch failed: %s', e)
        draw.rectangle((QR_X, QR_Y, QR_X + QR_SIZE, QR_Y + QR_SIZE),
                       outline=(200, 80, 255), width=1)
        draw.text((QR_X + 10, QR_Y + 18), 'QR', font=f_hdr, fill=(200, 80, 255))

    tx, ty = QR_X + QR_SIZE + 4, 16
    for line in textwrap.wrap(url, width=14)[:3]:
        draw.text((tx, ty), line, font=f_small, fill=(180, 180, 180))
        ty += 11
    if token:
        tok = f'{token[:10]}...' if len(token) > 10 else token
        draw.text((tx, H - 11), tok, font=f_small, fill=(120, 120, 120))

    _oled_show(disp, image)


# ── LCD 0.96" 160×80 Screens ─────────────────────────────────────────────────
# Physical canvas is 160×80 (landscape, W×H).  There is no room for a QR code,
# so the monitor screen shows a compact text layout.

_LCD0_W = 160
_LCD0_H =  80


def draw_monitor_lcd_0in96(disp):
    """Render the status screen on the 0.96\" 160×80 LCD.
    Layout: QR code left (58×58), info panel right.
    """
    W, H = _LCD0_W, _LCD0_H
    image = Image.new('RGB', (W, H), _C_BG)
    draw  = ImageDraw.Draw(image)

    f_hdr   = _load_font(_FONT_BOLD, 11)
    f_small = _load_font(_FONT_REG,   9)

    # Header bar
    draw.rectangle((0, 0, W, 18), fill=_C_HDR_ZC)
    draw.text((4, 3), 'ClawBerry', font=f_hdr, fill=_C_WHITE)

    w_ip = get_ip_address('wlan0')
    e_ip = get_ip_address('eth0')
    b_ip = get_ip_address('bnep0')
    u_ip = get_ip_address('usb0')
    primary_ip = w_ip or e_ip or u_ip or b_ip

    # QR left
    QR_SIZE = 58
    QR_X, QR_Y = 2, 20
    if primary_ip:
        qr_url = f'http://{primary_ip}:8080'
        try:
            qr_img = _generate_qr_image(qr_url, size=QR_SIZE).convert('RGB')
            image.paste(qr_img, (QR_X, QR_Y))
        except Exception as exc:
            logging.warning('LCD0 QR generation failed: %s', exc)
            draw.rectangle((QR_X, QR_Y, QR_X + QR_SIZE, QR_Y + QR_SIZE),
                           outline=_C_GREY, width=1)
            draw.text((QR_X + 4, QR_Y + 22), 'QR err', font=f_small, fill=_C_RED)
    else:
        draw.rectangle((QR_X, QR_Y, QR_X + QR_SIZE, QR_Y + QR_SIZE),
                       outline=_C_GREY, width=1)
        draw.text((QR_X + 6, QR_Y + 22), 'No IP', font=f_small, fill=_C_GREY)

    # Info panel right
    tx, y = QR_X + QR_SIZE + 4, 20
    any_ip = False
    for label, ip in (('W', w_ip), ('E', e_ip), ('U', u_ip), ('B', b_ip)):
        if ip:
            col = _IFACE_COL.get(
                {'W': 'WiFi', 'E': 'ETH', 'U': 'USB', 'B': 'BT'}[label],
                (80, 80, 80))
            draw.rectangle((tx, y, tx + 10, y + 10), fill=col)
            draw.text((tx + 1, y + 1), label, font=f_small, fill=_C_WHITE)
            draw.text((tx + 13, y + 1), ip,   font=f_small, fill=_C_DARK)
            y += 12
            any_ip = True
    if not any_ip:
        draw.text((tx, y), 'No net', font=f_small, fill=_C_RED)
        y += 12

    y += 2
    for svc, status in (('ZC', get_service_status('zeroclaw')),
                        ('PC', get_service_status('picoclaw'))):
        col = _C_GREEN if status == 'Running' else _C_RED
        draw.ellipse((tx, y + 1, tx + 8, y + 9), fill=col)
        draw.text((tx + 11, y), f'{svc}: {status}', font=f_small, fill=_C_DARK)
        y += 11

    disp.ShowImage(image)


def draw_paircode_lcd_0in96(disp, code):
    """Render the ZeroClaw pair-code screen on the 0.96\" 160×80 LCD."""
    W, H = _LCD0_W, _LCD0_H
    image = Image.new('RGB', (W, H), _C_BG)
    draw  = ImageDraw.Draw(image)

    f_hdr  = _load_font(_FONT_BOLD, 11)
    f_hint = _load_font(_FONT_REG,   9)

    draw.rectangle((0, 0, W, 18), fill=_C_HDR_ZC)
    draw.text((4, 3), 'ZeroClaw Pair Code', font=f_hdr, fill=_C_WHITE)

    for fsize in (36, 28, 22, 16):
        f_code = _load_font(_FONT_BOLD, fsize)
        bbox   = draw.textbbox((0, 0), code, font=f_code)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if tw <= W - 8:
            break

    cy = 20 + (H - 20 - 14 - th) // 2
    draw.text(((W - tw) // 2, cy), code, font=f_code, fill=_C_DARK)

    hint = 'scan / type in app'
    hbbox = draw.textbbox((0, 0), hint, font=f_hint)
    draw.text(((W - (hbbox[2] - hbbox[0])) // 2, H - 12),
              hint, font=f_hint, fill=_C_GREY)

    disp.ShowImage(image)


def draw_picoclaw_qr_lcd_0in96(disp, url, token=''):
    """Render the PicoClaw pairing QR on the 0.96\" 160×80 LCD.
    Layout: QR code left (58×58), URL text right.
    """
    W, H = _LCD0_W, _LCD0_H
    image = Image.new('RGB', (W, H), _C_BG)
    draw  = ImageDraw.Draw(image)

    f_hdr   = _load_font(_FONT_BOLD, 11)
    f_small = _load_font(_FONT_REG,   9)
    f_tiny  = _load_font(_FONT_REG,   8)

    draw.rectangle((0, 0, W, 18), fill=_C_HDR_PC)
    draw.text((4, 3), 'PicoClaw Pair', font=f_hdr, fill=_C_WHITE)

    # QR left
    QR_SIZE = 58
    QR_X, QR_Y = 2, 20
    try:
        qr_img = _fetch_qr_image(url, size=QR_SIZE).convert('RGB')
        image.paste(qr_img, (QR_X, QR_Y))
    except Exception as e:
        logging.warning('LCD0 QR fetch failed: %s', e)
        draw.rectangle((QR_X, QR_Y, QR_X + QR_SIZE, QR_Y + QR_SIZE),
                       outline=_C_GREY, width=1)
        draw.text((QR_X + 14, QR_Y + 22), 'QR', font=f_hdr, fill=_C_GREY)

    # URL text right
    tx, ty = QR_X + QR_SIZE + 4, 21
    for line in textwrap.wrap(url, width=14)[:4]:
        draw.text((tx, ty), line, font=f_small, fill=_C_DARK)
        ty += 12
    if token:
        tok = f'{token[:10]}...' if len(token) > 10 else token
        draw.text((tx, H - 10), tok, font=f_tiny, fill=_C_GREY)

    disp.ShowImage(image)


# ── Dispatch wrappers (route to eink or LCD based on _display_type) ──────────
def _render_monitor(force_full=False):
    if _disp is None:
        return
    if _display_type == 'lcd':
        draw_monitor_lcd(_disp)
    elif _display_type == 'lcd_0in96':
        draw_monitor_lcd_0in96(_disp)
    elif _display_type == 'oled':
        draw_monitor_oled(_disp)
    else:
        draw_monitor(_disp, force_full=force_full)


def _render_paircode(code):
    if _disp is None:
        return
    if _display_type == 'lcd':
        draw_paircode_lcd(_disp, code)
    elif _display_type == 'lcd_0in96':
        draw_paircode_lcd_0in96(_disp, code)
    elif _display_type == 'oled':
        draw_paircode_oled(_disp, code)
    else:
        draw_paircode(_disp, code)


def _render_picoclaw_qr(url, token=''):
    if _disp is None:
        return
    if _display_type == 'lcd':
        draw_picoclaw_qr_lcd(_disp, url, token)
    elif _display_type == 'lcd_0in96':
        draw_picoclaw_qr_lcd_0in96(_disp, url, token)
    elif _display_type == 'oled':
        draw_picoclaw_qr_oled(_disp, url, token)
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
logging.info('ClawBerry display service starting — display type: %s',
             _display_type or 'none (headless)')

last_state        = _get_current_state()
_render_monitor()
last_monitor_draw = time.monotonic()
last_file_mtime   = _file_mtime()   # capture mtime of any pre-existing request file
hold_until        = 0.0             # monotonic time until temp screen must not be overwritten
was_holding       = False           # True while a temporary screen is being shown

while True:
    time.sleep(_OLED_SCROLL_INTERVAL if _display_type == 'oled' else POLL_SECONDS)
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

    # ── 3. OLED: advance scroll offset and redraw every frame ─────────────
    # State changes are picked up each frame; no separate change-detection
    # path is needed because we're already redrawing continuously.
    if _display_type == 'oled':
        _oled_scroll_offset = (_oled_scroll_offset + 3) % 100000
        current_state = _get_current_state()
        if current_state != last_state:
            changed = [k for k in current_state if current_state[k] != last_state.get(k)]
            logging.info("State change detected (%s)", ', '.join(changed))
            last_state = current_state
        if just_released:
            logging.info("Temporary screen hold expired — restoring monitor display")
        _render_monitor()
        last_monitor_draw = now
        continue

    # ── 4. (non-OLED) Check for network / service state changes ──────────
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
