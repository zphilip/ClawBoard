#!/usr/bin/env python3
"""
extract_tokens.py — Agent-friendly Xiaomi Cloud token extractor.

Authenticates via QR code (no password input required), fetches all device
tokens from Xiaomi Cloud, and saves results to:
  references/devices.json   — full structured data
  references/devices.md     — Markdown table (compatible with xiaomi-home)

Machine-readable stdout lines (for the agent to parse):
  QR_SERVER=http://<ip>:<port>/qr/<token>  — single-use URL serving the QR image
  QR_IMAGE_URL=http://<ip>:<port>/qr/<token>  — same URL, use this verbatim value to show the user
  QR_URL=https://account.xiaomi.com/…  — direct Mi Account login URL
  QR_IMAGE_B64=<base64-encoded PNG>     — QR image inline (for agent/UI display)
  STATUS=waiting_for_scan               — QR presented, waiting
  STATUS=login_success                  — user scanned successfully
  STATUS=login_timeout                  — no scan within --timeout seconds
  STATUS=login_failed                   — auth error
  DEVICE=<json>                         — one device object (see schema below)
  DEVICES_SAVED=<path>                  — JSON file written
  DONE count=<n> json=<path> md=<path> — all finished

Device JSON schema:
  {"server":"cn","home_id":"…","name":"…","did":"…","ip":"…","token":"…",
   "mac":"…","model":"…","ble_key":null}

Usage:
  python3 extract_tokens.py [--server SERVER] [--filter TEXT]
                            [--host IP] [--port PORT] [--timeout SECS]
                            [--output-dir DIR]
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import random
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import requests

try:
    from Crypto.Cipher import ARC4
except ModuleNotFoundError:
    try:
        from Cryptodome.Cipher import ARC4
    except ModuleNotFoundError:
        print("ERROR: pycryptodome not installed. Run: pip3 install pycryptodome", flush=True)
        sys.exit(1)

# ── Globals ────────────────────────────────────────────────────────────────────
SERVERS = ["cn", "de", "us", "ru", "tw", "sg", "in", "i2"]
# Random single-use path token for the QR endpoint — generated fresh each run.
# Prevents drive-by fetches: only whoever holds the emitted QR_SERVER URL can
# retrieve the image.
_QR_PATH_TOKEN: str  = secrets.token_hex(16)
_qr_httpd: HTTPServer | None = None   # kept so we can shut it down after auth

# Xiaomi domains that are allowed to appear in server-provided URLs (qr, lp).
# Any URL pointing elsewhere is rejected to prevent SSRF.
_ALLOWED_MI_NETLOCS = (
    "account.xiaomi.com",
    "sts.api.io.mi.com",
)

def _assert_mi_url(url: str, label: str) -> None:
    """Raise ValueError if *url* does not point to a trusted Xiaomi domain."""
    host = urlparse(url).netloc.split(":")[0]
    if not (host in _ALLOWED_MI_NETLOCS
            or host.endswith(".xiaomi.com")
            or host.endswith(".mi.com")):
        raise ValueError(
            f"Untrusted domain in server-provided {label!r}: {host!r} — "
            "aborting to prevent SSRF"
        )
# ── Argument parsing ───────────────────────────────────────────────────────────
_ap = argparse.ArgumentParser(
    description="Extract Xiaomi device tokens via QR-code cloud login."
)
_ap.add_argument("--server",     "-s", default=None,  choices=SERVERS,
                 help="Cloud server (default: scan all)")
_ap.add_argument("--filter",     "-f", default=None,
                 help="Only include devices whose name contains this string (case-insensitive)")
_ap.add_argument("--host",              default=None,
                 help="Override host IP/hostname for QR server URL (auto-detected by default)")
_ap.add_argument("--port",       "-p", default=31415, type=int,
                 help="Port for QR image HTTP server (default: 31415)")
_ap.add_argument("--timeout",    "-t", default=120,   type=int,
                 help="Seconds to wait for QR scan (default: 120)")
_ap.add_argument("--retries",    "-r", default=2,     type=int,
                 help="How many times to re-generate the QR if it expires before the user scans "
                      "(default: 2; total attempts = retries + 1)")
_ap.add_argument("--output-dir", "-o", default=None,
                 help="Directory for devices.json and devices.md "
                      "(default: ../references/ relative to this script)")
ARGS = _ap.parse_args()

# Output directory: default is ../references/ relative to this script
if ARGS.output_dir:
    OUTPUT_DIR = Path(ARGS.output_dir)
else:
    OUTPUT_DIR = Path(__file__).parent.parent / "references"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Helpers ────────────────────────────────────────────────────────────────────

def _emit(line: str) -> None:
    """Write a machine-readable status line to stdout immediately."""
    print(line, flush=True)


def _get_local_ip() -> str:
    """Best-effort detection of the outbound local IP address.

    Tries several strategies in order:
      1. --host CLI override
      2. UDP routing trick against multiple public targets (no packets sent)
      3. Hostname-to-address resolution
      4. Enumerate all non-loopback AF_INET addresses via getaddrinfo
    Falls back to 127.0.0.1 and emits a warning if nothing else works.
    """
    if ARGS.host:
        return ARGS.host

    # Strategy 1: UDP routing trick — kernel fills in the source IP without
    # actually sending any packets. Try several targets for robustness.
    for target in ("8.8.8.8", "1.1.1.1", "192.168.1.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.1)
            s.connect((target, 80))
            ip = s.getsockname()[0]
            s.close()
            if not ip.startswith("127."):
                return ip
        except Exception:
            pass

    # Strategy 2: hostname → address lookup
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if not ip.startswith("127."):
            return ip
    except Exception:
        pass

    # Strategy 3: enumerate all AF_INET addresses reported by the OS
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                return ip
    except Exception:
        pass

    # Nothing worked — server will only be reachable from localhost
    _emit("WARNING=local_ip_detection_failed QR server accessible at 127.0.0.1 only; "
          "pass --host <your-ip> to override")
    return "127.0.0.1"


# ── QR image HTTP server ───────────────────────────────────────────────────────

_qr_image_data: bytes = b""


def _decode_qr_url(png_data: bytes) -> str | None:
    """Extract the URL encoded inside a QR-code PNG.

    The pre-generated PNG served by Xiaomi encodes the *exact* URL that the
    Mi Home app reads when it scans the image.  This URL is tied to the
    current QR session and is different from ``loginUrl``, which is a
    browser-based fallback that requires existing Mi Account cookies and
    expires independently.

    Tries two optional QR-decoding libraries in order:
      1. ``pyzbar``   (``pip install pyzbar`` + ``apt install libzbar0``)
      2. ``zxingcpp`` (``pip install zxingcpp``, pure-Python wheels available)

    Returns the decoded URL string, or ``None`` if neither library is
    installed / decoding fails.  Callers should fall back to ``loginUrl``.
    """
    if not png_data:
        return None
    for _lib in ("pyzbar", "zxingcpp"):
        try:
            from PIL import Image as _PIL
            img = _PIL.open(BytesIO(png_data))
            if _lib == "pyzbar":
                from pyzbar.pyzbar import decode as _pyzbar_decode  # type: ignore
                results = _pyzbar_decode(img)
                if results:
                    raw = results[0].data
                    return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            else:
                import zxingcpp as _zxing  # type: ignore
                results = _zxing.read_barcodes(img)
                if results:
                    return str(results[0].text)
        except Exception:
            continue
    return None


def _print_qr_art(url: str) -> None:
    """Print the QR code as Unicode half-block art to stderr.

    Tries the ``qrcode`` library first (clean vector rendering), then falls
    back to converting the already-downloaded PNG via PIL.  Errors are
    silently swallowed so a missing dependency never breaks the auth flow.
    """
    # Attempt 1: qrcode library — clean, no dependency on the downloaded PNG
    try:
        import qrcode as _qrcode
        qr = _qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        print("", file=sys.stderr)   # blank line before art
        qr.print_ascii(out=sys.stderr, invert=True)
        print("", file=sys.stderr)
        return
    except Exception:
        pass

    # Attempt 2: convert downloaded PNG to Unicode half-block characters via PIL
    try:
        from PIL import Image as _PILImage
        img = (
            _PILImage.open(BytesIO(_qr_image_data))
            .convert("1")
            .resize((58, 58), _PILImage.NEAREST)
        )
        w, h = img.size
        px   = list(img.getdata())
        print("", file=sys.stderr)
        for row in range(0, h - 1, 2):
            line = ""
            for col in range(w):
                top = px[row * w + col] == 0
                bot = px[(row + 1) * w + col] == 0
                if top and bot:
                    line += "█"
                elif top:
                    line += "▀"
                elif bot:
                    line += "▄"
                else:
                    line += " "
            print(line, file=sys.stderr)
        print("", file=sys.stderr)
    except Exception:
        pass  # silently skip if both methods fail


# Minimal 1×1 grey PNG served as last-resort fallback when PIL is unavailable
_FALLBACK_ERROR_PNG: bytes = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAAC0lEQVQI12NgAAIABQAABjE+ibYAAAAASUVORK5CYII="
)


def _make_error_png(msg: str = "QR Unavailable") -> bytes:
    """Return a 300×300 red PNG displaying *msg*.

    Used by the QR HTTP server when _qr_image_data is empty (e.g. the image
    was never downloaded, the server is called after _stop_qr_server cleared
    it, or the QR download itself failed).  Falls back to a hardcoded 1×1
    pixel if PIL is unavailable.
    """
    try:
        from PIL import Image as _I, ImageDraw as _D
        img  = _I.new("RGB", (300, 300), (180, 30, 30))
        draw = _D.Draw(img)
        draw.rectangle((15, 100, 285, 200), fill=(255, 255, 255))
        for i, word in enumerate(msg.replace("/", " / ").split()):
            draw.text((20, 108 + i * 22), word, fill=(180, 30, 30))
        buf = BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        return _FALLBACK_ERROR_PNG


class _QrHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler that serves the QR PNG at a secret one-time path."""

    def do_GET(self) -> None:  # noqa: N802
        # Only serve the image at the secret path; reject everything else.
        expected = f"/qr/{_QR_PATH_TOKEN}"
        if self.path.split("?")[0] != expected:
            self.send_response(404)
            self.end_headers()
            return
        if _qr_image_data:
            data, status = _qr_image_data, 200
        else:
            # Image data absent: either not yet downloaded, already cleared
            # after auth completed/timed-out, or the download failed.
            data, status = _make_error_png("QR Not Ready / Expired"), 503
        self.send_response(status)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args) -> None:  # suppress access log
        pass


def _start_qr_server() -> HTTPServer:
    # HTTPServer.__init__ calls server_bind() + server_activate() (socket.listen())
    # so the kernel accepts and queues connections immediately after construction,
    # before serve_forever() enters its select loop.  No Event needed.
    httpd = HTTPServer(("0.0.0.0", ARGS.port), _QrHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


def _stop_qr_server() -> None:
    """Shut down the QR HTTP server and clear the credential-bearing image
    from memory.  Called as soon as authentication succeeds, times out, or
    fails so the QR can no longer be fetched.
    """
    global _qr_image_data, _qr_httpd
    if _qr_httpd is not None:
        _qr_httpd.shutdown()
        _qr_httpd = None
    _qr_image_data = b""  # clear from heap


# ── Xiaomi Cloud connector ─────────────────────────────────────────────────────

class XiaomiCloudConnector:
    """Crypto + API layer, shared between all login methods."""

    def __init__(self) -> None:
        self._agent      = self._generate_agent()
        self._device_id  = self._generate_device_id()
        self._session    = requests.Session()
        self._ssecurity: str | None  = None
        self.userId:     str | None  = None
        self._serviceToken: str | None = None

    # ── API calls ─────────────────────────────────────────────────────────────

    def get_homes(self, country: str) -> dict | None:
        url = self._api_url(country) + "/v2/homeroom/gethome"
        params = {"data": '{"fg":true,"fetch_share":true,"fetch_share_dev":true,"limit":300,"app_ver":7}'}
        return self._api_call(url, params)

    def get_devices(self, country: str, home_id, owner_id) -> dict | None:
        url = self._api_url(country) + "/v2/home/home_device_list"
        params = {
            "data": (
                f'{{"home_owner":{owner_id},"home_id":{home_id},'
                '"limit":200,"get_split_device":true,"support_smart_home":true}'
            )
        }
        return self._api_call(url, params)

    def get_dev_cnt(self, country: str) -> dict | None:
        url = self._api_url(country) + "/v2/user/get_device_cnt"
        params = {"data": '{"fetch_own":true,"fetch_share":true}'}
        return self._api_call(url, params)

    def get_beaconkey(self, country: str, did: str) -> dict | None:
        url = self._api_url(country) + "/v2/device/blt_get_beaconkey"
        params = {"data": f'{{"did":"{did}","pdid":1}}'}
        return self._api_call(url, params)

    def _api_call(self, url: str, params: dict) -> dict | None:
        headers = {
            "Accept-Encoding": "identity",
            "User-Agent": self._agent,
            "Content-Type": "application/x-www-form-urlencoded",
            "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2",
            "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4",
        }
        cookies = {
            "userId": str(self.userId),
            "yetAnotherServiceToken": str(self._serviceToken),
            "serviceToken": str(self._serviceToken),
            "locale": "en_GB",
            "timezone": "GMT+02:00",
            "is_daylight": "1",
            "dst_offset": "3600000",
            "channel": "MI_APP_STORE",
        }
        millis      = round(time.time() * 1000)
        nonce       = self._nonce(millis)
        signed      = self._signed_nonce(nonce)
        fields      = self._enc_params(url, "POST", signed, nonce, params, self._ssecurity)
        response    = self._session.post(url, headers=headers, cookies=cookies, params=fields)
        if response.status_code == 200:
            decoded = self._decrypt_rc4(self._signed_nonce(fields["_nonce"]), response.text)
            return json.loads(decoded)
        return None

    # ── Crypto helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _api_url(country: str) -> str:
        prefix = "" if country == "cn" else (country + ".")
        return f"https://{prefix}api.io.mi.com/app"

    def _signed_nonce(self, nonce: str) -> str:
        h = hashlib.sha256(base64.b64decode(self._ssecurity) + base64.b64decode(nonce))
        return base64.b64encode(h.digest()).decode()

    @staticmethod
    def _nonce(millis: int) -> str:
        return base64.b64encode(os.urandom(8) + (millis // 60000).to_bytes(4, "big")).decode()

    @staticmethod
    def _enc_signature(url: str, method: str, signed_nonce: str, params: dict) -> str:
        parts = [method.upper(), url.split("com")[1].replace("/app/", "/")]
        parts += [f"{k}={v}" for k, v in params.items()]
        parts.append(signed_nonce)
        return base64.b64encode(hashlib.sha1("&".join(parts).encode()).digest()).decode()

    @classmethod
    def _enc_params(cls, url, method, signed_nonce, nonce, params, ssecurity) -> dict:
        params["rc4_hash__"] = cls._enc_signature(url, method, signed_nonce, params)
        for k, v in params.items():
            params[k] = cls._encrypt_rc4(signed_nonce, v)
        params.update({
            "signature": cls._enc_signature(url, method, signed_nonce, params),
            "ssecurity": ssecurity,
            "_nonce": nonce,
        })
        return params

    @staticmethod
    def _encrypt_rc4(password: str, payload: str) -> str:
        r = ARC4.new(base64.b64decode(password))
        r.encrypt(bytes(1024))
        return base64.b64encode(r.encrypt(payload.encode())).decode()

    @staticmethod
    def _decrypt_rc4(password: str, payload: str) -> bytes:
        r = ARC4.new(base64.b64decode(password))
        r.encrypt(bytes(1024))
        return r.encrypt(base64.b64decode(payload))

    @staticmethod
    def _to_json(text: str) -> dict:
        return json.loads(text.replace("&&&START&&&", ""))

    @staticmethod
    def _generate_agent() -> str:
        aid  = "".join(chr(random.randint(65, 69)) for _ in range(13))
        rand = "".join(chr(random.randint(97, 122)) for _ in range(18))
        return f"{rand}-{aid} APP/com.xiaomi.mihome APPV/10.5.201"

    @staticmethod
    def _generate_device_id() -> str:
        return "".join(chr(random.randint(97, 122)) for _ in range(6))


# ── QR login connector ─────────────────────────────────────────────────────────

class QrLoginConnector(XiaomiCloudConnector):
    """QR-code based login — no password input required."""

    def __init__(self) -> None:
        super().__init__()
        self._cUserId:     str | None = None
        self._pass_token:  str | None = None
        self._location:    str | None = None
        self._qr_image_url: str | None = None
        self._login_url:   str | None = None
        self._long_poll_url: str | None = None
        self._timeout:     int = ARGS.timeout

    def login(self) -> bool:
        """Attempt QR login, auto-retrying up to ARGS.retries times on expiry.

        QR sessions on Xiaomi's server are short-lived (typically 60–120 s).
        In an AI-agent context the total latency from QR generation to the
        user actually scanning (AI inference + user reaction) can easily
        exceed that window.  Each timeout transparently regenerates a fresh
        QR so the user gets another chance without having to restart.
        """
        max_attempts = 1 + ARGS.retries
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                _emit(f"QR_RETRY attempt={attempt} of={max_attempts}")

            if not self._step1_get_urls():
                _emit("STATUS=login_failed reason=cannot_get_qr_url")
                return False
            if not self._step2_serve_qr():
                _emit("STATUS=login_failed reason=cannot_download_qr_image")
                return False

            # _step3/_step4 always followed by QR server teardown.
            try:
                poll_result = self._step3_long_poll()
                if poll_result is None:
                    # QR expired — loop will regenerate a fresh one.
                    continue
                if not poll_result:
                    return False  # hard error; STATUS already emitted
                if not self._step4_service_token():
                    _emit("STATUS=login_failed reason=cannot_get_service_token")
                    return False
            finally:
                _stop_qr_server()   # clears image & stops HTTP server

            _emit("STATUS=login_success")
            return True

        # All retries exhausted without a successful scan.
        _emit("STATUS=login_timeout")
        return False

    def _step1_get_urls(self) -> bool:
        """Get QR image URL, login URL, and long-polling URL from Xiaomi."""
        url  = "https://account.xiaomi.com/longPolling/loginUrl"
        data = {
            "_qrsize": "480",
            "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
            "callback": "https://sts.api.io.mi.com/sts",
            "_hasLogo": "false",
            "sid": "xiaomiio",
            "serviceParam": "",
            "_locale": "en_GB",
            "_dc": str(int(time.time() * 1000)),
        }
        try:
            resp = self._session.get(url, params=data, timeout=15)
        except Exception as exc:
            _emit(f"STATUS=login_failed reason=network_error detail={exc}")
            return False
        if resp.status_code != 200:
            return False
        rd = self._to_json(resp.text)
        if "qr" not in rd:
            return False
        self._qr_image_url  = rd["qr"]
        self._login_url     = rd["loginUrl"]
        self._long_poll_url = rd["lp"]
        # Validate that server-provided URLs point to trusted Xiaomi domains
        # (guards against SSRF if the response is tampered in transit).
        try:
            _assert_mi_url(self._qr_image_url,  "qr")
            _assert_mi_url(self._long_poll_url,  "lp")
        except ValueError as exc:
            _emit(f"STATUS=login_failed reason=untrusted_url detail={exc}")
            return False
        # Cap the server-provided timeout at the user-supplied --timeout so a
        # rogue/misbehaving server cannot extend the QR window indefinitely.
        self._timeout = min(rd.get("timeout", ARGS.timeout), ARGS.timeout)
        return True

    def _step2_serve_qr(self) -> bool:
        """Download QR image, start HTTP server, emit inline image + URLs."""
        global _qr_image_data, _qr_httpd
        try:
            resp = self._session.get(self._qr_image_url, timeout=15)
        except Exception:
            return False
        if resp.status_code != 200 or not resp.content:
            return False
        _qr_image_data = resp.content
        local_ip = _get_local_ip()
        try:
            _qr_httpd = _start_qr_server()
        except OSError as exc:
            # Most common causes: port already bound by another process,
            # or port < 1024 without root privileges.
            _emit(f"STATUS=login_failed reason=qr_server_start_failed detail={exc}")
            _emit(f"  hint: try a different port with --port, e.g. --port 31416")
            _qr_image_data = b""   # nothing to serve; clear it
            return False
        _emit(f"QR_SERVER=http://{local_ip}:{ARGS.port}/qr/{_QR_PATH_TOKEN}")
        # QR_IMAGE_URL is the same URL written out explicitly — agents must
        # use this FULL value verbatim (including the /qr/<hex-token> path).
        _emit(f"QR_IMAGE_URL=http://{local_ip}:{ARGS.port}/qr/{_QR_PATH_TOKEN}")
        # Try to decode the URL that is actually encoded inside the QR PNG.
        # This URL is what Mi Home reads when scanning and does NOT require
        # existing browser cookies — unlike loginUrl (the browser fallback).
        qr_url = _decode_qr_url(_qr_image_data) or self._login_url
        if qr_url != self._login_url:
            _emit(f"QR_URL={qr_url}")          # decoded from PNG (preferred)
            _emit(f"QR_LOGIN_URL={self._login_url}")  # browser fallback
        else:
            _emit(f"QR_URL={qr_url}")
        # Inline base64 PNG — agents / UIs can render this directly without
        # needing to reach the HTTP server.
        _emit(f"QR_IMAGE_B64={base64.b64encode(_qr_image_data).decode()}")
        # Print block-art to stderr so the QR is visible in a human terminal.
        _print_qr_art(qr_url)
        _emit("STATUS=waiting_for_scan")
        return True

    def _step3_long_poll(self) -> bool | None:
        """Long-poll until user scans or timeout.

        Returns:
            True  — user scanned; session data populated.
            None  — QR expired before scan (retriable: caller can regenerate).
            False — hard network/parse error (not retriable).
        """
        url   = self._long_poll_url
        start = time.time()
        while True:
            try:
                resp = self._session.get(url, timeout=15)
            except requests.exceptions.Timeout:
                if time.time() - start > self._timeout:
                    return None   # timeout — caller decides whether to retry
                continue
            except Exception as exc:
                _emit(f"STATUS=login_failed reason=poll_error detail={exc}")
                return False

            if resp.status_code == 200:
                break
            if time.time() - start > self._timeout:
                return None   # timeout — caller decides whether to retry
            # back off briefly before retry
            time.sleep(2)

        rd = self._to_json(resp.text)
        self.userId          = rd.get("userId")
        self._ssecurity      = rd.get("ssecurity")
        self._cUserId        = rd.get("cUserId")
        self._pass_token     = rd.get("passToken")
        self._location       = rd.get("location")
        return bool(self._ssecurity)

    def _step4_service_token(self) -> bool:
        """Exchange location URL for service token."""
        if not self._location:
            return False
        try:
            resp = self._session.get(
                self._location,
                headers={"content-type": "application/x-www-form-urlencoded"},
                timeout=15,
            )
        except Exception:
            return False
        if resp.status_code != 200:
            return False
        self._serviceToken = resp.cookies.get("serviceToken")
        # Mirror to other Mi API domains
        for domain in [".api.io.mi.com", ".io.mi.com", ".mi.com"]:
            self._session.cookies.set("serviceToken", self._serviceToken, domain=domain)
            self._session.cookies.set("yetAnotherServiceToken", self._serviceToken, domain=domain)
        return bool(self._serviceToken)


# ── Device collection ──────────────────────────────────────────────────────────

def collect_devices(connector: XiaomiCloudConnector, servers_to_check: list[str]) -> list[dict]:
    """Fetch all devices from all homes across the given servers."""
    all_devices: list[dict] = []

    for server in servers_to_check:
        homes: list[dict] = []

        # Own homes
        homes_resp = connector.get_homes(server)
        if homes_resp and "result" in homes_resp:
            for h in homes_resp["result"].get("homelist", []):
                homes.append({"home_id": h["id"], "home_owner": connector.userId})

        # Shared homes
        cnt_resp = connector.get_dev_cnt(server)
        if cnt_resp and "result" in cnt_resp:
            for h in cnt_resp["result"].get("share", {}).get("share_family", []):
                homes.append({"home_id": h["home_id"], "home_owner": h["home_owner"]})

        for home in homes:
            dev_resp = connector.get_devices(server, home["home_id"], home["home_owner"])
            if not dev_resp or "result" not in dev_resp:
                continue
            device_info = dev_resp["result"].get("device_info") or []
            for device in device_info:
                ble_key = None
                did = device.get("did", "")
                if "blt" in did:
                    bk_resp = connector.get_beaconkey(server, did)
                    if bk_resp and "result" in bk_resp:
                        ble_key = bk_resp["result"].get("beaconkey")

                entry = {
                    "server":   server,
                    "home_id":  str(home["home_id"]),
                    "name":     device.get("name", ""),
                    "did":      did,
                    "ip":       device.get("localip", ""),
                    "token":    device.get("token", ""),
                    "mac":      device.get("mac", ""),
                    "model":    device.get("model", ""),
                    "ble_key":  ble_key,
                }
                all_devices.append(entry)

    return all_devices


# ── Output helpers ─────────────────────────────────────────────────────────────

def _apply_filter(devices: list[dict]) -> list[dict]:
    if not ARGS.filter:
        return devices
    q = ARGS.filter.lower()
    return [d for d in devices if q in d["name"].lower()]


def _save_json(devices: list[dict]) -> Path:
    path = OUTPUT_DIR / "devices.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(devices, f, ensure_ascii=False, indent=2)
    return path


def _save_markdown(devices: list[dict]) -> Path:
    path = OUTPUT_DIR / "devices.md"
    lines = [
        "# Xiaomi Devices — Token Registry",
        "",
        "> Auto-generated by `xiaomi-token-extractor`. Do **not** commit tokens to public repos.",
        "",
        f"Last updated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]

    # Group by server
    by_server: dict[str, list[dict]] = {}
    for d in devices:
        by_server.setdefault(d["server"], []).append(d)

    for server, devs in by_server.items():
        lines += [
            f"## Server: {server}",
            "",
            "| Device Name | IP | Token | Model | BLE Key |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ]
        for d in devs:
            ble = d["ble_key"] or "-"
            lines.append(
                f"| {d['name']} | {d['ip'] or '-'} | {d['token'] or '-'} | {d['model'] or '-'} | {ble} |"
            )
        lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    servers = [ARGS.server] if ARGS.server else SERVERS

    connector = QrLoginConnector()
    if not connector.login():
        sys.exit(1)

    all_devices   = collect_devices(connector, servers)
    shown_devices = _apply_filter(all_devices)

    for d in shown_devices:
        _emit(f"DEVICE={json.dumps(d, ensure_ascii=False)}")

    json_path = _save_json(all_devices)   # always save full list
    md_path   = _save_markdown(all_devices)
    _emit(f"DEVICES_SAVED={json_path}")
    _emit(f"DONE count={len(shown_devices)} json={json_path} md={md_path}")


if __name__ == "__main__":
    main()
