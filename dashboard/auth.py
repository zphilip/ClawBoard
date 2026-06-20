"""Authentication helpers for ClawBoard dashboard."""
import json
import os
import hashlib
import hmac
import secrets

from nicegui import app, ui

from .paths import SCRIPT_DIR

AUTH_FILE      = os.path.join(SCRIPT_DIR, 'config', 'auth.json')
_invite_tokens = {}  # one-time tokens → expiry_unix


def _load_auth():
    try:
        with open(AUTH_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def _save_auth(data):
    os.makedirs(os.path.dirname(AUTH_FILE), exist_ok=True)
    with open(AUTH_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def _hash_pw(pw):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 200_000)
    return f'{salt}:{h.hex()}'


def _verify_pw(pw, stored):
    try:
        salt, h = stored.split(':', 1)
        h2 = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 200_000)
        return hmac.compare_digest(h, h2.hex())
    except Exception:
        return False


def _is_authed():
    """True if this browser session is authenticated or has a valid paired-device token."""
    try:
        if app.storage.user.get('auth'):
            return True
    except Exception:
        pass
    tok = app.storage.browser.get('device_token', '')
    if not tok:
        return False
    d = _load_auth()
    if d and any(dv['token'] == tok for dv in d.get('paired_devices', [])):
        try:
            app.storage.user['auth'] = True
        except Exception:
            pass
        return True
    try:
        del app.storage.browser['device_token']
    except Exception:
        pass
    return False


def _logout():
    tok = app.storage.browser.get('device_token', '')
    if tok:
        d = _load_auth()
        if d:
            d['paired_devices'] = [dv for dv in d.get('paired_devices', []) if dv['token'] != tok]
            _save_auth(d)
        try:
            del app.storage.browser['device_token']
        except Exception:
            pass
    try:
        app.storage.user['auth'] = False
    except Exception:
        pass
    ui.navigate.to('/login')
