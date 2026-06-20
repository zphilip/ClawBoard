from nicegui import ui, app
from fastapi import Request
import tomlkit, os, sys, subprocess, hashlib, hmac, secrets, json, re, time as _time
from typing import Any
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import quote
import locales.zh as zh_strings
import locales.en as en_strings

SCRIPT_DIR        = os.path.dirname(os.path.abspath(__file__))
PATHS             = [os.path.join(SCRIPT_DIR, 'config/config.toml'), 'config.toml']
CONFIG_PATH       = next((p for p in PATHS if os.path.exists(p)), PATHS[0])
DEPLOY_CONFIG_PATH = '/var/lib/zeroclaw/.zeroclaw/config.toml'  # real zeroclaw config
PICOCLAW_CONFIG_PATH         = os.path.join(SCRIPT_DIR, 'config', 'config.json')      # picoclaw JSON config (local workspace copy)
PICOCLAW_PID_FILE            = '/var/lib/picoclaw/.picoclaw/.picoclaw.pid'             # runtime PID+token file
PICOCLAW_SECURITY_YML        = '/var/lib/picoclaw/.picoclaw/.security.yml'            # live security.yml (picoclaw runtime)
PICOCLAW_SECURITY_YML_LOCAL  = os.path.join(SCRIPT_DIR, 'config', 'security.yml')    # local workspace copy
PICOCLAW_DEPLOY_CONFIG_PATH  = '/var/lib/picoclaw/.picoclaw/config.json'              # picoclaw live config path
PICOCLAW_DEPLOY_SECURITY_PATH= '/var/lib/picoclaw/.picoclaw/.security.yml'            # same as PICOCLAW_SECURITY_YML

OPENCLAW_CONFIG_PATH         = os.path.join(SCRIPT_DIR, 'config', 'openclaw.json')
OPENCLAW_DEPLOY_CONFIG_PATH  = '/var/lib/openclaw/.openclaw/openclaw.json'
CLAWPROXY_CONFIG_PATH        = os.path.join(SCRIPT_DIR, 'clawproxy', 'config.toml')
CLAWPROXY_CONFIG_EXAMPLE     = os.path.join(SCRIPT_DIR, 'clawproxy', 'config.toml.example')
CLAWPROXY_DEPLOY_CONFIG_PATH = '/opt/clawproxy/config.toml'
CHARACTERS_DIR               = os.path.join(SCRIPT_DIR, 'characters')          # character personas folder

def _sudo_read_file(path: str) -> tuple[str, str]:
    """Read a file that requires elevated privileges via `sudo cat`.
    Returns (content, error_message).  error_message is '' on success.
    Requires a sudoers rule:
      zeroclaw ALL=(root) NOPASSWD: /usr/bin/cat /var/lib/picoclaw/.picoclaw/.picoclaw.pid
      zeroclaw ALL=(root) NOPASSWD: /usr/bin/cat /var/lib/picoclaw/.picoclaw/.security.yml
    """
    r = subprocess.run(
        ['sudo', '/usr/bin/cat', path],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        err = r.stderr.strip() or f'sudo cat {path} failed (exit {r.returncode})'
        return '', err
    return r.stdout, ''

def _get_lan_ip() -> str:
    """Return the device's primary LAN IPv4 address for use in QR pairing URLs.
    Prefers the IP of the active WiFi interface (via nmcli), then falls back to
    the default-route interface, then socket-based detection.
    Returns an empty string if nothing can be determined."""
    # 1. nmcli: find connected wifi device, then its IP
    try:
        nm = subprocess.run(
            ['nmcli', '-t', '-f', 'DEVICE,TYPE,STATE', 'dev'],
            capture_output=True, text=True
        )
        for row in nm.stdout.splitlines():
            parts = row.split(':')
            if len(parts) >= 3 and parts[1] == 'wifi' and parts[2] == 'connected':
                iface = parts[0]
                ip_r = subprocess.run(
                    ['ip', '-4', 'addr', 'show', iface],
                    capture_output=True, text=True
                )
                for ln in ip_r.stdout.splitlines():
                    ln = ln.strip()
                    if ln.startswith('inet '):
                        return ln.split()[1].split('/')[0]
    except Exception:
        pass
    # 2. default-route interface
    try:
        gw_r = subprocess.run(['ip', 'route', 'show', 'default'],
                              capture_output=True, text=True)
        for ln in gw_r.stdout.strip().splitlines():
            parts = ln.split()
            # "default via <gw> dev <iface> ..."
            if 'dev' in parts:
                iface = parts[parts.index('dev') + 1]
                ip_r = subprocess.run(
                    ['ip', '-4', 'addr', 'show', iface],
                    capture_output=True, text=True
                )
                for aln in ip_r.stdout.splitlines():
                    aln = aln.strip()
                    if aln.startswith('inet '):
                        return aln.split()[1].split('/')[0]
    except Exception:
        pass
    # 3. socket trick
    try:
        import socket as _sock
        s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ''

def _read_picoclaw_pid_token() -> tuple[str, str]:
    """Read the runtime token from the picoclaw PID file using sudo.
    Returns (pid_token, error_message)."""
    raw, err = _sudo_read_file(PICOCLAW_PID_FILE)
    if err:
        return '', err
    try:
        data = json.loads(raw)
        tok = str(data.get('token', '')).strip()
        if not tok:
            return '', f'token field empty in {PICOCLAW_PID_FILE}'
        return tok, ''
    except Exception as _e:
        return '', f'PID file parse error: {_e}'

def _read_security_yml_token(section: str = 'pico_client') -> tuple[str, str]:
    """Read channel_list.<section>.settings.token from .security.yml via sudo.

    Returns (token, error_message). error_message is '' on success.
    """
    raw, err = _sudo_read_file(PICOCLAW_SECURITY_YML)
    if err:
        return '', err
    try:
        import yaml as _yaml
        data = _yaml.safe_load(raw)
        root = data or {}
        # Canonical picoclaw path: channel_list.<section>.settings.token
        tok = (((root.get('channel_list') or {}).get(section) or {}).get('settings') or {}).get('token', '')
        if tok:
            return str(tok).strip(), ''
        # Backward compatibility for older layouts
        tok = (((root.get('channels') or {}).get(section) or {}).get('settings') or {}).get('token', '')
        if tok:
            return str(tok).strip(), ''
        tok = ((root.get('channels') or {}).get(section) or {}).get('token', '')
        if tok:
            return str(tok).strip(), ''
    except ImportError:
        pass
    # Fallback: simple line-by-line parser for known structures.
    in_channel_list = False
    in_section  = False
    in_settings = False
    indent_ch   = None   # indent of channel keys (e.g. 2)
    indent_sec  = None   # indent of section sub-keys
    indent_set  = None   # indent of settings sub-keys
    for line in raw.splitlines():
        stripped = line.lstrip()
        indent   = len(line) - len(stripped)
        if stripped.startswith('channel_list:') or stripped.startswith('channels:'):
            in_channel_list = True
            indent_ch   = None
            in_section = False
            in_settings = False
            continue
        if not in_channel_list:
            continue
        if indent_ch is None and stripped and not stripped.startswith('#'):
            indent_ch = indent
        if indent_ch is not None and indent == indent_ch:
            in_section = stripped.startswith(f'{section}:')
            indent_sec = None
            in_settings = False
            indent_set = None
            continue
        if in_section:
            if indent_sec is None and stripped and not stripped.startswith('#'):
                indent_sec = indent
            if indent_sec is not None and indent == indent_sec:
                if stripped.startswith('settings:'):
                    in_settings = True
                    indent_set = None
                    continue
                if stripped.startswith('token:'):
                    tok = stripped[len('token:'):].strip().strip('"\'')
                    return tok, ''
            if in_settings:
                if indent_set is None and stripped and not stripped.startswith('#'):
                    indent_set = indent
                if indent_set is not None and indent == indent_set and stripped.startswith('token:'):
                    tok = stripped[len('token:'):].strip().strip('"\'')
                    return tok, ''
    return '', f'channel_list.{section}.settings.token not found in {PICOCLAW_SECURITY_YML}'

def load_picoclaw_config():
    """Load picoclaw config.json; return empty dict on failure."""
    try:
        with open(PICOCLAW_CONFIG_PATH, 'r') as f:
            return json.load(f)
    except Exception:
        return {}

def _parse_json5(text: str):
    """Parse a JSON5-ish string: strips // and /* */ comments outside strings,
    removes trailing commas, then delegates to the standard json parser."""
    import re as _re
    # State-machine pass: walk character by character to strip comments that
    # are NOT inside a string literal.  Handles \" escapes inside strings.
    out = []
    i = 0
    n = len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == '\\' and i + 1 < n:
                i += 1
                out.append(text[i])
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
                out.append(c)
            elif c == '/' and i + 1 < n and text[i + 1] == '/':
                # Line comment — skip to end of line
                while i < n and text[i] != '\n':
                    i += 1
                continue
            elif c == '/' and i + 1 < n and text[i + 1] == '*':
                # Block comment — skip to */
                i += 2
                while i < n - 1 and not (text[i] == '*' and text[i + 1] == '/'):
                    i += 1
                i += 2  # skip closing */
                continue
            else:
                out.append(c)
        i += 1
    stripped = ''.join(out)
    # Remove trailing commas before ] or }
    stripped = _re.sub(r',\s*([}\]])', r'\1', stripped)
    try:
        return json.loads(stripped)
    except Exception:
        try:
            import json5 as _json5
            return _json5.loads(text)
        except ImportError:
            raise

def load_openclaw_config():
    """Load openclaw config (JSON5-tolerant); return empty dict on failure."""
    try:
        with open(OPENCLAW_CONFIG_PATH, 'r') as f:
            return _parse_json5(f.read())
    except Exception:
        return {}

def save_openclaw_config(data):
    os.makedirs(os.path.dirname(OPENCLAW_CONFIG_PATH), exist_ok=True)
    with open(OPENCLAW_CONFIG_PATH, 'w') as f:
        json.dump(data, f, indent=2)

def deploy_openclaw_config():
    """Deploy openclaw.json from local workspace to OPENCLAW_DEPLOY_CONFIG_PATH via sudo tee.
    Returns (ok: bool, message: str)."""
    import shutil
    bak = OPENCLAW_CONFIG_PATH + '.bak'
    try:
        shutil.copy2(OPENCLAW_CONFIG_PATH, bak)
    except Exception as e:
        return False, f'Backup failed: {e}'
    try:
        with open(OPENCLAW_CONFIG_PATH, 'r') as f:
            content = f.read()
    except Exception as e:
        return False, f'Read failed: {e}'
    subprocess.run(
        ['sudo', '/usr/bin/mkdir', '-p', os.path.dirname(OPENCLAW_DEPLOY_CONFIG_PATH)],
        capture_output=True
    )
    r = subprocess.run(
        ['sudo', '/usr/bin/tee', OPENCLAW_DEPLOY_CONFIG_PATH],
        input=content, capture_output=True, text=True
    )
    if r.returncode != 0:
        err = r.stderr.strip() or f'sudo tee failed (exit {r.returncode})'
        return False, err
    subprocess.run(
        ['sudo', '/usr/bin/chown', 'openclaw:openclaw', OPENCLAW_DEPLOY_CONFIG_PATH],
        capture_output=True
    )
    return True, ''

def restart_openclaw_service():
    """Restart openclaw.service via sudo systemctl.
    Returns (ok: bool, stderr: str)."""
    r = subprocess.run(
        ['sudo', '/usr/bin/systemctl', 'restart', 'openclaw.service'],
        capture_output=True, text=True
    )
    return r.returncode == 0, r.stderr.strip()

def load_clawproxy_config():
    """Load clawproxy config.toml (TOML format); returns tomlkit document or empty on failure."""
    for path in (CLAWPROXY_CONFIG_PATH, CLAWPROXY_CONFIG_EXAMPLE):
        try:
            with open(path, 'r') as f:
                return tomlkit.load(f)
        except Exception:
            pass
    return tomlkit.document()

def save_clawproxy_config(conf):
    """Save clawproxy config.toml to local workspace copy."""
    os.makedirs(os.path.dirname(CLAWPROXY_CONFIG_PATH), exist_ok=True)
    with open(CLAWPROXY_CONFIG_PATH, 'w') as f:
        f.write(tomlkit.dumps(conf))

def deploy_clawproxy_config():
    """Deploy clawproxy config.toml from local workspace to /opt/clawproxy/config.toml via sudo tee.
    Returns (ok: bool, message: str)."""
    try:
        with open(CLAWPROXY_CONFIG_PATH, 'r') as f:
            content = f.read()
    except Exception as e:
        return False, f'Read failed: {e}'
    subprocess.run(
        ['sudo', '/usr/bin/mkdir', '-p', os.path.dirname(CLAWPROXY_DEPLOY_CONFIG_PATH)],
        capture_output=True
    )
    r = subprocess.run(
        ['sudo', '/usr/bin/tee', CLAWPROXY_DEPLOY_CONFIG_PATH],
        input=content, capture_output=True, text=True
    )
    if r.returncode != 0:
        err = r.stderr.strip() or f'sudo tee failed (exit {r.returncode})'
        return False, err
    subprocess.run(
        ['sudo', '/usr/bin/chown', 'zero:zero', CLAWPROXY_DEPLOY_CONFIG_PATH],
        capture_output=True
    )
    return True, ''

# Machine target for openclaw user systemd session (works even without XDG_RUNTIME_DIR)
_OC_MACHINE = 'openclaw@.host'

def openclaw_service_status() -> str:
    """Return is-active state of openclaw.service via the openclaw user's systemd session."""
    r = subprocess.run(
        ['sudo', '/usr/bin/systemctl', f'--machine={_OC_MACHINE}', '--user',
         'is-active', 'openclaw.service'],
        capture_output=True, text=True
    )
    return r.stdout.strip()

def openclaw_service_is_enabled() -> bool:
    """Return True if openclaw.service is enabled in the openclaw user's systemd session."""
    r = subprocess.run(
        ['sudo', '/usr/bin/systemctl', f'--machine={_OC_MACHINE}', '--user',
         'is-enabled', 'openclaw.service'],
        capture_output=True, text=True
    )
    return r.stdout.strip() in ('enabled', 'static', 'indirect')

def enable_openclaw_user_service() -> tuple[bool, str]:
    """Enable and start openclaw.service in the openclaw user's systemd session.
    Returns (ok, error_message)."""
    r = subprocess.run(
        ['sudo', '/usr/bin/systemctl', f'--machine={_OC_MACHINE}', '--user',
         'enable', '--now', 'openclaw.service'],
        capture_output=True, text=True
    )
    if r.returncode == 0:
        return True, ''
    return False, r.stderr.strip() or f'exit {r.returncode}'

def _read_openclaw_deploy_token() -> tuple[str, str]:
    """Read gateway.auth.token from the deployed openclaw.json via sudo cat.
    Falls back to local config/openclaw.json on failure.
    Returns (token, error_message)."""
    raw, err = _sudo_read_file(OPENCLAW_DEPLOY_CONFIG_PATH)
    if not err:
        try:
            data = _parse_json5(raw)
            tok = str(data.get('gateway', {}).get('auth', {}).get('token', '')).strip()
            if tok:
                return tok, ''
        except Exception as e:
            err = f'Parse error: {e}'
    # Fallback to local copy
    local_conf = load_openclaw_config()
    tok = str(local_conf.get('gateway', {}).get('auth', {}).get('token', '')).strip()
    if tok:
        return tok, f'(local copy — deploy read failed: {err})'
    return '', err

def save_picoclaw_config(data):
    os.makedirs(os.path.dirname(PICOCLAW_CONFIG_PATH), exist_ok=True)
    with open(PICOCLAW_CONFIG_PATH, 'w') as f:
        json.dump(data, f, indent=2)

def load_picoclaw_security():
    """Load picoclaw security.yml from local workspace copy; return {} on failure."""
    try:
        import yaml
        with open(PICOCLAW_SECURITY_YML_LOCAL, 'r') as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}

def save_picoclaw_security(sec):
    """Save picoclaw security.yml to local workspace copy (chmod 600)."""
    import yaml
    os.makedirs(os.path.dirname(PICOCLAW_SECURITY_YML_LOCAL), exist_ok=True)
    with open(PICOCLAW_SECURITY_YML_LOCAL, 'w') as f:
        yaml.dump(sec, f, allow_unicode=True, default_flow_style=False)
    try:
        os.chmod(PICOCLAW_SECURITY_YML_LOCAL, 0o600)
    except Exception:
        pass

def load_provider_hints():
    """Load provider hints from config/provider_hints.json.
    Falls back to the picoclaw model_list if the file is missing.
    Each entry: {model_name, model, provider_id, api_base, auth_method?}
    """
    hints_path = os.path.join(SCRIPT_DIR, 'config', 'provider_hints.json')
    try:
        with open(hints_path, 'r') as f:
            hints = json.load(f)
            if isinstance(hints, list):
                return hints
    except Exception:
        pass
    # Fallback: derive from picoclaw model_list
    return [
        {'model_name': e.get('model_name', ''), 'model': e.get('model', ''),
         'api_base': e.get('api_base', ''), 'auth_method': e.get('auth_method', 'apikey'),
         'provider_id': e.get('model', '').split('/')[0]}
        for e in load_picoclaw_config().get('model_list', [])
        if e.get('model_name')
    ]

def load_pc_provider_hints():
    """Load PicoClaw-specific provider hints from config/pc_provider_hints.json.
    Schema: {model_name, model, provider, api_base, auth_method?}
    """
    hints_path = os.path.join(SCRIPT_DIR, 'config', 'pc_provider_hints.json')
    try:
        with open(hints_path, 'r') as f:
            hints = json.load(f)
            if isinstance(hints, list):
                return hints
    except Exception:
        pass
    return []

def load_oc_provider_hints():
    """Load OpenClaw-specific provider hints from config/oc_provider_hints.json.
    Schema: {model_name, model, provider, api_base, primary, api_key_required?}
    """
    hints_path = os.path.join(SCRIPT_DIR, 'config', 'oc_provider_hints.json')
    try:
        with open(hints_path, 'r') as f:
            hints = json.load(f)
            if isinstance(hints, list):
                return hints
    except Exception:
        pass
    return []


def _oc_model_ref_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get('primary', '') or '')
    return str(value or '')


def _oc_provider_models(conf: dict[str, Any]) -> dict[str, Any]:
    # OpenClaw path: models.providers.<name>
    models = conf.get('models', {})
    if isinstance(models, dict):
        providers = models.get('providers', {})
        if isinstance(providers, dict):
            return providers

    # Legacy/borrowed ZeroClaw path: providers.models.<name>
    providers = conf.get('providers', {})
    if isinstance(providers, dict):
        m = providers.get('models', {})
        if isinstance(m, dict):
            return m

    return {}

# ── Auth ─────────────────────────────────────────────────────────────────────
AUTH_FILE      = os.path.join(SCRIPT_DIR, 'config', 'auth.json')
_invite_tokens = {}  # one-time tokens → expiry_unix

def _load_auth():
    try:
        with open(AUTH_FILE) as f: return json.load(f)
    except Exception: return None

def _save_auth(data):
    os.makedirs(os.path.dirname(AUTH_FILE), exist_ok=True)
    with open(AUTH_FILE, 'w') as f: json.dump(data, f, indent=2)

def _hash_pw(pw):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 200_000)
    return f'{salt}:{h.hex()}'

def _verify_pw(pw, stored):
    try:
        salt, h = stored.split(':', 1)
        h2 = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 200_000)
        return hmac.compare_digest(h, h2.hex())
    except Exception: return False

def _is_authed():
    """True if this browser session is authenticated or has a valid paired-device token.
    Uses app.storage.browser (cookie-based, always available) as the primary truth source
    so it works during the initial HTTP request before the WebSocket session is ready.
    """
    # Fast path: WS session already marked (may raise before WS connects – catch it)
    try:
        if app.storage.user.get('auth'):
            return True
    except Exception:
        pass
    # Reliable path: check the browser-persistent device token against auth.json
    tok = app.storage.browser.get('device_token', '')
    if not tok:
        return False
    d = _load_auth()
    if d and any(dv['token'] == tok for dv in d.get('paired_devices', [])):
        try:
            app.storage.user['auth'] = True   # cache for this WS session if available
        except Exception:
            pass
        return True
    # Token in browser but not in auth.json → it was revoked; clear it
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


@ui.page('/login')
def login_page():
    if _is_authed(): ui.navigate.to('/'); return
    if _load_auth() is None: ui.navigate.to('/setup'); return
    with ui.card().classes('absolute-center w-80 shadow-2'):
        ui.label('🔒 ClawBoard').classes('text-h5 text-center w-full q-mb-md')
        w_pw   = ui.input('Password', password=True, password_toggle_button=True).classes('w-full')
        w_rem  = ui.checkbox('Remember this device', value=False)
        w_name = ui.input('Device name (optional)', value='').classes('w-full')
        def do_login():
            d = _load_auth()
            if d and _verify_pw(w_pw.value, d['password_hash']):
                app.storage.user['auth'] = True
                if w_rem.value:
                    tok = secrets.token_urlsafe(32)
                    dname = w_name.value.strip() or f'Device {datetime.now().strftime("%m-%d %H:%M")}'
                    d.setdefault('paired_devices', []).append(
                        {'token': tok, 'name': dname, 'paired_at': int(_time.time())}
                    )
                    _save_auth(d)
                    app.storage.browser['device_token'] = tok
                ui.navigate.to('/')
            else:
                ui.notify('❌ Wrong password', type='negative')
                w_pw.set_value('')
        w_pw.on('keydown.enter', lambda e: do_login())
        ui.button('Login', on_click=do_login).props('elevated').classes('w-full bg-blue-8 text-white q-mt-sm')


@ui.page('/setup')
def setup_page():
    if _load_auth() is not None: ui.navigate.to('/login'); return
    with ui.card().classes('absolute-center w-80 shadow-2'):
        ui.label('⚙️ First-time Setup').classes('text-h5 text-center w-full')
        ui.label('No password set. Create one to secure the dashboard.').classes(
            'text-caption text-grey-6 text-center q-mb-md')
        w_p1 = ui.input('Password',         password=True, password_toggle_button=True).classes('w-full')
        w_p2 = ui.input('Confirm Password', password=True, password_toggle_button=True).classes('w-full')
        def do_setup():
            if len(w_p1.value) < 6:
                ui.notify('Password must be ≥ 6 characters', type='warning'); return
            if w_p1.value != w_p2.value:
                ui.notify('Passwords do not match', type='warning'); return
            _save_auth({'password_hash': _hash_pw(w_p1.value), 'paired_devices': []})
            app.storage.user['auth'] = True
            ui.notify('✅ Password set!', type='positive')
            ui.timer(0.8, lambda: ui.navigate.to('/'), once=True)
        ui.button('Set Password', on_click=do_setup).props('elevated').classes(
            'w-full bg-green-8 text-white q-mt-sm')


@ui.page('/pair')
def pair_page(request: Request):
    tok = request.query_params.get('token', '')
    exp = _invite_tokens.get(tok, 0)
    if not tok or _time.time() > exp:
        with ui.card().classes('absolute-center w-72 shadow-2'):
            ui.label('⚠️ Invite invalid or expired').classes(
                'text-h6 text-negative text-center w-full')
        return
    del _invite_tokens[tok]
    d = _load_auth()
    if not d: ui.navigate.to('/setup'); return
    device_tok = secrets.token_urlsafe(32)
    d.setdefault('paired_devices', []).append(
        {'token': device_tok, 'name': f'Invited {datetime.now().strftime("%m-%d %H:%M")}', 'paired_at': int(_time.time())}
    )
    _save_auth(d)
    app.storage.browser['device_token'] = device_tok
    app.storage.user['auth'] = True
    with ui.card().classes('absolute-center w-72 shadow-2'):
        ui.label('✅ Device Paired!').classes('text-h5 text-center w-full')
        ui.label('Redirecting to dashboard…').classes('text-caption text-center text-grey-6')
    ui.timer(1.5, lambda: ui.navigate.to('/'), once=True)


PROVIDER_IDS = [
    # ── All 75 canonical model_provider slots from for_each_model_provider_slot! ──
    'ai21', 'aihubmix', 'anthropic', 'anyscale', 'arcee', 'astrai',
    'atomic_chat', 'avian', 'azure', 'baichuan', 'baseten', 'bedrock',
    'cerebras', 'cloudflare', 'cohere', 'copilot', 'custom', 'deepinfra',
    'deepmyst', 'deepseek', 'doubao', 'featherless', 'fireworks', 'friendli',
    'gemini', 'gemini_cli', 'github_models', 'glm', 'groq', 'huggingface',
    'hunyuan', 'hyperbolic', 'inception', 'kilo', 'kilocli', 'lambda_ai',
    'lepton', 'litellm', 'llamacpp', 'lmstudio', 'manifest', 'minimax',
    'mistral', 'moonshot', 'morph', 'nearai', 'nebius', 'novita', 'nscale',
    'nvidia', 'ollama', 'openai', 'opencode', 'openrouter', 'osaurus',
    'ovh', 'perplexity', 'qianfan', 'qwen', 'reka', 'sambanova', 'sglang',
    'siliconflow', 'stepfun', 'synthetic', 'telnyx', 'together', 'upstage',
    'venice', 'vercel', 'vllm', 'xai', 'yi', 'zai',
]

CHANNEL_SCHEMAS = {
    'telegram': {'label': 'Telegram', 'fields': [
        ('bot_token',               'Bot Token',                             'password', ''),
        ('allowed_users',           'allowed_users (one per line, * = all)', 'textarea', '*'),
        ('stream_mode',             'stream_mode',                           'select:off,partial', 'off'),
        ('mention_only',            'mention_only',                          'bool', False),
        ('interrupt_on_new_message','interrupt_on_new_message',              'bool', False),
    ]},
    'discord': {'label': 'Discord', 'fields': [
        ('bot_token',     'Bot Token',                      'password', ''),
        ('guild_id',      'guild_id (optional)',            'text',     ''),
        ('allowed_users', 'allowed_users (one per line)',   'textarea', '*'),
        ('listen_to_bots','listen_to_bots',                 'bool',     False),
        ('mention_only',  'mention_only',                   'bool',     False),
    ]},
    'slack': {'label': 'Slack', 'fields': [
        ('bot_token',    'bot_token (xoxb-...)',            'password', ''),
        ('app_token',    'app_token (xapp-...)',            'password', ''),
        ('channel_id',   'channel_id (optional, * = all)', 'text',     ''),
        ('allowed_users','allowed_users (one per line)',    'textarea', '*'),
    ]},
    'mattermost': {'label': 'Mattermost', 'fields': [
        ('url',          'url',                           'text',     'https://mm.example.com'),
        ('bot_token',    'bot_token',                     'password', ''),
        ('channel_id',   'channel_id',                   'text',     ''),
        ('allowed_users','allowed_users (one per line)', 'textarea', '*'),
    ]},
    'matrix': {'label': 'Matrix', 'fields': [
        ('homeserver',   'homeserver',                     'text',     'https://matrix.example.com'),
        ('access_token', 'access_token',                   'password', ''),
        ('user_id',      'user_id (optional, E2EE)',       'text',     ''),
        ('device_id',    'device_id (optional, E2EE)',     'text',     ''),
        ('room_id',      'room_id or alias',               'text',     ''),
        ('allowed_users','allowed_users (one per line)',   'textarea', '*'),
    ]},
    'signal': {'label': 'Signal', 'fields': [
        ('http_url',          'http_url (signal-cli bridge)',   'text',     'http://127.0.0.1:8686'),
        ('account',           'account (+E.164)',               'text',     ''),
        ('group_id',          'group_id (dm / group-id)',       'text',     'dm'),
        ('allowed_from',      'allowed_from (one per line)',    'textarea', '*'),
        ('ignore_attachments','ignore_attachments',             'bool',     False),
        ('ignore_stories',    'ignore_stories',                 'bool',     True),
    ]},
    'whatsapp': {'label': 'WhatsApp', 'fields': [
        ('access_token',    'access_token (Cloud API)',                     'password', ''),
        ('phone_number_id', 'phone_number_id (Cloud API)',                  'text',     ''),
        ('verify_token',    'verify_token (Cloud API)',                     'password', ''),
        ('app_secret',      'app_secret (optional)',                        'password', ''),
        ('session_path',    'session_path (Web mode)',                      'text',     '~/.zeroclaw/state/whatsapp-web/session.db'),
        ('pair_phone',      'pair_phone (Web mode, optional)',              'text',     ''),
        ('pair_code',       'pair_code (Web mode, optional)',               'text',     ''),
        ('allowed_numbers', 'allowed_numbers (one per line, E.164 or *)',   'textarea', '*'),
    ]},
    'dingtalk': {'label': 'DingTalk', 'fields': [
        ('client_id',    'client_id',                      'text',     ''),
        ('client_secret','client_secret',                  'password', ''),
        ('allowed_users','allowed_users (one per line)',   'textarea', '*'),
    ]},
    'qq': {'label': 'QQ', 'fields': [
        ('app_id',       'app_id',                         'text',     ''),
        ('app_secret',   'app_secret',                     'password', ''),
        ('allowed_users','allowed_users (one per line)',   'textarea', '*'),
    ]},
    'lark': {'label': 'Lark', 'fields': [
        ('app_id',             'app_id (cli_xxx)',                'text',     ''),
        ('app_secret',         'app_secret',                      'password', ''),
        ('encrypt_key',        'encrypt_key (optional)',          'password', ''),
        ('verification_token', 'verification_token (optional)',   'text',     ''),
        ('allowed_users',      'allowed_users (one per line)',    'textarea', '*'),
        ('mention_only',       'mention_only',                    'bool',     False),
        ('receive_mode',       'receive_mode',                    'select:websocket,webhook', 'websocket'),
        ('port',               'port (webhook mode)',             'int',      8081),
    ]},
    'feishu': {'label': 'Feishu', 'fields': [
        ('app_id',             'app_id (cli_xxx)',                'text',     ''),
        ('app_secret',         'app_secret',                      'password', ''),
        ('encrypt_key',        'encrypt_key (optional)',          'password', ''),
        ('verification_token', 'verification_token (optional)',   'text',     ''),
        ('allowed_users',      'allowed_users (one per line)',    'textarea', '*'),
        ('receive_mode',       'receive_mode',                    'select:websocket,webhook', 'websocket'),
        ('port',               'port (webhook mode)',             'int',      8081),
    ]},
    'email': {'label': 'Email', 'fields': [
        ('imap_host',          'imap_host',                        'text',     ''),
        ('imap_port',          'imap_port',                        'int',      993),
        ('imap_folder',        'imap_folder',                      'text',     'INBOX'),
        ('smtp_host',          'smtp_host',                        'text',     ''),
        ('smtp_port',          'smtp_port',                        'int',      465),
        ('smtp_tls',           'smtp_tls',                         'bool',     True),
        ('username',           'username',                         'text',     ''),
        ('password',           'password',                         'password', ''),
        ('from_address',       'from_address',                     'text',     ''),
        ('poll_interval_secs', 'poll_interval_secs',               'int',      60),
        ('allowed_senders',    'allowed_senders (one per line)',   'textarea', '*'),
    ]},
    'irc': {'label': 'IRC', 'fields': [
        ('server',            'server',                             'text',     'irc.libera.chat'),
        ('port',              'port',                               'int',      6697),
        ('nickname',          'nickname',                           'text',     'zeroclaw-bot'),
        ('username',          'username (optional)',                'text',     ''),
        ('channels',          'channels (one per line, #chan)',     'textarea', '#zeroclaw'),
        ('allowed_users',     'allowed_users (one per line)',       'textarea', '*'),
        ('server_password',   'server_password (optional)',         'password', ''),
        ('nickserv_password', 'nickserv_password (optional)',       'password', ''),
        ('sasl_password',     'sasl_password (optional)',           'password', ''),
        ('verify_tls',        'verify_tls',                         'bool',     True),
    ]},
    'webhook': {'label': 'Webhook', 'fields': [
        ('port',   'port',               'int',      8080),
        ('secret', 'secret (optional)',  'password', ''),
    ]},
    'nostr': {'label': 'Nostr', 'fields': [
        ('private_key',    'private_key (nsec1... or hex)',                    'password', ''),
        ('relays',         'relays (one per line, wss://...)',                 'textarea', ''),
        ('allowed_pubkeys','allowed_pubkeys (one per line, hex/npub or *)',    'textarea', '*'),
    ]},
    'nextcloud_talk': {'label': 'Nextcloud Talk', 'fields': [
        ('base_url',       'base_url',                       'text',     'https://cloud.example.com'),
        ('app_token',      'app_token',                      'password', ''),
        ('webhook_secret', 'webhook_secret (optional)',      'password', ''),
        ('allowed_users',  'allowed_users (one per line)',   'textarea', '*'),
    ]},
    'linq': {'label': 'Linq', 'fields': [
        ('api_token',       'api_token',                                       'password', ''),
        ('from_phone',      'from_phone (+E.164)',                             'text',     ''),
        ('signing_secret',  'signing_secret (optional)',                       'password', ''),
        ('allowed_senders', 'allowed_senders (one per line, E.164 or *)',      'textarea', '*'),
    ]},
    'imessage': {'label': 'iMessage', 'fields': [
        ('allowed_contacts','allowed_contacts (one per line)', 'textarea', '*'),
    ]},
}

CHANNEL_KEYS   = list(CHANNEL_SCHEMAS.keys())
CHANNEL_LABELS = {k: v['label'] for k, v in CHANNEL_SCHEMAS.items()}

def load_config(source='local'):
    """Load ZeroClaw config.toml.  source='local' prefers config/config.toml
    (the source-of-truth template); source='runtime' prefers the live deploy
    path (/var/lib/zeroclaw/.zeroclaw/config.toml).
    Uses tomlkit so that save_config preserves blank lines, comments and key order.
    Returns (conf, actual_source) — actual_source is the path that was loaded."""
    if source == 'local':
        paths = [CONFIG_PATH, DEPLOY_CONFIG_PATH]
    else:
        paths = [DEPLOY_CONFIG_PATH, CONFIG_PATH]
    for path in paths:
        try:
            with open(path, 'r') as f:
                return tomlkit.load(f), path
        except Exception:
            pass  # direct open failed; try sudo cat for deploy path
        # Runtime config may be owned by zeroclaw:zeroclaw — use sudo cat
        if path == DEPLOY_CONFIG_PATH:
            try:
                r = subprocess.run(
                    ['sudo', '/usr/bin/cat', DEPLOY_CONFIG_PATH],
                    capture_output=True, text=True
                )
                if r.returncode == 0:
                    return tomlkit.loads(r.stdout), path
            except Exception:
                pass
    return tomlkit.document(), None

def save_config(conf):
    with open(CONFIG_PATH, 'w') as f:
        f.write(tomlkit.dumps(conf))

def deploy_config(conf=None):
    """Backup CONFIG_PATH → .bak, then deploy to DEPLOY_CONFIG_PATH via sudo tee.
    Uses: sudo /usr/bin/tee /var/lib/zeroclaw/.zeroclaw/config.toml
    Requires the matching sudoers rule in daemon/sudoers.d-clawboard.
    If conf is provided, use it directly (bypasses re-reading from disk).
    Returns (ok: bool, message: str)."""
    import shutil
    # Step 1: backup local copy
    bak = CONFIG_PATH + '.bak'
    try:
        shutil.copy2(CONFIG_PATH, bak)
    except Exception as e:
        return False, f'Backup failed: {e}'
    # Step 2: use the provided conf, or load from disk
    if conf is not None:
        # Round-trip through TOML to get a deep copy independent of the
        # wizard's in-memory dict (paired_tokens merge won't mutate caller).
        conf_to_deploy = tomlkit.loads(tomlkit.dumps(conf))
    else:
        try:
            with open(CONFIG_PATH, 'r') as f:
                conf_to_deploy = tomlkit.load(f)
        except Exception as e:
            return False, f'Read failed: {e}'
    # Preserve paired_tokens from the live deploy path.
    # zeroclaw encrypts paired_tokens with its own runtime key; overwriting
    # them with a stale local copy causes "Decryption failed" on next startup.
    try:
        with open(DEPLOY_CONFIG_PATH, 'r') as lf:
            _live = tomlkit.load(lf)
        _live_tokens = _live.get('gateway', {}).get('paired_tokens')
        if _live_tokens is not None:
            conf_to_deploy.setdefault('gateway', {})['paired_tokens'] = _live_tokens
    except Exception:
        pass  # live file may not exist yet; proceed without
    content = tomlkit.dumps(conf_to_deploy)
    # Step 3: ensure target directory exists (best-effort, may already exist)
    subprocess.run(
        ['sudo', '/usr/bin/mkdir', '-p', os.path.dirname(DEPLOY_CONFIG_PATH)],
        capture_output=True
    )
    # Step 4: write via sudo tee (stdin pipe, no shell glob needed)
    r = subprocess.run(
        ['sudo', '/usr/bin/tee', DEPLOY_CONFIG_PATH],
        input=content, capture_output=True, text=True
    )
    if r.returncode != 0:
        err = r.stderr.strip() or f'sudo tee failed (exit {r.returncode})'
        return False, err
    # Ensure zeroclaw can read/write its own config (for key encryption).
    subprocess.run(
        ['sudo', '/usr/bin/chown', 'zeroclaw:zeroclaw', DEPLOY_CONFIG_PATH],
        capture_output=True
    )
    # Step 5: read-back verification — ensure the deploy target actually
    # has the content we wrote (catches silent tee failures and cases where
    # a concurrently-running zeroclaw rewrites the file between steps).
    try:
        r2 = subprocess.run(
            ['sudo', '/usr/bin/cat', DEPLOY_CONFIG_PATH],
            capture_output=True, text=True
        )
        if r2.returncode == 0:
            _deployed = tomlkit.loads(r2.stdout)
            _wrote = tomlkit.loads(content)
            _wrote_models = _wrote.get('providers', {}).get('models', {})
            _deployed_models = _deployed.get('providers', {}).get('models', {})
            if set(_wrote_models.keys()) != set(_deployed_models.keys()):
                # Extract just the models section from both for diagnosis
                _wrote_keys = sorted(_wrote_models.keys())
                _deployed_keys = sorted(_deployed_models.keys())
                return False, (
                    f'Deploy verification failed.\n'
                    f'Source ({CONFIG_PATH}): {_wrote_keys}\n'
                    f'Target ({DEPLOY_CONFIG_PATH}): {_deployed_keys}\n'
                    f'Extra keys in target: {sorted(set(_deployed_keys) - set(_wrote_keys))}'
                )
    except Exception:
        pass  # verification is best-effort; don't block deploy
    return True, ''

def deploy_picoclaw_config():
    """Deploy picoclaw config.json from local workspace copy to PicoClaw's live path via sudo tee.
    Requires sudoers rules:
      zero ALL=(root) NOPASSWD: /usr/bin/tee /var/lib/picoclaw/.picoclaw/config.json
      zero ALL=(root) NOPASSWD: /usr/bin/mkdir -p /var/lib/picoclaw/.picoclaw
    Returns (ok: bool, message: str)."""
    try:
        with open(PICOCLAW_CONFIG_PATH, 'r') as f:
            content = f.read()
    except Exception as e:
        return False, f'Read failed: {e}'
    subprocess.run(
        ['sudo', '/usr/bin/mkdir', '-p', os.path.dirname(PICOCLAW_DEPLOY_CONFIG_PATH)],
        capture_output=True
    )
    r = subprocess.run(
        ['sudo', '/usr/bin/tee', PICOCLAW_DEPLOY_CONFIG_PATH],
        input=content, capture_output=True, text=True
    )
    if r.returncode != 0:
        err = r.stderr.strip() or f'sudo tee failed (exit {r.returncode})'
        return False, err
    subprocess.run(
        ['sudo', '/usr/bin/chown', 'picoclaw:picoclaw', PICOCLAW_DEPLOY_CONFIG_PATH],
        capture_output=True
    )
    return True, ''

def deploy_picoclaw_security():
    """Deploy picoclaw security.yml from local workspace copy to PicoClaw's live path via sudo tee.
    Also sets chmod 600 on the deployed file (api_keys must not be world-readable).
    Requires sudoers rules:
      zero ALL=(root) NOPASSWD: /usr/bin/tee /var/lib/picoclaw/.picoclaw/.security.yml
      zero ALL=(root) NOPASSWD: /usr/bin/chmod 600 /var/lib/picoclaw/.picoclaw/.security.yml
    Returns (ok: bool, message: str)."""
    try:
        with open(PICOCLAW_SECURITY_YML_LOCAL, 'r') as f:
            content = f.read()
    except Exception as e:
        return False, f'Read failed: {e}'
    subprocess.run(
        ['sudo', '/usr/bin/mkdir', '-p', os.path.dirname(PICOCLAW_DEPLOY_SECURITY_PATH)],
        capture_output=True
    )
    r = subprocess.run(
        ['sudo', '/usr/bin/tee', PICOCLAW_DEPLOY_SECURITY_PATH],
        input=content, capture_output=True, text=True
    )
    if r.returncode != 0:
        err = r.stderr.strip() or f'sudo tee failed (exit {r.returncode})'
        return False, err
    # Ensure api_keys are not readable by other users
    subprocess.run(
        ['sudo', '/usr/bin/chmod', '600', PICOCLAW_DEPLOY_SECURITY_PATH],
        capture_output=True
    )
    subprocess.run(
        ['sudo', '/usr/bin/chown', 'picoclaw:picoclaw', PICOCLAW_DEPLOY_SECURITY_PATH],
        capture_output=True
    )
    return True, ''

def deploy_character(src_dir: str, workspace_dir: str, owner: str, include_memory: bool = False) -> tuple[bool, str]:
    """Copy files from src_dir (character/agent subfolder) into workspace_dir using sudo tee.
    Creates workspace_dir if it doesn't exist. Sets ownership to owner:owner.
    MEMORY.md is skipped unless include_memory=True.
    Returns (ok, message)."""
    try:
        files = [
            f for f in os.listdir(src_dir)
            if os.path.isfile(os.path.join(src_dir, f))
            and (f != 'MEMORY.md' or include_memory)
        ]
    except Exception as exc:
        return False, f'Cannot list source: {exc}'
    if not files:
        return False, 'No files in character folder (or only MEMORY.md which is excluded by default)'
    subprocess.run(['sudo', '/usr/bin/mkdir', '-p', workspace_dir], capture_output=True)
    errors: list[str] = []
    for fname in files:
        try:
            with open(os.path.join(src_dir, fname), 'r', encoding='utf-8') as fh:
                content = fh.read()
        except Exception as exc:
            errors.append(f'{fname}: read error: {exc}')
            continue
        r = subprocess.run(
            ['sudo', '/usr/bin/tee', os.path.join(workspace_dir, fname)],
            input=content, capture_output=True, text=True,
        )
        if r.returncode != 0:
            errors.append(f'{fname}: write error: {r.stderr.strip()}')
    subprocess.run(
        ['sudo', '/usr/bin/chown', '-R', f'{owner}:{owner}', workspace_dir],
        capture_output=True,
    )
    if errors:
        return False, '; '.join(errors)
    return True, ''

def restart_service():
    """Restart via systemctl. Requires narrow sudoers rules — no password needed.
    Install with: sudo cp daemon/sudoers.d-clawboard /etc/sudoers.d/clawboard
    Required rules (see daemon/sudoers.d-clawboard):
      zero ALL=(root) NOPASSWD: /usr/bin/systemctl restart zeroclaw.service
      zero ALL=(root) NOPASSWD: /usr/bin/cat /var/lib/picoclaw/.picoclaw/.picoclaw.pid
      zero ALL=(root) NOPASSWD: /usr/bin/cat /var/lib/picoclaw/.picoclaw/.security.yml
    """
    r = subprocess.run(
        ['sudo', '/usr/bin/systemctl', 'restart', 'zeroclaw.service'],
        capture_output=True, text=True
    )
    return r.returncode == 0, r.stderr.strip()

def service_status():
    r = subprocess.run(['systemctl', 'is-active', 'zeroclaw.service'], capture_output=True, text=True)
    return r.stdout.strip()

def restart_picoclaw_service():
    """Restart picoclaw.service via sudo systemctl.
    Requires sudoers rule:
      zero ALL=(root) NOPASSWD: /usr/bin/systemctl restart picoclaw.service
    Returns (ok: bool, stderr: str)."""
    r = subprocess.run(
        ['sudo', '/usr/bin/systemctl', 'restart', 'picoclaw.service'],
        capture_output=True, text=True
    )
    return r.returncode == 0, r.stderr.strip()

def _http_json(
    url: str,
    method: str = 'GET',
    timeout: int = 10,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    req = urllib.request.Request(url, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode('utf-8')
        return json.loads(raw) if raw else {}

def setup_pico_channel_token() -> tuple[bool, str]:
    """Ensure the pico channel token exists — mirrors EnsurePicoChannel() locally.

    Does NOT contact port 18800 (picoclaw-web), which requires browser auth.
    Steps (same logic as _local_pico_setup in chat_picoclaw.py):
      1. Idempotency: if token already in live .security.yml, return immediately.
      2. Enable channel_list.pico in local config.json, deploy to live path.
      3. Generate secrets.token_hex(16) (Go: generateSecureToken = 16 rand bytes → hex).
      4. Write token into local security.yml, deploy to live path.
      5. Restart picoclaw service.
      6. Poll up to 15 s for channel_list.pico.settings.token in live .security.yml.
    """
    import secrets as _secrets

    # ── 1. Idempotency ────────────────────────────────────────────────────────
    tok, _ = _read_security_yml_token('pico')
    if tok:
        return True, ''

    # ── 2. Enable channel_list.pico in config.json ────────────────────────────
    try:
        _pc = load_picoclaw_config() or {}
        _pc.setdefault('channel_list', {}).setdefault('pico', {})['enabled'] = True
        save_picoclaw_config(_pc)
        ok_cfg, err_cfg = deploy_picoclaw_config()
        if not ok_cfg:
            return False, f'deploy_picoclaw_config failed: {err_cfg}'
    except Exception as e:
        return False, f'failed to enable channel_list.pico in config: {e}'

    # ── 3 + 4. Generate token, write + deploy security.yml ────────────────────
    try:
        _sec = load_picoclaw_security()
        _sec_settings = (
            _sec
            .setdefault('channel_list', {})
            .setdefault('pico', {})
            .setdefault('settings', {})
        )
        if not _sec_settings.get('token'):
            _sec_settings['token'] = _secrets.token_hex(16)
        save_picoclaw_security(_sec)
        ok_sec, err_sec = deploy_picoclaw_security()
        if not ok_sec:
            return False, f'deploy_picoclaw_security failed: {err_sec}'
    except Exception as e:
        return False, f'failed to write token to security.yml: {e}'

    # ── 5. Restart picoclaw ───────────────────────────────────────────────────
    ok_svc, svc_err = restart_picoclaw_service()
    if not ok_svc:
        return False, f'restart failed: {svc_err or "unknown"}'

    # ── 6. Poll for token in live .security.yml (up to 15 s) ─────────────────
    last_err = ''
    for _ in range(30):
        tok, err = _read_security_yml_token('pico')
        if tok:
            return True, ''
        last_err = err
        _time.sleep(0.5)
    return False, last_err or 'channel_list.pico.settings.token still missing after setup'

def picoclaw_service_status():
    r = subprocess.run(['systemctl', 'is-active', 'picoclaw.service'], capture_output=True, text=True)
    return r.stdout.strip()

def to_int(v, default=0):
    try:   return int(float(v))
    except: return default

def to_float(v, default=0.0):
    try:   return float(v)
    except: return default

def lines_to_list(text):
    return [l.strip() for l in text.splitlines() if l.strip()]

@ui.page('/')
def index(request: Request):
    if not _is_authed(): ui.navigate.to('/login'); return
    lang       = request.query_params.get('lang', 'zh')
    T          = zh_strings.STRINGS if lang == 'zh' else en_strings.STRINGS
    other_lang = 'en' if lang == 'zh' else 'zh'
    zc_source  = request.query_params.get('zc_source', 'local')  # 'local' (source of truth) or 'runtime'

    conf, _loaded_from = load_config(source=zc_source)
    # Warn if the requested source wasn't available and we fell back
    if _loaded_from and zc_source == 'runtime' and _loaded_from == CONFIG_PATH:
        ui.notify('Runtime config not found — loaded local template instead', type='warning')
    elif _loaded_from and zc_source == 'local' and _loaded_from == DEPLOY_CONFIG_PATH:
        ui.notify('Local template not found — loaded runtime config instead', type='warning')
    _diag = ''  # populated by _wiz_apply; read by _wiz_refresh_summary (shared index scope)
    provider_panels = {}
    channel_panels  = {}

    # ── ZeroClaw provider hints ───────────────────────────────────────────
    _ph_hints  = load_provider_hints()

    # ── PicoClaw provider hints ───────────────────────────────────────────
    _pc_ph_hints  = load_pc_provider_hints()
    _pc_ph_map    = {h['model_name']: h for h in _pc_ph_hints if h.get('model_name')}
    _pc_ph_provs: list[str] = []
    _pc_ph_pid_base:   dict[str, str]       = {}  # provider → first api_base
    _pc_ph_pid_models: dict[str, list[str]] = {}  # provider → [model_names...]
    for _pch in _pc_ph_hints:
        _pcpid = _pch.get('provider', '')
        if not _pcpid:
            continue
        if _pcpid not in _pc_ph_provs:
            _pc_ph_provs.append(_pcpid)
        if _pcpid not in _pc_ph_pid_base and _pch.get('api_base'):
            _pc_ph_pid_base[_pcpid] = _pch['api_base']
        if _pch.get('model_name'):
            _pc_ph_pid_models.setdefault(_pcpid, []).append(_pch['model_name'])

    # ── OpenClaw provider hints ───────────────────────────────────────────
    _oc_ph_hints  = load_oc_provider_hints()
    _oc_ph_map    = {h['model_name']: h for h in _oc_ph_hints if h.get('model_name')}
    _oc_ph_provs: list[str] = []
    _oc_ph_pid_base:   dict[str, str]       = {}  # provider → api_base
    _oc_ph_pid_models: dict[str, list[str]] = {}  # provider → [model_names...]
    for _och in _oc_ph_hints:
        _ocpid = _och.get('provider', '')
        if not _ocpid:
            continue
        if _ocpid not in _oc_ph_provs:
            _oc_ph_provs.append(_ocpid)
        if _ocpid not in _oc_ph_pid_base and _och.get('api_base'):
            _oc_ph_pid_base[_ocpid] = _och['api_base']
        if _och.get('model_name'):
            _oc_ph_pid_models.setdefault(_ocpid, []).append(_och['model_name'])

    # (ZeroClaw hints continued)
    _ph_models = [h['model'] for h in _ph_hints if h.get('model')]
    _ph_pid_base:   dict[str, str]       = {}  # provider_id → first api_base
    _ph_pid_models: dict[str, list[str]] = {}  # provider_id → [models...]
    for _hh in _ph_hints:
        _hpid   = _hh.get('provider_id', '')
        _hmodel = _hh.get('model', '')
        if _hpid:
            if _hpid not in _ph_pid_base and _hh.get('api_base'):
                _ph_pid_base[_hpid] = _hh['api_base']
            if _hmodel:
                _ph_pid_models.setdefault(_hpid, []).append(_hmodel)

    def build_provider_card(container, alias, mp_data):
        with container:
            with ui.card().classes('w-full q-mb-sm') as card:
                with ui.row().classes('w-full items-center justify-between'):
                    ui.label(f'[providers.models.{alias}]').classes('text-caption text-blue-7 text-bold')
                    def _rm(a=alias, c=card):
                        provider_panels.pop(a, None); c.delete()
                    ui.button(icon='delete', on_click=_rm).props('flat round dense color=negative')
                w_name = ui.select(PROVIDER_IDS, label=T['lbl_provider_name'],
                    value=mp_data.get('name', alias) if mp_data.get('name', alias) in PROVIDER_IDS else PROVIDER_IDS[0]
                ).classes('w-full')
                w_base_url    = ui.input(T['lbl_provider_base_url'], value=str(mp_data.get('uri') or mp_data.get('base_url', ''))).classes('w-full')
                w_openai_auth = ui.checkbox('requires_openai_auth', value=bool(mp_data.get('requires_openai_auth', False)))
                w_api_key_mp  = ui.input(T['lbl_provider_api_key'], value=str(mp_data.get('api_key', '')),
                                         password=True, password_toggle_button=True).classes('w-full')
                provider_panels[alias] = {'name': w_name, 'base_url': w_base_url,
                                          'requires_openai_auth': w_openai_auth, 'api_key': w_api_key_mp}

                # Auto-fill base_url from provider hints when provider name changes
                def _autofill_base(e, _wb=w_base_url, _pm=_ph_pid_base):
                    pid = e.value
                    if pid and pid in _pm:
                        _wb.set_value(_pm[pid])
                w_name.on_value_change(_autofill_base)

    def build_channel_card(container, ch_key, ch_data):
        schema = CHANNEL_SCHEMAS.get(ch_key)
        if schema is None: return
        with container:
            with ui.card().classes('w-full q-mb-sm') as card:
                with ui.row().classes('w-full items-center justify-between'):
                    ui.label(f'[channels.{ch_key}]').classes('text-caption text-green-7 text-bold')
                    def _rm(k=ch_key, c=card):
                        channel_panels.pop(k, None); c.delete()
                    ui.button(icon='delete', on_click=_rm).props('flat round dense color=negative')
                widgets = {}
                for (fkey, flabel, ftype, fdefault) in schema['fields']:
                    raw = ch_data.get(fkey, fdefault)
                    if ftype == 'text':
                        widgets[fkey] = ui.input(flabel, value=str(raw)).classes('w-full')
                    elif ftype == 'password':
                        widgets[fkey] = ui.input(flabel, value=str(raw), password=True, password_toggle_button=True).classes('w-full')
                    elif ftype == 'bool':
                        widgets[fkey] = ui.checkbox(flabel, value=bool(raw))
                    elif ftype == 'int':
                        widgets[fkey] = ui.number(flabel, value=to_int(raw, fdefault), step=1).classes('w-full')
                    elif ftype == 'textarea':
                        if isinstance(raw, list): raw = '\n'.join(str(x) for x in raw)
                        ui.label(flabel).classes('text-caption text-grey-6')
                        widgets[fkey] = ui.textarea(value=str(raw)).classes('w-full').props('outlined rows=3')
                    elif ftype.startswith('select:'):
                        opts = ftype.split(':', 1)[1].split(',')
                        cur  = str(raw) if str(raw) in opts else opts[0]
                        widgets[fkey] = ui.select(opts, label=flabel, value=cur).classes('w-full')
                channel_panels[ch_key] = widgets

    def collect():
        conf.setdefault('secrets',  {})['encrypt'] = w_secrets_encrypt.value
        conf.setdefault('identity', {})['format']  = w_identity_format.value

        # ── Provider models → [providers.models.<alias>] (ZeroClaw schema v2)
        default_temp    = to_float(w_temperature.value, 0.7)
        default_timeout = to_int(w_prov_timeout.value, 120)
        default_model   = w_default_model.value.strip() or ''
        default_key     = w_api_key.value or ''

        conf.setdefault('providers', {}).setdefault('models', {})
        # Replace the models table wholesale to pick up removals
        conf['providers']['models'] = {}
        for alias, wmap in provider_panels.items():
            entry = {
                'name':                  wmap['name'].value,
                'uri':                   wmap['base_url'].value,
                'requires_openai_auth':  wmap['requires_openai_auth'].value,
                'model':                 default_model,
                'temperature':           default_temp,
                'timeout_secs':          default_timeout,
            }
            key = wmap['api_key'].value
            if not key and default_key:
                key = default_key
            if key:
                entry['api_key'] = key
            conf['providers']['models'][alias] = entry

        a = conf.setdefault('autonomy', {})
        a['level']                            = w_auto_level.value
        a['workspace_only']                   = w_auto_workspace.value
        a['require_approval_for_medium_risk'] = w_auto_require_approval.value
        a['block_high_risk_commands']         = w_auto_block_high.value
        a['max_actions_per_hour']             = to_int(w_auto_max_actions.value, 20)
        a['max_cost_per_day_cents']           = to_int(w_auto_max_cost.value, 500)
        a['allowed_commands']                 = lines_to_list(w_auto_cmds.value)
        a['auto_approve']                     = lines_to_list(w_auto_approve.value)
        a['always_ask']                       = lines_to_list(w_auto_always_ask.value)
        a['forbidden_paths']                  = lines_to_list(w_auto_forbidden.value)
        a['allowed_roots']                    = lines_to_list(w_auto_allowed_roots.value)
        a['shell_env_passthrough']            = lines_to_list(w_auto_shell_env.value)

        ag = conf.setdefault('agent', {})
        ag['compact_context']      = w_agent_compact.value
        ag['parallel_tools']       = w_agent_parallel.value
        ag['max_tool_iterations']  = to_int(w_agent_max_iter.value, 10)
        ag['max_history_messages'] = to_int(w_agent_max_hist.value, 50)
        ag['tool_dispatcher']      = w_agent_tool_dispatcher.value

        o = conf.setdefault('observability', {})
        o['backend']                   = w_obs_backend.value
        o['runtime_trace_mode']        = w_obs_trace_mode.value
        o['otel_endpoint']             = w_obs_otel_endpoint.value
        o['otel_service_name']         = w_obs_otel_service.value
        o['runtime_trace_path']        = w_obs_trace_path.value
        o['runtime_trace_max_entries'] = to_int(w_obs_trace_max.value, 200)

        sk = conf.setdefault('skills', {})
        sk['open_skills_enabled']   = w_skills_open.value
        sk['allow_scripts']         = w_skills_allow_scripts.value
        sk['prompt_injection_mode'] = w_skills_mode.value
        sk.setdefault('skill_creation', {})['enabled'] = w_sk_cr_enabled.value
        sk['skill_creation']['max_skills'] = to_int(w_sk_cr_max.value, 500)
        sk['skill_creation']['similarity_threshold'] = to_float(w_sk_cr_sim.value, 0.85)
        sk.setdefault('skill_improvement', {})['enabled'] = w_sk_im_enabled.value
        sk['skill_improvement']['cooldown_secs'] = to_int(w_sk_im_cooldown.value, 3600)

        m = conf.setdefault('memory', {})
        m['backend']                    = w_mem_backend.value
        m['search_mode']                = w_mem_search_mode.value
        m['auto_save']                  = w_mem_auto_save.value
        m['hygiene_enabled']            = w_mem_hygiene.value
        m['archive_after_days']         = to_int(w_mem_archive_days.value, 7)
        m['purge_after_days']           = to_int(w_mem_purge_days.value, 30)
        m['conversation_retention_days']= to_int(w_mem_conv_retention.value, 30)
        m['embedding_provider']         = w_mem_embed_provider.value
        m['embedding_model']            = w_mem_embed_model.value
        m['embedding_dimensions']       = to_int(w_mem_embed_dims.value, 1536)
        m['vector_weight']              = to_float(w_mem_vec_weight.value, 0.7)
        m['keyword_weight']             = to_float(w_mem_kw_weight.value, 0.3)
        m['min_relevance_score']        = to_float(w_mem_min_relevance.value, 0.4)
        m['embedding_cache_size']       = to_int(w_mem_cache_size.value, 10000)
        m['chunk_max_tokens']           = to_int(w_mem_chunk_tokens.value, 512)
        m['response_cache_enabled']     = w_mem_resp_cache.value
        m['response_cache_ttl_minutes'] = to_int(w_mem_resp_ttl.value, 60)
        m['response_cache_max_entries'] = to_int(w_mem_resp_max.value, 5000)
        m['snapshot_enabled']           = w_mem_snapshot.value
        m['snapshot_on_hygiene']        = w_mem_snap_hygiene.value
        m['auto_hydrate']               = w_mem_auto_hydrate.value

        g = conf.setdefault('gateway', {})
        g['port']              = to_int(w_gw_port.value, 42617)
        g['host']              = w_gw_host.value
        g['require_pairing']   = w_gw_pairing.value
        g['allow_public_bind'] = w_gw_public.value

        conf.setdefault('tunnel', {})['provider'] = w_tunnel.value

        ch_conf = conf.setdefault('channels', {})
        ch_conf['cli']                  = w_cli_enabled.value
        ch_conf['message_timeout_secs'] = to_int(w_msg_timeout.value, 300)
        ch_conf['ack_reactions']        = w_ch_ack_reactions.value
        ch_conf['show_tool_calls']      = w_ch_show_tool_calls.value
        ch_conf['session_persistence']  = w_ch_session_persist.value
        ch_conf['session_backend']      = w_ch_session_backend.value
        ch_conf['session_ttl_hours']    = to_int(w_ch_session_ttl.value, 0)
        ch_conf['debounce_ms']          = to_int(w_ch_debounce_ms.value, 0)
        _static_ch_keys = {
            'cli', 'message_timeout_secs', 'ack_reactions', 'show_tool_calls',
            'session_persistence', 'session_backend', 'session_ttl_hours', 'debounce_ms',
        }
        for k in [k for k in list(ch_conf.keys()) if k not in _static_ch_keys]:
            del ch_conf[k]
        for ch_key, wmap in channel_panels.items():
            schema = CHANNEL_SCHEMAS[ch_key]; entry = {}
            for (fkey, _fl, ftype, _fd) in schema['fields']:
                w = wmap.get(fkey)
                if w is None: continue
                if ftype == 'textarea':   entry[fkey] = lines_to_list(w.value)
                elif ftype == 'bool':     entry[fkey] = w.value
                elif ftype == 'int':      entry[fkey] = to_int(w.value)
                else:                     entry[fkey] = w.value
            ch_conf[ch_key] = entry

        sec = conf.setdefault('security', {})
        sr = sec.setdefault('resources', {})
        sr['max_memory_mb']        = to_int(w_sec_mem.value, 512)
        sr['max_cpu_time_seconds'] = to_int(w_sec_cpu.value, 60)
        sr['max_subprocesses']     = to_int(w_sec_procs.value, 10)
        sr['memory_monitoring']    = w_sec_mem_monitor.value

        sec.setdefault('sandbox', {})['backend'] = w_sec_sandbox.value

        sa = sec.setdefault('audit', {})
        sa['enabled']     = w_sec_audit_enabled.value
        sa['log_path']    = w_sec_audit_log_path.value
        sa['max_size_mb'] = to_int(w_sec_audit_max.value, 100)
        sa['sign_events'] = w_sec_audit_sign.value

        so = sec.setdefault('otp', {})
        so['enabled']          = w_sec_otp_enabled.value
        so['method']           = w_sec_otp_method.value
        so['token_ttl_secs']   = to_int(w_sec_otp_ttl.value, 30)
        so['cache_valid_secs'] = to_int(w_sec_otp_cache.value, 300)
        so['gated_actions']    = lines_to_list(w_sec_otp_actions.value)
        so['gated_domains']    = lines_to_list(w_sec_otp_domains.value)

        se = sec.setdefault('estop', {})
        se['enabled']               = w_sec_estop_enabled.value
        se['state_file']            = w_sec_estop_file.value
        se['require_otp_to_resume'] = w_sec_estop_otp.value

        r = conf.setdefault('reliability', {})
        r['provider_retries']             = to_int(w_rel_retries.value, 2)
        r['provider_backoff_ms']          = to_int(w_rel_backoff.value, 500)
        r['channel_initial_backoff_secs'] = to_int(w_rel_ch_backoff.value, 2)
        r['channel_max_backoff_secs']     = to_int(w_rel_ch_max.value, 60)

        s = conf.setdefault('scheduler', {})
        s['enabled']        = w_sched_enabled.value
        s['max_tasks']      = to_int(w_sched_tasks.value, 64)
        s['max_concurrent'] = to_int(w_sched_concurrent.value, 4)

        wf = conf.setdefault('web_fetch', {})
        wf['enabled']          = w_wf_enabled.value
        wf['allowed_domains']  = lines_to_list(w_wf_domains.value)
        wf['blocked_domains']  = lines_to_list(w_wf_blocked.value)
        wf['max_response_size']= to_int(w_wf_max_size.value, 500000)
        wf['timeout_secs']     = to_int(w_wf_timeout.value, 30)

        ws = conf.setdefault('web_search', {})
        ws['enabled']      = w_ws_enabled.value
        ws['provider']     = w_ws_provider.value
        ws['max_results']  = to_int(w_ws_max.value, 5)
        ws['timeout_secs'] = to_int(w_ws_timeout.value, 15)

        hr = conf.setdefault('http_request', {})
        hr['enabled']          = w_http_enabled.value
        hr['allowed_domains']  = lines_to_list(w_http_domains.value)
        hr['max_response_size']= to_int(w_http_max_size.value, 1000000)
        hr['timeout_secs']     = to_int(w_http_timeout.value, 30)

        br = conf.setdefault('browser', {})
        br['enabled']             = w_br_enabled.value
        br['allowed_domains']     = lines_to_list(w_br_domains.value)
        br['backend']             = w_br_backend.value
        br['native_headless']     = w_br_headless.value
        br['native_webdriver_url']= w_br_webdriver.value

        mm = conf.setdefault('multimodal', {})
        mm['max_images']         = to_int(w_mm_images.value, 4)
        mm['max_image_size_mb']  = to_int(w_mm_image_size.value, 5)
        mm['allow_remote_fetch'] = w_mm_remote.value

        c = conf.setdefault('cost', {})
        c['enabled']           = w_cost_enabled.value
        c['daily_limit_usd']   = to_float(w_cost_daily.value, 10.0)
        c['monthly_limit_usd'] = to_float(w_cost_monthly.value, 100.0)
        c['warn_at_percent']   = to_int(w_cost_warn.value, 80)
        c['allow_override']    = w_cost_override.value

        cp = conf.setdefault('composio', {})
        cp['enabled']   = w_comp_enabled.value
        cp['entity_id'] = w_comp_entity.value

        conf.setdefault('hooks', {})['enabled'] = w_hooks_enabled.value

        hw = conf.setdefault('hardware', {})
        hw['enabled']             = w_hw_enabled.value
        hw['transport']           = w_hw_transport.value
        hw['baud_rate']           = to_int(w_hw_baud.value, 115200)
        hw['workspace_datasheets']= w_hw_datasheets.value

        tr = conf.setdefault('transcription', {})
        tr['enabled']          = w_tr_enabled.value
        tr['api_url']          = w_tr_url.value
        tr['model']            = w_tr_model.value
        tr['max_duration_secs']= to_int(w_tr_max_duration.value, 120)

        hb = conf.setdefault('heartbeat', {})
        hb['enabled']          = w_hb_enabled.value
        hb['interval_minutes'] = to_int(w_hb_interval.value, 30)

        cr = conf.setdefault('cron', {})
        cr['enabled']         = w_cron_enabled.value
        cr['max_run_history'] = to_int(w_cron_max_history.value, 50)

    def do_save():
        try:
            collect(); save_config(conf)
            ui.notify(T['notify_saved'], type='positive')
            ui.timer(1.0, lambda: ui.navigate.to(
                f'/?lang={lang}&zc_source={zc_source}'))
        except Exception as e:
            ui.notify(T['notify_save_fail'].format(e), type='negative')

    def do_save_deploy():
        try:
            # 1. Collect form values → save to config/config.toml
            collect()
            save_config(conf)
            # 2. Deploy to runtime
            ok_deploy, deploy_err = deploy_config()
            if not ok_deploy:
                ui.notify(f'⚠️ Saved locally but deploy failed: {deploy_err}', type='warning')
                return
            ui.notify(T['notify_saved_restarted'], type='positive')
            ui.timer(2.0, lambda: ui.navigate.to(
                f'/?lang={lang}&zc_source={zc_source}'))
        except Exception as e:
            ui.notify(T['notify_op_fail'].format(e), type='negative')

    def do_status():
        st = service_status()
        ui.notify(T['notify_service'].format(st), type='positive' if st == 'active' else 'negative')

    # ── shortcuts ─────────────────────────────────────────────────────────────
    top          = conf
    autonomy     = conf.get('autonomy',    {})
    agent_c      = conf.get('agent',       {})
    obs          = conf.get('observability',{})
    skills       = conf.get('skills',      {})
    memory       = conf.get('memory',      {})
    gateway      = conf.get('gateway',     {})
    ch_conf_top  = conf.get('channels', {})
    sec          = conf.get('security',    {})
    sec_res      = sec.get('resources',   {})
    sec_sandbox  = sec.get('sandbox',     {})
    sec_audit    = sec.get('audit',       {})
    sec_otp      = sec.get('otp',         {})
    sec_estop    = sec.get('estop',       {})
    reliability  = conf.get('reliability', {})
    scheduler    = conf.get('scheduler',   {})
    web_fetch    = conf.get('web_fetch',   {})
    web_search   = conf.get('web_search',  {})
    http_request = conf.get('http_request',{})
    browser      = conf.get('browser',     {})
    multimodal   = conf.get('multimodal',  {})
    cost         = conf.get('cost',        {})
    composio_c   = conf.get('composio',    {})
    tunnel       = conf.get('tunnel',      {})
    transcription= conf.get('transcription',{})
    heartbeat    = conf.get('heartbeat',   {})
    cron         = conf.get('cron',        {})
    hooks        = conf.get('hooks',       {})
    hardware     = conf.get('hardware',    {})
    identity     = conf.get('identity',    {})
    secrets_c    = conf.get('secrets',     {})

    # Derive General-tab provider defaults from [providers.models] (schema v2)
    _prov_models   = conf.get('providers', {}).get('models', {})
    _default_prov  = next(iter(_prov_models.values()), {}) if _prov_models else {}

    # ── Header ────────────────────────────────────────────────────────────────
    with ui.header().classes('bg-blue-9 text-white q-pa-sm row items-center justify-between'):
        with ui.row().classes('items-center gap-1'):
            ui.button(icon='menu', on_click=lambda: drawer.toggle()).props('flat round dense color=white')
            ui.label(T['app_title']).classes('text-h6')
        with ui.row().classes('gap-1 items-center'):
            ui.button(T['lbl_lang_switch'],
                on_click=lambda: ui.navigate.to(f'/?lang={other_lang}')
            ).props('flat dense color=white no-caps')
            ui.button(icon='info', on_click=do_status).props('flat round dense color=white')
            ui.button(icon='logout', on_click=_logout).props('flat round dense color=white').tooltip('Logout')

    # ── Sidebar ───────────────────────────────────────────────────────────────
    with ui.left_drawer(value=True, bordered=True).classes('bg-grey-1') as drawer:
        ui.label('ClawBoard').classes('text-subtitle1 text-bold text-blue-9 q-pa-sm q-pb-xs')
        ui.separator()
        btn_zc   = ui.button('🦾 ZeroClaw',    icon='terminal').props('flat align=left color=blue-8').classes('w-full q-mt-xs')
        btn_pc   = ui.button('🐾 PicoClaw',     icon='memory'  ).props('flat align=left color=grey-7').classes('w-full')
        btn_oc   = ui.button('📂 OpenClaw',     icon='folder'  ).props('flat align=left color=grey-7').classes('w-full')
        btn_wifi    = ui.button(T['btn_wifi'],    icon='wifi'    ).props('flat align=left color=grey-7').classes('w-full')
        btn_proxy   = ui.button(T['btn_proxy'],   icon='swap_horiz').props('flat align=left color=grey-7').classes('w-full')
        btn_upgrade = ui.button(T['btn_upgrade'], icon='system_update_alt').props('flat align=left color=grey-7').classes('w-full')

    # ── Character tab builder (shared by all agents) ───────────────────────────
    def _build_character_tab(workspace_dir: str, owner: str, accent: str = 'blue-8'):
        """Render the Characters tab panel body for a given agent workspace.
        Character files are read from characters/{name}/{owner}/ subfolders.
        MEMORY.md is excluded from the default deploy; a separate opt-in checkbox
        with a warning is shown instead."""
        chars_dir = os.path.join(SCRIPT_DIR, 'characters')
        agent_key = owner  # subfolder name inside each character dir (picoclaw / zeroclaw / …)
        char_names: list[str] = []
        if os.path.isdir(chars_dir):
            char_names = sorted([
                d for d in os.listdir(chars_dir)
                if os.path.isdir(os.path.join(chars_dir, d))
            ])
        if not char_names:
            ui.label(T['char_no_chars']).classes('text-caption text-grey-6 q-mt-sm')
            return
        with ui.card().classes('w-full q-pa-md'):
            ui.label(T['char_title']).classes(f'text-subtitle1 text-bold text-{accent} q-mb-xs')
            ui.label(f'{T["char_workspace_lbl"]}: {workspace_dir}').classes('text-caption text-grey-7 q-mb-sm font-mono')
            char_select = ui.select(
                char_names, label=T['char_select_label'], value=char_names[0],
            ).classes('w-full q-mb-xs')
            ui.label(T['char_files_label']).classes('text-caption text-grey-6 q-mt-xs')
            files_col = ui.column().classes('w-full q-pl-sm q-mb-sm')

            def _refresh_preview(name: str):
                files_col.clear()
                src = os.path.join(chars_dir, name, agent_key)
                with files_col:
                    if os.path.isdir(src):
                        shown = False
                        for fn in sorted(os.listdir(src)):
                            if os.path.isfile(os.path.join(src, fn)) and fn != 'MEMORY.md':
                                ui.label(f'📄 {fn}').classes('text-caption text-mono')
                                shown = True
                        if not shown:
                            ui.label('(no files)').classes('text-caption text-grey-6')
                    else:
                        ui.label('(not found)').classes('text-caption text-grey-6')

            _refresh_preview(char_names[0])
            char_select.on('update:model-value', lambda e: _refresh_preview(e.value))

            # MEMORY.md: separate opt-in with overwrite warning
            with ui.row().classes('items-center q-mt-xs q-mb-sm'):
                memory_chk = ui.checkbox('MEMORY.md').props('color=warning')
                ui.label(T['char_memory_warn']).classes('text-caption text-warning q-ml-xs')

            deploy_status = ui.label('').classes('text-caption q-mt-xs')

            def _do_deploy(_ws=workspace_dir, _own=owner, _sel=char_select,
                           _st=deploy_status, _mchk=memory_chk):
                src = os.path.join(chars_dir, _sel.value, agent_key)
                ok, err = deploy_character(src, _ws, _own, include_memory=_mchk.value)
                if ok:
                    txt = T['char_deploy_ok'].format(_sel.value, _ws)
                    _st.set_text(txt)
                    _st.classes(remove='text-negative', add='text-positive')
                    ui.notify(txt, type='positive')
                else:
                    txt = T['char_deploy_err'].format(err)
                    _st.set_text(txt)
                    _st.classes(remove='text-positive', add='text-negative')
                    ui.notify(txt, type='negative')

            ui.button(T['char_deploy_btn'], icon='upload', on_click=_do_deploy) \
                .props(f'color={accent} elevated')

    # ── Skills tab builder (shared by all agents) ──────────────────────────────
    def _build_skills_tab(workspace_dir: str, owner: str, accent: str = 'blue-8'):
        """Render the Skills Config tab panel body for a given agent workspace."""
        _sk_ws = os.path.join(workspace_dir, 'skills')
        # Use sudo find — the dashboard runs as 'zero' and cannot directly read
        # agent home dirs (zeroclaw:zeroclaw, picoclaw:picoclaw, etc.).
        _find = subprocess.run(
            ['sudo', '/usr/bin/find', _sk_ws,
             '-mindepth', '2', '-maxdepth', '2', '-name', 'config.json', '-type', 'f'],
            capture_output=True, text=True,
        )
        _skill_configs: list[tuple[str, str]] = []
        if _find.returncode == 0:
            for _line in sorted(_find.stdout.splitlines()):
                _cfg = _line.strip()
                if _cfg:
                    _sn = os.path.basename(os.path.dirname(_cfg))
                    _skill_configs.append((_sn, _cfg))
        if not _skill_configs:
            ui.label(T['skills_no_config']).classes('text-caption text-grey-6 q-mt-sm')
            if _find.returncode != 0 and _find.stderr.strip():
                ui.label(_find.stderr.strip()).classes('text-caption text-red-6 q-mt-xs font-mono')
            return
        _color_base = accent.split('-')[0]
        with ui.tabs().classes(f'w-full bg-{_color_base}-1') as _sk_tabs:
            _sk_tab_objs: list = []
            for _sn, _ in _skill_configs:
                _sk_tab_objs.append(ui.tab(_sn, icon='extension'))
        with ui.tab_panels(_sk_tabs, value=_sk_tab_objs[0]).classes('w-full'):
            for (_sn, _scfg_path), _tab_obj in zip(_skill_configs, _sk_tab_objs):
                with ui.tab_panel(_tab_obj):
                    # Read via sudo — same reason as above
                    _cat = subprocess.run(
                        ['sudo', '/usr/bin/cat', _scfg_path],
                        capture_output=True, text=True,
                    )
                    _raw = _cat.stdout if _cat.returncode == 0 else f'// error reading: {_cat.stderr.strip()}'
                    with ui.card().classes('w-full q-pa-md'):
                        ui.label(f'skills/{_sn}/config.json').classes('text-caption text-grey-7 q-mb-xs font-mono')
                        _ta = ui.textarea(value=_raw).classes('w-full font-mono') \
                                .props('outlined rows=16 label="config.json"')

                        def _make_save(_area=_ta, _skill=_sn, _dst=_scfg_path, _own=owner):
                            def _do_save():
                                raw = _area.value
                                try:
                                    json.loads(raw)
                                except json.JSONDecodeError as _je:
                                    ui.notify(T['skills_json_invalid'].format(_je), type='negative')
                                    return
                                subprocess.run(
                                    ['sudo', '/usr/bin/mkdir', '-p', os.path.dirname(_dst)],
                                    capture_output=True,
                                )
                                r = subprocess.run(
                                    ['sudo', '/usr/bin/tee', _dst],
                                    input=raw, capture_output=True, text=True,
                                )
                                if r.returncode == 0:
                                    subprocess.run(
                                        ['sudo', '/usr/bin/chown', f'{_own}:{_own}', _dst],
                                        capture_output=True,
                                    )
                                    ui.notify(T['skills_saved_ok'].format(_skill), type='positive')
                                else:
                                    ui.notify(T['skills_save_err'].format(r.stderr.strip()), type='negative')
                            return _do_save

                        def _make_fmt(_area=_ta):
                            def _do_fmt():
                                try:
                                    _parsed = json.loads(_area.value)
                                    _area.set_value(json.dumps(_parsed, indent=2, ensure_ascii=False))
                                except json.JSONDecodeError as _je:
                                    ui.notify(T['skills_json_invalid'].format(_je), type='negative')
                            return _do_fmt

                        with ui.row().classes('q-mt-sm gap-2'):
                            ui.button(T['skills_save_btn'], icon='save',
                                      on_click=_make_save()).props(f'color={accent}')
                            ui.button(T['skills_fmt_btn'], icon='format_align_left',
                                      on_click=_make_fmt()).props('flat color=grey-7')

    # ══ ZeroClaw Dashboard ════════════════════════════════════════════════════
    zc_content = ui.column().classes('w-full q-px-sm q-pt-sm')
    with zc_content:
        with ui.row().classes('w-full items-center q-mb-xs'):
            ui.label(T['zc_dashboard']).classes('text-h6 text-blue-9')
            ui.button(icon='refresh', on_click=lambda: ui.navigate.reload()) \
                .props('flat round dense color=blue-9').tooltip(T['tooltip_reload'])
        with ui.tabs().classes('w-full bg-blue-1') as zc_sub_tabs:
            t_zc_wiz  = ui.tab(T['tab_wizard'],        icon='auto_fix_high')
            t_zc_cfg  = ui.tab(T['tab_configuration'], icon='settings')
            t_zc_pair = ui.tab(T['tab_pair_device'],   icon='devices')
            t_zc_char   = ui.tab(T['tab_characters'],    icon='face')
            t_zc_skills = ui.tab(T['tab_skills'],        icon='extension')

        with ui.tab_panels(zc_sub_tabs, value=t_zc_cfg).classes('w-full'):

            # ── ZeroClaw › Wizard ──────────────────────────────────────────
            with ui.tab_panel(t_zc_wiz):
                ui.label('🧙 Quick Setup Wizard').classes('text-h6 text-blue-9 q-mb-xs')
                ui.label(
                    'Configure an AI provider and a messaging channel in 3 steps. '
                    'Click Apply at the end — then restart ZeroClaw to activate.'
                ).classes('text-caption text-grey-6 q-mb-md')

                # ── Config source selector (same as Configuration tab) ─────
                with ui.row().classes('items-center gap-2 q-mb-sm'):
                    ui.label('Config source:').classes('text-caption text-grey-6')
                    _wiz_src_sel = ui.select(
                        {'runtime': 'Runtime  (/var/lib/zeroclaw/…)',
                         'local':   'Template (config/config.toml)'},
                        value=zc_source,
                    ).props('dense outlined').classes('text-caption').style('min-width: 240px')
                    def _wiz_reload_source():
                        new_src = _wiz_src_sel.value or 'local'
                        ui.navigate.to(f'/?lang={lang}&zc_source={new_src}')
                        _wiz_src_btn.props('loading')
                    _wiz_src_btn = ui.button('Load', icon='refresh', on_click=_wiz_reload_source
                        ).props('dense flat size=sm color=blue-7')

                with ui.stepper(value='wiz_provider').props('vertical animated').classes('w-full') as _wiz:

                    # ── Step 1: Provider ────────────────────────────────────
                    with ui.step('wiz_provider', title='1  Provider', icon='cloud'):
                        ui.label('Choose an AI provider and supply its API key.').classes('text-caption text-grey-6 q-mb-sm')

                        # ── Quick pick from provider_hints.json ────────────
                        _ph_map   = {h['model_name']: h for h in _ph_hints if h.get('model_name')}
                        _pid_base = _ph_pid_base  # reuse page-level dict

                        ui.label('⚡ Quick pick — fills fields automatically').classes('text-caption text-blue-7')
                        # Use a list (not dict) so e.value is the plain string key
                        wiz_quick = ui.select(
                            options=list(_ph_map.keys()),
                            label='Known model / provider',
                            value=None,
                            clearable=True,
                        ).classes('w-full q-mb-xs')
                        ui.separator().classes('q-my-xs')

                        wiz_prov_id = ui.select(
                            PROVIDER_IDS, label='Provider ID', value='openrouter',
                        ).classes('w-full q-mb-sm')
                        wiz_prov_alias = ui.input(
                            'Alias  (used as config key, e.g. "openrouter")',
                            value='openrouter').classes('w-full q-mb-sm')
                        wiz_prov_key = ui.input(
                            'API Key',
                            password=True, password_toggle_button=True).classes('w-full q-mb-sm')
                        wiz_prov_base = ui.input(
                            'base_url  (leave blank to use provider default)',
                            value='').classes('w-full q-mb-sm')
                        wiz_def_model = ui.input(
                            'default_model  (e.g. anthropic/claude-sonnet-4-6)',
                            value=str(top.get('default_model', ''))).classes('w-full')
                        wiz_is_fallback = ui.checkbox(
                            'Set as fallback provider  — used when the primary provider fails',
                            value=False)

                        # ── Callbacks defined AFTER all widgets so closures resolve ──
                        def _fill_from_hint(e):
                            """Fill provider fields from a quick-pick hint selection."""
                            picked = e.value
                            h = _ph_map.get(picked) if picked else None
                            if not h:
                                return
                            pid = h.get('provider_id', '') or h.get('model', '').split('/')[0]
                            if pid in PROVIDER_IDS:
                                # set provider_id — may cascade into _fill_from_pid,
                                # which writes prov_base; we overwrite it again below.
                                wiz_prov_id.set_value(pid)
                            suggested = h.get('suggested_alias', '')
                            if suggested:
                                wiz_prov_alias.set_value(suggested)
                            else:
                                slug = (h.get('model_name') or '').lower() \
                                    .replace(' ', '_').replace('-', '_').replace('.', '_') \
                                    .split('(')[0].rstrip('_')
                                if slug:
                                    wiz_prov_alias.set_value(slug)
                            if h.get('model'):
                                wiz_def_model.set_value(h['model'])
                            # Set base_url LAST so hint value wins over any cascade
                            wiz_prov_base.set_value(h.get('api_base', ''))

                        # Track last provider_id so _fill_from_pid can detect
                        # when the alias hasn't been customized (matches old pid)
                        _last_pid = ['openrouter']

                        def _fill_from_pid(e):
                            """Fill base_url from provider-ID selection.
                            Only auto-set alias when the current alias still
                            matches the previous provider-id — i.e. the user
                            hasn't customized it. This preserves regional
                            variants like minimax-cn set by the quick-pick."""
                            pid = e.value
                            if not pid:
                                return
                            wiz_prov_base.set_value(_pid_base.get(pid, ''))
                            cur_alias = (wiz_prov_alias.value or '').strip()
                            if not cur_alias or cur_alias == _last_pid[0]:
                                wiz_prov_alias.set_value(pid.split(':')[0])
                            _last_pid[0] = pid

                        # Register via .on_value_change() — callbacks are fully defined above
                        wiz_quick.on_value_change(_fill_from_hint)
                        wiz_prov_id.on_value_change(_fill_from_pid)

                        with ui.stepper_navigation():
                            ui.button('Next →', on_click=_wiz.next).props('color=blue-8')

                    # ── Step 2: Channel ─────────────────────────────────────
                    with ui.step('wiz_channel', title='2  Channel', icon='forum'):
                        ui.label('Pick a messaging channel to enable.').classes('text-caption text-grey-6 q-mb-sm')
                        wiz_ch_sel = ui.select(
                            {k: v['label'] for k, v in CHANNEL_SCHEMAS.items()},
                            label='Channel type',
                        ).classes('w-full q-mb-sm')
                        wiz_ch_container = ui.column().classes('w-full')
                        _wiz_ch_widgets: dict = {}

                        def _wiz_build_ch(e=None):
                            wiz_ch_container.clear()
                            _wiz_ch_widgets.clear()
                            key = wiz_ch_sel.value
                            schema = CHANNEL_SCHEMAS.get(key)
                            if not schema:
                                return
                            with wiz_ch_container:
                                for (fkey, flabel, ftype, fdefault) in schema['fields']:
                                    if ftype == 'text':
                                        _wiz_ch_widgets[fkey] = ui.input(flabel, value=str(fdefault)).classes('w-full')
                                    elif ftype == 'password':
                                        _wiz_ch_widgets[fkey] = ui.input(
                                            flabel, value='',
                                            password=True, password_toggle_button=True,
                                        ).classes('w-full')
                                    elif ftype == 'bool':
                                        _wiz_ch_widgets[fkey] = ui.checkbox(flabel, value=bool(fdefault))
                                    elif ftype == 'int':
                                        _wiz_ch_widgets[fkey] = ui.number(flabel, value=to_int(fdefault, 0), step=1).classes('w-full')
                                    elif ftype == 'textarea':
                                        ui.label(flabel).classes('text-caption text-grey-6')
                                        _wiz_ch_widgets[fkey] = ui.textarea(
                                            value='\n'.join(fdefault) if isinstance(fdefault, list) else str(fdefault)
                                        ).classes('w-full').props('outlined rows=3')
                                    elif ftype.startswith('select:'):
                                        opts = ftype.split(':', 1)[1].split(',')
                                        _wiz_ch_widgets[fkey] = ui.select(opts, label=flabel, value=opts[0]).classes('w-full')

                        wiz_ch_sel.on('update:model-value', _wiz_build_ch)

                        def _wiz_refresh_summary():
                            key = str(wiz_prov_key.value)
                            masked = ('*' * 6 + key[-4:]) if len(key) >= 6 else ('*' * len(key))
                            ch_label = CHANNEL_SCHEMAS.get(wiz_ch_sel.value or '', {}).get('label', '(none selected)')
                            lines = [
                                _diag,
                                '',
                                f'Provider:      {wiz_prov_id.value}',
                                f'Alias:         {wiz_prov_alias.value or "default"}',
                                f'API Key:       {masked if key else "(not set)"}',
                                f'base_url:      {wiz_prov_base.value or "(provider default)"}',
                                f'default_model: {wiz_def_model.value or "(unchanged)"}',
                                f'Fallback:      {"✓ YES" if wiz_is_fallback.value else "—"}',
                                f'Channel:       {ch_label}',
                                f'Web Search:    {"on" if wiz_tool_web_search.value else "off"}',
                                f'Web Fetch:     {"on" if wiz_tool_web_fetch.value else "off"}',
                                f'HTTP Request:  {"on" if wiz_tool_http_req.value else "off"}',
                                f'Browser:       {"on" if wiz_tool_browser.value else "off"}',
                                f'workspace_only:                  {wiz_sec_workspace_only.value}',
                                f'req_approval_medium_risk:        {wiz_sec_req_approval.value}',
                                f'block_high_risk_commands:        {wiz_sec_block_high.value}',
                            ]
                            wiz_summary.set_text('\n'.join(lines))

                        with ui.stepper_navigation():
                            ui.button('← Back', on_click=_wiz.previous).props('flat color=grey-7')
                            ui.button('Next →', on_click=_wiz.next).props('color=blue-8')

                    # ── Step 3: Tools & Security ─────────────────────────────
                    with ui.step('wiz_tools_sec', title='3  Tools & Security', icon='security'):
                        ui.label('Enable tools and configure security permissions.').classes('text-caption text-grey-6 q-mb-sm')

                        ui.label('🔧 Tools').classes('text-subtitle2 text-blue-8 q-mb-xs')
                        wiz_tool_web_search = ui.checkbox('Web Search', value=True)
                        wiz_tool_web_fetch  = ui.checkbox('Web Fetch', value=True)
                        wiz_tool_http_req   = ui.checkbox('HTTP Request', value=False)
                        wiz_tool_browser    = ui.checkbox('Browser', value=False)

                        ui.separator().classes('q-my-sm')
                        ui.label('🔒 Security').classes('text-subtitle2 text-blue-8 q-mb-xs')
                        wiz_sec_workspace_only = ui.checkbox('workspace_only  — restrict agent to workspace directory', value=True)
                        wiz_sec_req_approval   = ui.checkbox('require_approval_for_medium_risk', value=True)
                        wiz_sec_block_high     = ui.checkbox('block_high_risk_commands', value=True)

                        with ui.stepper_navigation():
                            ui.button('← Back', on_click=_wiz.previous).props('flat color=grey-7')
                            ui.button('Next →',
                                on_click=lambda: (_wiz_refresh_summary(), _wiz.next())
                            ).props('color=blue-8')

                    # ── Step 4: Apply ───────────────────────────────────────
                    with ui.step('wiz_apply', title='4  Apply', icon='check_circle'):
                        ui.label(
                            'Review the summary below, then click Apply & Save.'
                        ).classes('text-caption text-grey-6 q-mb-sm')
                        wiz_summary = ui.label('…').classes(
                            'text-caption text-mono bg-grey-2 q-pa-sm w-full q-mb-sm'
                        ).style('white-space: pre; border-radius: 4px;')
                        wiz_result_lbl = ui.label('').classes('text-caption w-full q-mb-xs')

                        def _wiz_apply():
                            nonlocal _diag
                            alias = (wiz_prov_alias.value or 'default').strip()
                            # ── provider
                            prov_entry: dict = {
                                'name': wiz_prov_id.value,
                                'requires_openai_auth': False,
                                'model': (wiz_def_model.value.strip() or ''),
                                'temperature': 0.7,
                                'timeout_secs': 120,
                            }
                            if wiz_prov_key.value:
                                prov_entry['api_key'] = wiz_prov_key.value
                            if wiz_prov_base.value: prov_entry['uri'] = wiz_prov_base.value
                            conf.setdefault('providers', {}).setdefault('models', {})
                            # Diagnostic: capture state before cleanup
                            _before_keys = list(conf['providers']['models'].keys())
                            # Remove any stale entries that share the same provider name
                            # (e.g. leftover "minimax" or "custom:https://..." aliases from
                            # previous buggy wizard runs) so only the current alias stays.
                            # tomlkit tables are MutableMapping, NOT dict — use hasattr, not isinstance.
                            pname = wiz_prov_id.value
                            for _k in list(conf['providers']['models'].keys()):
                                _v = conf['providers']['models'].get(_k)
                                if hasattr(_v, 'get') and _v.get('name') == pname and _k != alias:
                                    del conf['providers']['models'][_k]
                            conf['providers']['models'][alias] = prov_entry
                            # Diagnostic: capture state after cleanup
                            _after_keys = list(conf['providers']['models'].keys())
                            _loaded_src = _loaded_from or 'unknown'
                            _diag = (
                                f'📋 Loaded from: {_loaded_src}\n'
                                f'   Before cleanup: {sorted(_before_keys)}\n'
                                f'   After cleanup:  {sorted(_after_keys)}\n'
                                f'   Will save to:   {CONFIG_PATH}\n'
                                f'   Will deploy to: {DEPLOY_CONFIG_PATH}'
                            )
                            _wiz_refresh_summary()
                            # ── fallback
                            if wiz_is_fallback.value:
                                conf.setdefault('providers', {})['fallback'] = alias
                            # ── channel
                            ch_key = wiz_ch_sel.value
                            if ch_key and ch_key in CHANNEL_SCHEMAS:
                                ch_entry: dict = {'enabled': True}
                                for (fkey, _fl, ftype, _fd) in CHANNEL_SCHEMAS[ch_key]['fields']:
                                    w = _wiz_ch_widgets.get(fkey)
                                    if w is None: continue
                                    if ftype == 'textarea': ch_entry[fkey] = lines_to_list(w.value)
                                    elif ftype == 'bool':   ch_entry[fkey] = w.value
                                    elif ftype == 'int':    ch_entry[fkey] = to_int(w.value)
                                    else:                   ch_entry[fkey] = w.value
                                conf.setdefault('channels', {})[ch_key] = ch_entry
                            # ── tools
                            conf.setdefault('web_search', {})['enabled'] = wiz_tool_web_search.value
                            conf.setdefault('web_fetch', {})['enabled'] = wiz_tool_web_fetch.value
                            conf.setdefault('http_request', {})['enabled'] = wiz_tool_http_req.value
                            conf.setdefault('browser', {})['enabled'] = wiz_tool_browser.value
                            # ── security (autonomy)
                            conf.setdefault('autonomy', {})['workspace_only'] = wiz_sec_workspace_only.value
                            conf.setdefault('autonomy', {})['require_approval_for_medium_risk'] = wiz_sec_req_approval.value
                            conf.setdefault('autonomy', {})['block_high_risk_commands'] = wiz_sec_block_high.value
                            try:
                                save_config(conf)
                            except Exception as _e:
                                msg = f'❌ Save failed: {_e}'
                                ui.notify(msg, type='negative')
                                wiz_result_lbl.set_text(msg)
                                wiz_result_lbl.classes(remove='text-positive text-warning', add='text-negative')
                                return
                            ok_deploy, deploy_err = deploy_config(conf)
                            if not ok_deploy:
                                msg = f'⚠️ Saved locally but deploy failed: {deploy_err}\n{_diag}'
                                ui.notify(msg, type='warning')
                                wiz_result_lbl.set_text(msg)
                                wiz_result_lbl.classes(remove='text-positive text-negative', add='text-warning')
                                return
                            msg = f'✅ Wizard applied & deployed successfully\n{_diag}'
                            ui.notify(msg, type='positive')
                            wiz_result_lbl.set_text(msg)
                            wiz_result_lbl.classes(remove='text-warning text-negative', add='text-positive')
                            # Reload the page after a short delay to reflect the
                            # deployed config from disk (not the old in-memory conf)
                            ui.timer(2.0, lambda: ui.navigate.to(
                                f'/?lang={lang}&zc_source={zc_source}'))

                        with ui.stepper_navigation():
                            ui.button('← Back', on_click=_wiz.previous).props('flat color=grey-7')
                            ui.button('✅ Apply & Deploy', on_click=_wiz_apply).props('color=green-8')

            # ── ZeroClaw › Configuration ───────────────────────────────────
            with ui.tab_panel(t_zc_cfg):

                # ── Header row: schema badge + config source selector ──────
                _zc_schema_ver = int(conf.get('schema_version', 0))
                with ui.row().classes('items-center gap-3 q-px-sm q-pt-xs q-pb-none'):
                    _zc_badge_color = 'green-7' if _zc_schema_ver >= 2 else 'orange-7'
                    ui.badge(f'schema v{_zc_schema_ver}', color=_zc_badge_color).props('outline')
                    ui.space()
                    ui.label('Config source:').classes('text-caption text-grey-6')
                    _zc_src_sel = ui.select(
                        {'runtime': 'Runtime  (/var/lib/zeroclaw/…)',
                         'local':   'Template (config/config.toml)'},
                        value=zc_source,
                    ).props('dense outlined').classes('text-caption').style('min-width: 240px')
                    def _zc_reload_source():
                        new_src = _zc_src_sel.value or 'local'
                        ui.navigate.to(f'/?lang={lang}&zc_source={new_src}')
                    ui.button('Load', icon='refresh', on_click=_zc_reload_source
                        ).props('dense flat size=sm color=blue-7')

                # ── Upgrade banner (shown when schema is below current version) ──
                if _zc_schema_ver < 2:
                    with ui.card().classes('w-full q-pa-sm q-mb-sm bg-orange-1'):
                        with ui.row().classes('items-center gap-2'):
                            ui.icon('warning', color='orange-8').classes('text-h6')
                            ui.label(
                                f'Config schema is version {_zc_schema_ver} — ZeroClaw requires version 2. '
                                'Click Migrate to run the built-in migration as the zeroclaw account.'
                            ).classes('text-caption text-orange-9')
                        _zc_upg_log = ui.textarea('').classes('w-full text-caption font-mono').props(
                            'outlined readonly rows=6 label="Migration output"')
                        _zc_upg_log.set_visibility(False)

                        async def _run_zc_cfg_migrate():
                            import asyncio as _aio
                            _zc_upg_log.set_visibility(True)
                            _zc_upg_log.set_value('⏳ Running zeroclaw config migrate…\n')
                            _zc_upg_btn.props('disabled loading')
                            try:
                                proc = await _aio.create_subprocess_exec(
                                    'sudo', '-u', 'zeroclaw', '/opt/zeroclaw/zeroclaw', 'config', 'migrate',
                                    stdout=_aio.subprocess.PIPE,
                                    stderr=_aio.subprocess.STDOUT,
                                )
                                buf = '⏳ Running zeroclaw config migrate…\n'
                                async for line in proc.stdout:
                                    buf += line.decode(errors='replace')
                                    _zc_upg_log.set_value(buf)
                                await proc.wait()
                                if proc.returncode == 0:
                                    _zc_upg_log.set_value(buf + '\n✅ Migration complete.')
                                    ui.notify('✅ Schema migrated — reloading page…', type='positive')
                                    ui.navigate.reload()
                                else:
                                    _zc_upg_log.set_value(buf + '\n⚠️ Migration exited with error.')
                                    ui.notify('⚠️ Migration returned an error', type='warning')
                            except Exception as _ex:
                                _zc_upg_log.set_value(f'❌ {_ex}')
                                ui.notify(f'❌ {_ex}', type='negative')
                            finally:
                                _zc_upg_btn.props(remove='disabled loading')

                        _zc_upg_btn = ui.button(
                            'Migrate schema to v2', icon='upgrade',
                            on_click=_run_zc_cfg_migrate,
                        ).props('color=orange-8 size=sm')

                with ui.tabs().classes('w-full bg-blue-1') as tabs:
                    t_gen   = ui.tab(T['tab_general'],   icon='tune')
                    t_prov  = ui.tab(T['tab_providers'],  icon='cloud')
                    t_auto  = ui.tab(T['tab_autonomy'],   icon='psychology')
                    t_agent = ui.tab(T['tab_agent'],      icon='smart_toy')
                    t_mem   = ui.tab(T['tab_memory'],     icon='memory')
                    t_comm  = ui.tab(T['tab_comms'],      icon='hub')
                    t_ch    = ui.tab(T['tab_channels'],   icon='forum')
                    t_sec   = ui.tab(T['tab_security'],   icon='security')
                    t_feat  = ui.tab(T['tab_features'],   icon='extension')
                    t_sys   = ui.tab(T['tab_system'],     icon='computer')

                with ui.tab_panels(tabs, value=t_gen).classes('w-full'):

                    # ══ General ══════════════════════════════════════════════
                    with ui.tab_panel(t_gen):
                        ui.label(T['section_api']).classes('text-subtitle2 text-grey-7 q-mt-sm')
                        w_api_key = ui.input(T['lbl_api_key'], value=str(_default_prov.get('api_key', '') or top.get('api_key', '')),
                            password=True, password_toggle_button=True).classes('w-full')
                        cur_prov = str(_default_prov.get('name', '') or top.get('default_provider', 'dashscope'))
                        _eff_prov = cur_prov if cur_prov in PROVIDER_IDS else PROVIDER_IDS[0]
                        w_default_provider = ui.select(PROVIDER_IDS, label='default_provider',
                            value=_eff_prov).classes('w-full')
                        _cur_def_model = str(_default_prov.get('model', '') or top.get('default_model', 'anthropic/claude-sonnet-4-6'))
                        # Seed model list from the currently selected provider
                        _init_models = _ph_pid_models.get(_eff_prov, _ph_models)
                        _dm_opts = list(_init_models) if _cur_def_model in _init_models \
                            else ([_cur_def_model] + list(_init_models))
                        w_default_model = ui.select(
                            options=_dm_opts,
                            label='default_model',
                            value=_cur_def_model,
                            with_input=True,
                            new_value_mode='add-unique',
                        ).classes('w-full')

                        # Re-populate model list when provider changes
                        def _on_prov_change(e):
                            pid    = e.value or ''
                            models = _ph_pid_models.get(pid, _ph_models)
                            cur    = w_default_model.value
                            new_val = cur if cur in models else (models[0] if models else cur)
                            w_default_model.set_options(list(models), value=new_val)
                        w_default_provider.on_value_change(_on_prov_change)
                        w_temperature = ui.number('default_temperature',
                            value=_default_prov.get('temperature', top.get('default_temperature', 0.7)), min=0.0, max=2.0, step=0.1).classes('w-full')
                        w_prov_timeout = ui.number('provider_timeout_secs',
                            value=_default_prov.get('timeout_secs', top.get('provider_timeout_secs', 120)), min=5, step=5).classes('w-full')
                        ui.separator().classes('q-my-sm')
                        ui.label(T['section_secrets']).classes('text-subtitle2 text-grey-7')
                        w_secrets_encrypt = ui.checkbox('secrets.encrypt', value=bool(secrets_c.get('encrypt', True)))
                        cur_id = str(identity.get('format', 'openclaw'))
                        w_identity_format = ui.select(['openclaw', 'aieos'], label='identity.format',
                            value=cur_id if cur_id in ['openclaw','aieos'] else 'openclaw').classes('w-full')

                    # ══ Providers ════════════════════════════════════════════
                    with ui.tab_panel(t_prov):
                        ui.label(T['section_providers']).classes('text-subtitle2 text-grey-7 q-mt-sm')
                        ui.label(T['hint_providers']).classes('text-caption text-grey-5')
                        provider_container = ui.column().classes('w-full')
                        for alias, mp_data in conf.get('providers', {}).get('models', {}).items():
                            build_provider_card(provider_container, alias, mp_data)
                        ui.separator().classes('q-my-sm')
                        ui.label(T['lbl_add_provider']).classes('text-caption text-blue-7')
                        # unique provider ids from hints, preserving first-seen order
                        _seen_pids: list[str] = []
                        for _hh in _ph_hints:
                            _pid = _hh.get('provider_id', '')
                            if _pid and _pid not in _seen_pids:
                                _seen_pids.append(_pid)
                        new_prov_sel = ui.select(
                            options=_seen_pids,
                            label='Provider  (pick to auto-fill)',
                            value=None,
                            clearable=True,
                            with_input=True,
                        ).classes('w-full q-mb-xs')
                        with ui.row().classes('w-full gap-2 items-center'):
                            new_alias_input = ui.input(
                                'Alias  [providers.models.<alias>]  — auto-filled, edit for duplicates',
                                value='',
                            ).classes('flex-1')
                            ui.button(T['btn_add_provider'], on_click=lambda: _add_provider()).props('outline color=blue')

                        def _on_prov_sel(e):
                            pid = e.value or ''
                            # Only auto-fill alias if user hasn't manually changed it away from a known pid
                            if pid:
                                new_alias_input.set_value(pid)
                        new_prov_sel.on_value_change(_on_prov_sel)

                        def _add_provider():
                            alias = new_alias_input.value.strip()
                            if not alias:
                                ui.notify(T['warn_alias_empty'], type='warning'); return
                            if alias in provider_panels:
                                ui.notify(T['warn_alias_exists'].format(alias), type='warning'); return
                            pid = new_prov_sel.value or alias
                            pre = {
                                'name': pid,
                                'uri':  _ph_pid_base.get(pid, ''),
                            }
                            build_provider_card(provider_container, alias, pre)
                            new_alias_input.set_value('')
                            new_prov_sel.set_value(None)

                    # ══ Autonomy ═════════════════════════════════════════════
                    with ui.tab_panel(t_auto):
                        ui.label(T['section_autonomy']).classes('text-subtitle2 text-grey-7 q-mt-sm')
                        cur_lvl = autonomy.get('level', 'supervised')
                        w_auto_level = ui.select(['read_only', 'supervised', 'full'], label='autonomy.level',
                            value=cur_lvl if cur_lvl in ['read_only','supervised','full'] else 'supervised').classes('w-full')
                        w_auto_workspace        = ui.checkbox('workspace_only',                   value=autonomy.get('workspace_only', True))
                        w_auto_require_approval = ui.checkbox('require_approval_for_medium_risk', value=autonomy.get('require_approval_for_medium_risk', True))
                        w_auto_block_high       = ui.checkbox('block_high_risk_commands',          value=autonomy.get('block_high_risk_commands', True))
                        ui.separator().classes('q-my-sm')
                        w_auto_max_actions = ui.number('max_actions_per_hour',  value=autonomy.get('max_actions_per_hour', 20),   min=1,  step=1).classes('w-full')
                        w_auto_max_cost    = ui.number('max_cost_per_day_cents', value=autonomy.get('max_cost_per_day_cents', 500), min=0,  step=10).classes('w-full')
                        ui.separator().classes('q-my-sm')
                        ui.label(T['lbl_allowed_commands']).classes('text-caption text-grey-6')
                        w_auto_cmds = ui.textarea(value='\n'.join(autonomy.get('allowed_commands', []))).classes('w-full').props('outlined rows=5')
                        ui.label(T['lbl_auto_approve']).classes('text-caption text-grey-6')
                        w_auto_approve = ui.textarea(value='\n'.join(autonomy.get('auto_approve', []))).classes('w-full').props('outlined rows=3')
                        ui.label(T['lbl_always_ask']).classes('text-caption text-grey-6')
                        w_auto_always_ask = ui.textarea(value='\n'.join(autonomy.get('always_ask', []))).classes('w-full').props('outlined rows=3')
                        ui.label(T['lbl_forbidden_paths']).classes('text-caption text-grey-6')
                        w_auto_forbidden = ui.textarea(value='\n'.join(autonomy.get('forbidden_paths', []))).classes('w-full').props('outlined rows=5')
                        ui.label(T['lbl_allowed_roots']).classes('text-caption text-grey-6')
                        w_auto_allowed_roots = ui.textarea(value='\n'.join(autonomy.get('allowed_roots', []))).classes('w-full').props('outlined rows=3')
                        ui.label(T['lbl_shell_env']).classes('text-caption text-grey-6')
                        w_auto_shell_env = ui.textarea(value='\n'.join(autonomy.get('shell_env_passthrough', []))).classes('w-full').props('outlined rows=3')

                    # ══ Agent ════════════════════════════════════════════════
                    with ui.tab_panel(t_agent):
                        ui.label(T['section_agent']).classes('text-subtitle2 text-grey-7 q-mt-sm')
                        w_agent_compact  = ui.checkbox('compact_context', value=agent_c.get('compact_context', False))
                        w_agent_parallel = ui.checkbox('parallel_tools',  value=agent_c.get('parallel_tools', False))
                        w_agent_max_iter = ui.number('max_tool_iterations',  value=agent_c.get('max_tool_iterations', 10),  min=1, step=1).classes('w-full')
                        w_agent_max_hist = ui.number('max_history_messages', value=agent_c.get('max_history_messages', 50), min=1, step=5).classes('w-full')
                        cur_disp = agent_c.get('tool_dispatcher', 'auto')
                        w_agent_tool_dispatcher = ui.select(['auto', 'sequential', 'parallel'], label='tool_dispatcher',
                            value=cur_disp if cur_disp in ['auto','sequential','parallel'] else 'auto').classes('w-full')
                        ui.separator().classes('q-my-sm')
                        ui.label(T['section_obs']).classes('text-subtitle2 text-grey-7')
                        cur_obs = obs.get('backend', 'none')
                        w_obs_backend = ui.select(['none', 'noop', 'log', 'prometheus', 'otel'], label='backend',
                            value=cur_obs if cur_obs in ['none','noop','log','prometheus','otel'] else 'none').classes('w-full')
                        cur_tm = obs.get('runtime_trace_mode', obs.get('log_persistence', 'none'))
                        w_obs_trace_mode = ui.select(['none', 'rolling', 'full'], label='runtime_trace_mode',
                            value=cur_tm if cur_tm in ['none','rolling','full'] else 'none').classes('w-full')
                        w_obs_otel_endpoint = ui.input('otel_endpoint', value=str(obs.get('otel_endpoint', 'http://localhost:4318'))).classes('w-full')
                        w_obs_otel_service  = ui.input('otel_service_name', value=str(obs.get('otel_service_name', 'zeroclaw'))).classes('w-full')
                        w_obs_trace_path    = ui.input('runtime_trace_path', value=str(obs.get('runtime_trace_path', obs.get('log_persistence_path', 'state/runtime-trace.jsonl')))).classes('w-full')
                        w_obs_trace_max     = ui.number('runtime_trace_max_entries', value=obs.get('runtime_trace_max_entries', obs.get('log_persistence_max_entries', 200)), min=10, step=50).classes('w-full')
                        ui.separator().classes('q-my-sm')
                        ui.label(T['section_skills']).classes('text-subtitle2 text-grey-7')
                        w_skills_open = ui.checkbox('open_skills_enabled', value=skills.get('open_skills_enabled', False))
                        w_skills_allow_scripts = ui.checkbox('allow_scripts', value=skills.get('allow_scripts', True))
                        cur_pm = skills.get('prompt_injection_mode', 'full')
                        w_skills_mode = ui.select(['full', 'compact'], label='prompt_injection_mode',
                            value=cur_pm if cur_pm in ['full','compact'] else 'full').classes('w-full')
                        ui.separator().classes('q-my-xs')
                        ui.label('skill_creation').classes('text-caption text-blue-7 text-bold')
                        _sk_cr = skills.get('skill_creation', {})
                        w_sk_cr_enabled = ui.checkbox('skill_creation.enabled', value=_sk_cr.get('enabled', False))
                        w_sk_cr_max = ui.number('skill_creation.max_skills', value=_sk_cr.get('max_skills', 500), min=1, step=10).classes('w-full')
                        w_sk_cr_sim = ui.number('skill_creation.similarity_threshold', value=_sk_cr.get('similarity_threshold', 0.85), min=0.0, max=1.0, step=0.01).classes('w-full')
                        ui.label('skill_improvement').classes('text-caption text-blue-7 text-bold')
                        _sk_im = skills.get('skill_improvement', {})
                        w_sk_im_enabled = ui.checkbox('skill_improvement.enabled', value=_sk_im.get('enabled', True))
                        w_sk_im_cooldown = ui.number('skill_improvement.cooldown_secs', value=_sk_im.get('cooldown_secs', 3600), min=60, step=300).classes('w-full')

                    # ══ Memory ═══════════════════════════════════════════════
                    with ui.tab_panel(t_mem):
                        ui.label(T['section_storage']).classes('text-subtitle2 text-grey-7 q-mt-sm')
                        cur_mb = memory.get('backend', 'sqlite')
                        w_mem_backend = ui.select(['sqlite', 'lucid', 'markdown', 'none'], label='backend',
                            value=cur_mb if cur_mb in ['sqlite','lucid','markdown','none'] else 'sqlite').classes('w-full')
                        cur_sm = str(memory.get('search_mode', 'hybrid'))
                        w_mem_search_mode = ui.select(['hybrid', 'fts', 'vector', 'cache'], label='search_mode',
                            value=cur_sm if cur_sm in ['hybrid','fts','vector','cache'] else 'hybrid').classes('w-full')
                        w_mem_auto_save     = ui.checkbox('auto_save',       value=memory.get('auto_save', True))
                        w_mem_hygiene       = ui.checkbox('hygiene_enabled', value=memory.get('hygiene_enabled', True))
                        w_mem_auto_hydrate  = ui.checkbox('auto_hydrate',    value=memory.get('auto_hydrate', True))
                        w_mem_archive_days  = ui.number('archive_after_days',          value=memory.get('archive_after_days', 7),   min=1, step=1).classes('w-full')
                        w_mem_purge_days    = ui.number('purge_after_days',            value=memory.get('purge_after_days', 30),    min=1, step=1).classes('w-full')
                        w_mem_conv_retention= ui.number('conversation_retention_days', value=memory.get('conversation_retention_days', 30), min=1, step=1).classes('w-full')
                        ui.separator().classes('q-my-sm')
                        ui.label(T['section_embedding']).classes('text-subtitle2 text-grey-7')
                        cur_ep = memory.get('embedding_provider', 'none')
                        w_mem_embed_provider = ui.select(['none', 'openai', 'custom:<url>'], label='embedding_provider',
                            value=cur_ep if cur_ep in ['none','openai','custom:<url>'] else 'none').classes('w-full')
                        w_mem_embed_model  = ui.input('embedding_model',   value=str(memory.get('embedding_model', 'text-embedding-3-small'))).classes('w-full')
                        w_mem_embed_dims   = ui.number('embedding_dimensions', value=memory.get('embedding_dimensions', 1536),   min=64,  step=128).classes('w-full')
                        w_mem_vec_weight   = ui.number('vector_weight',        value=memory.get('vector_weight', 0.7),           min=0.0, max=1.0, step=0.05).classes('w-full')
                        w_mem_kw_weight    = ui.number('keyword_weight',       value=memory.get('keyword_weight', 0.3),          min=0.0, max=1.0, step=0.05).classes('w-full')
                        w_mem_min_relevance= ui.number('min_relevance_score',  value=memory.get('min_relevance_score', 0.4),     min=0.0, max=1.0, step=0.05).classes('w-full')
                        w_mem_cache_size   = ui.number('embedding_cache_size', value=memory.get('embedding_cache_size', 10000),  min=0,   step=1000).classes('w-full')
                        w_mem_chunk_tokens = ui.number('chunk_max_tokens',     value=memory.get('chunk_max_tokens', 512),        min=64,  step=64).classes('w-full')
                        ui.separator().classes('q-my-sm')
                        ui.label(T['section_cache']).classes('text-subtitle2 text-grey-7')
                        w_mem_resp_cache   = ui.checkbox('response_cache_enabled', value=memory.get('response_cache_enabled', False))
                        w_mem_snapshot     = ui.checkbox('snapshot_enabled',       value=memory.get('snapshot_enabled', False))
                        w_mem_snap_hygiene = ui.checkbox('snapshot_on_hygiene',    value=memory.get('snapshot_on_hygiene', False))
                        w_mem_resp_ttl     = ui.number('response_cache_ttl_minutes',  value=memory.get('response_cache_ttl_minutes', 60),   min=1, step=5).classes('w-full')
                        w_mem_resp_max     = ui.number('response_cache_max_entries',  value=memory.get('response_cache_max_entries', 5000), min=0, step=500).classes('w-full')

                    # ══ Comms ════════════════════════════════════════════════
                    with ui.tab_panel(t_comm):
                        ui.label(T['section_gateway']).classes('text-subtitle2 text-grey-7 q-mt-sm')
                        w_gw_port    = ui.number('port', value=gateway.get('port', 42617), min=1024, max=65535, step=1).classes('w-full')
                        w_gw_host    = ui.input('host',  value=str(gateway.get('host', '127.0.0.1'))).classes('w-full')
                        w_gw_pairing = ui.checkbox('require_pairing',   value=gateway.get('require_pairing', True))
                        w_gw_public  = ui.checkbox('allow_public_bind', value=gateway.get('allow_public_bind', False))
                        ui.separator().classes('q-my-sm')
                        ui.label(T['section_tunnel']).classes('text-subtitle2 text-grey-7')
                        cur_tn = tunnel.get('provider', tunnel.get('tunnel_provider', 'none'))
                        _tunnel_backends = ['none', 'cloudflare', 'tailscale', 'ngrok', 'openvpn', 'pinggy', 'custom']
                        w_tunnel = ui.select(_tunnel_backends, label='tunnel.provider',
                            value=cur_tn if cur_tn in _tunnel_backends else 'none').classes('w-full')
                        ui.separator().classes('q-my-sm')
                        ui.label(T['section_channels_global']).classes('text-subtitle2 text-grey-7')
                        w_cli_enabled = ui.checkbox(T['lbl_cli'], value=ch_conf_top.get('cli', True))
                        w_msg_timeout = ui.number('message_timeout_secs', value=ch_conf_top.get('message_timeout_secs', 300), min=30, step=30).classes('w-full')
                        w_ch_ack_reactions    = ui.checkbox('ack_reactions',       value=bool(ch_conf_top.get('ack_reactions', True)))
                        w_ch_show_tool_calls  = ui.checkbox('show_tool_calls',     value=bool(ch_conf_top.get('show_tool_calls', False)))
                        w_ch_session_persist  = ui.checkbox('session_persistence', value=bool(ch_conf_top.get('session_persistence', True)))
                        cur_sb = str(ch_conf_top.get('session_backend', 'sqlite'))
                        w_ch_session_backend  = ui.select(['sqlite', 'memory', 'none'], label='session_backend',
                            value=cur_sb if cur_sb in ['sqlite','memory','none'] else 'sqlite').classes('w-full')
                        w_ch_session_ttl      = ui.number('session_ttl_hours',  value=ch_conf_top.get('session_ttl_hours', 0),  min=0, step=1).classes('w-full')
                        w_ch_debounce_ms      = ui.number('debounce_ms',         value=ch_conf_top.get('debounce_ms', 0),        min=0, step=50).classes('w-full')

                    # ══ Channels ═════════════════════════════════════════════
                    with ui.tab_panel(t_ch):
                        ui.label(T['section_channels']).classes('text-subtitle2 text-grey-7 q-mt-sm')
                        ui.label(T['hint_channels']).classes('text-caption text-grey-5')
                        channel_container = ui.column().classes('w-full')
                        for ch_key in CHANNEL_KEYS:
                            if ch_key in ch_conf_top and isinstance(ch_conf_top[ch_key], dict):
                                build_channel_card(channel_container, ch_key, ch_conf_top[ch_key])
                        ui.separator().classes('q-my-sm')
                        with ui.row().classes('w-full gap-2 items-end'):
                            new_ch_select = ui.select({k: v for k, v in CHANNEL_LABELS.items()},
                                label=T['lbl_channel_type']).classes('flex-1')
                            def _add_channel():
                                ch_key = new_ch_select.value
                                if not ch_key: ui.notify(T['warn_channel_empty'], type='warning'); return
                                if ch_key in channel_panels:
                                    ui.notify(T['warn_channel_exists'].format(CHANNEL_LABELS.get(ch_key, ch_key)), type='warning'); return
                                build_channel_card(channel_container, ch_key, {})
                            ui.button(T['btn_add_channel'], on_click=_add_channel).props('outline color=green')

                    # ══ Security ═════════════════════════════════════════════
                    with ui.tab_panel(t_sec):
                        with ui.expansion(T['exp_dashboard_access'], icon='vpn_key').classes('w-full'):
                            ui.label(T['lbl_change_password']).classes('text-subtitle2 q-mt-xs')
                            w_cur_pw  = ui.input(T['lbl_cur_pw'],    password=True, password_toggle_button=True).classes('w-full')
                            w_new_pw  = ui.input(T['lbl_new_pw'],    password=True, password_toggle_button=True).classes('w-full')
                            w_new_pw2 = ui.input(T['lbl_confirm_pw'], password=True, password_toggle_button=True).classes('w-full')
                            def do_change_pw():
                                d = _load_auth()
                                if not d or not _verify_pw(w_cur_pw.value, d['password_hash']):
                                    ui.notify(T['notify_pw_wrong'], type='negative'); return
                                if len(w_new_pw.value) < 6:
                                    ui.notify(T['notify_pw_short'], type='warning'); return
                                if w_new_pw.value != w_new_pw2.value:
                                    ui.notify(T['notify_pw_mismatch'], type='warning'); return
                                d['password_hash'] = _hash_pw(w_new_pw.value)
                                _save_auth(d)
                                ui.notify(T['notify_pw_changed'], type='positive')
                                w_cur_pw.set_value(''); w_new_pw.set_value(''); w_new_pw2.set_value('')
                            ui.button(T['btn_change_pw'], on_click=do_change_pw).props('outline color=blue').classes('q-mb-sm')
                        with ui.expansion(T['exp_resources'], icon='memory').classes('w-full'):
                            w_sec_mem         = ui.number('max_memory_mb',        value=sec_res.get('max_memory_mb', 512),        min=64,  step=64).classes('w-full')
                            w_sec_cpu         = ui.number('max_cpu_time_seconds', value=sec_res.get('max_cpu_time_seconds', 60),  min=5,   step=5).classes('w-full')
                            w_sec_procs       = ui.number('max_subprocesses',     value=sec_res.get('max_subprocesses', 10),      min=1,   step=1).classes('w-full')
                            w_sec_mem_monitor = ui.checkbox('memory_monitoring',  value=bool(sec_res.get('memory_monitoring', True)))
                        with ui.expansion(T['exp_sandbox'], icon='shield').classes('w-full'):
                            cur_sb = sec_sandbox.get('backend', 'auto')
                            w_sec_sandbox = ui.select(['auto', 'firejail', 'none'], label='sandbox.backend',
                                value=cur_sb if cur_sb in ['auto','firejail','none'] else 'auto').classes('w-full')
                        with ui.expansion(T['exp_audit'], icon='fact_check').classes('w-full'):
                            w_sec_audit_enabled  = ui.checkbox('enabled',     value=bool(sec_audit.get('enabled', True)))
                            w_sec_audit_log_path = ui.input('log_path',       value=str(sec_audit.get('log_path', 'audit.log'))).classes('w-full')
                            w_sec_audit_max      = ui.number('max_size_mb',   value=sec_audit.get('max_size_mb', 100), min=1, step=10).classes('w-full')
                            w_sec_audit_sign     = ui.checkbox('sign_events', value=bool(sec_audit.get('sign_events', False)))
                        with ui.expansion(T['exp_otp'], icon='lock').classes('w-full'):
                            w_sec_otp_enabled = ui.checkbox('enabled', value=bool(sec_otp.get('enabled', False)))
                            cur_om = sec_otp.get('method', 'totp')
                            w_sec_otp_method  = ui.select(['totp', 'pairing', 'cli-prompt'], label='method',
                                value=cur_om if cur_om in ['totp','pairing','cli-prompt'] else 'totp').classes('w-full')
                            w_sec_otp_ttl     = ui.number('token_ttl_secs',   value=sec_otp.get('token_ttl_secs', 30),    min=10, step=5).classes('w-full')
                            w_sec_otp_cache   = ui.number('cache_valid_secs', value=sec_otp.get('cache_valid_secs', 300), min=30, step=30).classes('w-full')
                            ui.label(T['lbl_otp_actions']).classes('text-caption text-grey-6')
                            w_sec_otp_actions = ui.textarea(value='\n'.join(sec_otp.get('gated_actions',
                                ['shell', 'file_write', 'browser_open', 'browser', 'memory_forget']))).classes('w-full').props('outlined rows=4')
                            ui.label(T['lbl_otp_domains']).classes('text-caption text-grey-6')
                            w_sec_otp_domains = ui.textarea(value='\n'.join(sec_otp.get('gated_domains', []))).classes('w-full').props('outlined rows=3')
                        with ui.expansion(T['exp_estop'], icon='emergency').classes('w-full'):
                            w_sec_estop_enabled = ui.checkbox('enabled',               value=bool(sec_estop.get('enabled', False)))
                            w_sec_estop_file    = ui.input('state_file',               value=str(sec_estop.get('state_file', '~/.zeroclaw/estop-state.json'))).classes('w-full')
                            w_sec_estop_otp     = ui.checkbox('require_otp_to_resume', value=bool(sec_estop.get('require_otp_to_resume', True)))
                        with ui.expansion(T['exp_reliability'], icon='sync').classes('w-full'):
                            w_rel_retries    = ui.number('provider_retries',             value=reliability.get('provider_retries', 2),            min=0, step=1).classes('w-full')
                            w_rel_backoff    = ui.number('provider_backoff_ms',          value=reliability.get('provider_backoff_ms', 500),        min=0, step=100).classes('w-full')
                            w_rel_ch_backoff = ui.number('channel_initial_backoff_secs', value=reliability.get('channel_initial_backoff_secs', 2), min=1, step=1).classes('w-full')
                            w_rel_ch_max     = ui.number('channel_max_backoff_secs',     value=reliability.get('channel_max_backoff_secs', 60),    min=5, step=5).classes('w-full')
                        with ui.expansion(T['exp_scheduler'], icon='schedule').classes('w-full'):
                            w_sched_enabled    = ui.checkbox('enabled', value=scheduler.get('enabled', True))
                            w_sched_tasks      = ui.number('max_tasks',      value=scheduler.get('max_tasks', 64),     min=1, step=8).classes('w-full')
                            w_sched_concurrent = ui.number('max_concurrent', value=scheduler.get('max_concurrent', 4), min=1, step=1).classes('w-full')

                    # ══ Features ═════════════════════════════════════════════
                    with ui.tab_panel(t_feat):
                        with ui.expansion(T['exp_webfetch'], icon='download').classes('w-full'):
                            w_wf_enabled  = ui.checkbox('enabled', value=web_fetch.get('enabled', False))
                            ui.label(T['lbl_wf_allowed']).classes('text-caption text-grey-6')
                            w_wf_domains  = ui.textarea(value='\n'.join(web_fetch.get('allowed_domains', ['*']))).classes('w-full').props('outlined rows=3')
                            ui.label(T['lbl_wf_blocked']).classes('text-caption text-grey-6')
                            w_wf_blocked  = ui.textarea(value='\n'.join(web_fetch.get('blocked_domains', []))).classes('w-full').props('outlined rows=3')
                            w_wf_max_size = ui.number('max_response_size (bytes)', value=web_fetch.get('max_response_size', 500000), min=1000, step=100000).classes('w-full')
                            w_wf_timeout  = ui.number('timeout_secs',              value=web_fetch.get('timeout_secs', 30),         min=5,    step=5).classes('w-full')
                        with ui.expansion(T['exp_websearch'], icon='search').classes('w-full'):
                            w_ws_enabled  = ui.checkbox('enabled', value=web_search.get('enabled', False))
                            cur_wsp = web_search.get('provider', web_search.get('search_provider', 'duckduckgo'))
                            _ws_providers = ['duckduckgo', 'brave', 'tavily', 'searxng']
                            w_ws_provider = ui.select(_ws_providers, label='provider',
                                value=cur_wsp if cur_wsp in _ws_providers else 'duckduckgo').classes('w-full')
                            w_ws_max      = ui.number('max_results',  value=web_search.get('max_results', 5),   min=1, step=1).classes('w-full')
                            w_ws_timeout  = ui.number('timeout_secs', value=web_search.get('timeout_secs', 15), min=5, step=5).classes('w-full')
                        with ui.expansion(T['exp_httpreq'], icon='http').classes('w-full'):
                            w_http_enabled  = ui.checkbox('enabled', value=http_request.get('enabled', False))
                            ui.label(T['lbl_http_allowed']).classes('text-caption text-grey-6')
                            w_http_domains  = ui.textarea(value='\n'.join(http_request.get('allowed_domains', []))).classes('w-full').props('outlined rows=3')
                            w_http_max_size = ui.number('max_response_size (bytes)', value=http_request.get('max_response_size', 1000000), min=1000, step=100000).classes('w-full')
                            w_http_timeout  = ui.number('timeout_secs',              value=http_request.get('timeout_secs', 30),           min=5,    step=5).classes('w-full')
                        with ui.expansion(T['exp_browser'], icon='open_in_browser').classes('w-full'):
                            w_br_enabled   = ui.checkbox('enabled', value=browser.get('enabled', False))
                            ui.label(T['lbl_br_allowed']).classes('text-caption text-grey-6')
                            w_br_domains   = ui.textarea(value='\n'.join(browser.get('allowed_domains', []))).classes('w-full').props('outlined rows=3')
                            cur_bb = browser.get('backend', 'agent_browser')
                            w_br_backend   = ui.select(['agent_browser', 'rust_native', 'computer_use', 'auto'], label='backend',
                                value=cur_bb if cur_bb in ['agent_browser','rust_native','computer_use','auto'] else 'agent_browser').classes('w-full')
                            w_br_headless  = ui.checkbox('native_headless',      value=bool(browser.get('native_headless', True)))
                            w_br_webdriver = ui.input('native_webdriver_url',    value=str(browser.get('native_webdriver_url', 'http://127.0.0.1:9515'))).classes('w-full')
                        with ui.expansion(T['exp_multimodal'], icon='image').classes('w-full'):
                            w_mm_images     = ui.number('max_images',        value=multimodal.get('max_images', 4),        min=1, step=1).classes('w-full')
                            w_mm_image_size = ui.number('max_image_size_mb', value=multimodal.get('max_image_size_mb', 5), min=1, step=1).classes('w-full')
                            w_mm_remote     = ui.checkbox('allow_remote_fetch', value=bool(multimodal.get('allow_remote_fetch', False)))
                        with ui.expansion(T['exp_cost'], icon='attach_money').classes('w-full'):
                            w_cost_enabled  = ui.checkbox('enabled',         value=cost.get('enabled', False))
                            w_cost_override = ui.checkbox('allow_override',  value=bool(cost.get('allow_override', False)))
                            w_cost_daily    = ui.number('daily_limit_usd',   value=cost.get('daily_limit_usd', 10.0),    min=0, step=1.0).classes('w-full')
                            w_cost_monthly  = ui.number('monthly_limit_usd', value=cost.get('monthly_limit_usd', 100.0), min=0, step=5.0).classes('w-full')
                            w_cost_warn     = ui.number('warn_at_percent',   value=cost.get('warn_at_percent', 80),      min=10, max=100, step=5).classes('w-full')
                        with ui.expansion(T['exp_composio'], icon='hub').classes('w-full'):
                            w_comp_enabled = ui.checkbox('enabled', value=bool(composio_c.get('enabled', False)))
                            w_comp_entity  = ui.input('entity_id', value=str(composio_c.get('entity_id', 'default'))).classes('w-full')
                        with ui.expansion(T['exp_hooks'], icon='webhook').classes('w-full'):
                            w_hooks_enabled = ui.checkbox('hooks.enabled', value=bool(hooks.get('enabled', True)))
                        with ui.expansion(T['exp_hardware'], icon='developer_board').classes('w-full'):
                            w_hw_enabled    = ui.checkbox('enabled', value=bool(hardware.get('enabled', False)))
                            cur_ht = hardware.get('transport', 'none')
                            w_hw_transport  = ui.select(['None', 'native', 'serial', 'probe'], label='transport',
                                value=cur_ht if cur_ht in ['None','native','serial','probe'] else 'None').classes('w-full')
                            w_hw_baud       = ui.number('baud_rate',           value=hardware.get('baud_rate', 115200), min=1200, step=9600).classes('w-full')
                            w_hw_datasheets = ui.checkbox('workspace_datasheets', value=bool(hardware.get('workspace_datasheets', False)))

                    # ══ System ═══════════════════════════════════════════════
                    with ui.tab_panel(t_sys):
                        ui.label(T['section_transcription']).classes('text-subtitle2 text-grey-7 q-mt-sm')
                        w_tr_enabled      = ui.checkbox('enabled', value=transcription.get('enabled', False))
                        w_tr_url          = ui.input('api_url', value=str(transcription.get('api_url', 'https://api.groq.com/openai/v1/audio/transcriptions'))).classes('w-full')
                        w_tr_model        = ui.input('model',   value=str(transcription.get('model', 'whisper-large-v3-turbo'))).classes('w-full')
                        w_tr_max_duration = ui.number('max_duration_secs', value=transcription.get('max_duration_secs', 120), min=10, step=10).classes('w-full')
                        ui.separator().classes('q-my-sm')
                        ui.label(T['section_heartbeat']).classes('text-subtitle2 text-grey-7')
                        w_hb_enabled  = ui.checkbox('enabled', value=heartbeat.get('enabled', False))
                        w_hb_interval = ui.number('interval_minutes', value=heartbeat.get('interval_minutes', 30), min=1, step=5).classes('w-full')
                        ui.separator().classes('q-my-sm')
                        ui.label(T['section_cron']).classes('text-subtitle2 text-grey-7')
                        w_cron_enabled     = ui.checkbox('enabled', value=cron.get('enabled', True))
                        w_cron_max_history = ui.number('max_run_history', value=cron.get('max_run_history', 50), min=1, step=10).classes('w-full')
                        ui.separator().classes('q-my-sm')
                        ui.label(T['section_logs']).classes('text-subtitle2 text-grey-7')
                        with ui.row().classes('w-full gap-2'):
                            ui.button(T['btn_view_logs'], icon='article', on_click=lambda: ui.notify(
                                subprocess.getoutput('journalctl -u zeroclaw.service -n 30 --no-pager'),
                                multi_line=True, timeout=15000)).props('outline').classes('flex-1')
                            ui.button(T['btn_service_status'], icon='info', on_click=do_status).props('outline').classes('flex-1')

                ui.separator()
                with ui.row().classes('w-full gap-2 q-pa-sm'):
                    ui.button(T['btn_save'],         on_click=do_save).props('elevated').classes('flex-1 bg-blue text-white')
                    ui.button(T['btn_save_deploy'], on_click=do_save_deploy).props('elevated').classes('flex-1 bg-green text-white')

            # ── ZeroClaw › Pair Device ─────────────────────────────────────
            with ui.tab_panel(t_zc_pair):
                # ── Gateway pair code ──────────────────────────────────────
                with ui.card().classes('w-full q-mb-sm'):
                    ui.label(T['pair_gw_title']).classes('text-subtitle1 text-bold q-mb-xs')
                    ui.label(T['pair_gw_hint']).classes('text-caption text-grey-6 q-mb-sm')
                    paircode_box = ui.card().classes('w-full bg-grey-2 q-pa-md items-center')
                    with paircode_box:
                        paircode_lbl = ui.label('…').classes(
                            'text-h4 text-bold text-blue-9 text-center letter-spacing-wide'
                        )
                        paircode_status = ui.label(T['pair_loading']).classes('text-caption text-grey-6 text-center q-mt-xs')

                    _PAIRCODE_SCRIPT = os.path.join(SCRIPT_DIR, 'clawberry_paircode.py')

                    def _parse_paircode(raw: str):
                        """Extract the numeric code from box-drawing output. Returns None if not found."""
                        for _line in raw.splitlines():
                            _s = _line.strip()
                            if _s.startswith('│') and _s.endswith('│'):
                                _inner = _s[1:-1].strip()
                                if _inner:
                                    return _inner
                        return None  # no code in output

                    def _push_to_display(code: str):
                        """Queue pair code for the clawberry-display service via handoff file."""
                        try:
                            import importlib
                            import clawberry_paircode as _cp
                            importlib.reload(_cp)
                            _cp.request_paircode_display(code)
                            paircode_status.set_text('✅ Queued — sending to e-ink display')
                        except Exception as exc:
                            paircode_status.set_text(f'⚠️ Could not queue display: {exc}')
                            ui.notify(f'Display queue error: {exc}', type='warning')

                    def _fetch_paircode(new: bool = False, push_display: bool = True):
                        """Fetch current (or generate new) pair code and update UI.
                        Only pushes to e-ink display when push_display=True."""
                        paircode_lbl.set_text('…')
                        paircode_status.set_text('Generating new code…' if new else 'Fetching current code…')
                        try:
                            cmd = ['zeroclaw', 'gateway', 'get-paircode']
                            if new:
                                cmd.append('--new')
                            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                            raw = r.stdout.strip() or r.stderr.strip()
                            if r.returncode != 0:
                                paircode_lbl.set_text('Error')
                                paircode_status.set_text(f'Command failed: {raw[:80]}')
                                ui.notify(f'❌ {raw}', type='negative')
                                return
                            code = _parse_paircode(raw)
                            if code is None:
                                paircode_lbl.set_text('NA')
                                paircode_status.set_text('No available pair code')
                                return
                            paircode_lbl.set_text(code)
                            if push_display:
                                _push_to_display(code)
                                if new:
                                    ui.notify(f'✅ New pair code: {code}', type='positive')
                            else:
                                paircode_status.set_text('Code fetched (not pushed to display)')
                        except FileNotFoundError:
                            paircode_lbl.set_text('N/A')
                            paircode_status.set_text('zeroclaw not found in PATH')
                            ui.notify('❌ zeroclaw not found in PATH', type='negative')
                        except subprocess.TimeoutExpired:
                            paircode_lbl.set_text('Timeout')
                            paircode_status.set_text('Command timed out')
                            ui.notify('❌ Command timed out', type='negative')
                        except Exception as exc:
                            paircode_lbl.set_text('Error')
                            paircode_status.set_text(str(exc))
                            ui.notify(f'❌ {exc}', type='negative')

                    # Idle state — user presses a button to load/generate
                    paircode_lbl.set_text('NA')
                    paircode_status.set_text(T['pair_idle'])

                    with ui.row().classes('w-full gap-2 q-mt-sm'):
                        ui.button(T['pair_btn_refresh'], on_click=lambda: _fetch_paircode(new=False, push_display=True)).props(
                            'outline color=blue-8'
                        ).classes('flex-1')
                        ui.button(T['pair_btn_new'], on_click=lambda: _fetch_paircode(new=True, push_display=True)).props(
                            'elevated color=blue-8'
                        ).classes('flex-1')

                # ── Paired Devices ─────────────────────────────────────────
                with ui.card().classes('w-full q-mb-sm'):
                    ui.label(T['pair_devices_title']).classes('text-subtitle1 text-bold q-mb-xs')
                    device_list = ui.column().classes('w-full')
                    def _refresh_devices():
                        device_list.clear()
                        d2 = _load_auth()
                        devs = d2.get('paired_devices', []) if d2 else []
                        if not devs:
                            with device_list:
                                ui.label(T['pair_no_devices']).classes('text-caption text-grey-5')
                            return
                        for dv in devs:
                            dt = datetime.fromtimestamp(dv['paired_at']).strftime('%Y-%m-%d %H:%M')
                            with device_list:
                                with ui.row().classes('w-full items-center justify-between'):
                                    ui.label(f"📱 {dv['name']}  ({dt})").classes('text-caption')
                                    def _revoke(t=dv['token']):
                                        d3 = _load_auth()
                                        if d3:
                                            d3['paired_devices'] = [x for x in d3.get('paired_devices', []) if x['token'] != t]
                                            _save_auth(d3)
                                        _refresh_devices()
                                    ui.button(icon='delete', on_click=_revoke).props('flat round dense color=negative')
                    _refresh_devices()
                    ui.separator()
                    invite_lbl = ui.label('').classes('text-caption text-blue-7 q-mt-xs break-all')
                    def _gen_invite():
                        it = secrets.token_urlsafe(16)
                        _invite_tokens[it] = _time.time() + 300
                        base = str(request.base_url).rstrip('/')
                        link = f'{base}/pair?token={it}'
                        invite_lbl.set_text(f'🔗 {link}  (valid 5 min)')
                        ui.clipboard.write(link)
                        ui.notify(T['pair_invite_copied'], type='positive')
                    ui.button(T['pair_invite_btn'], on_click=_gen_invite).props('outline color=green')

            # ── ZeroClaw › Characters ──────────────────────────────────────
            with ui.tab_panel(t_zc_char):
                _build_character_tab('/var/lib/zeroclaw/.zeroclaw/workspace', 'zeroclaw', 'blue-8')

            # ── ZeroClaw › Skills ─────────────────────────────────────────
            with ui.tab_panel(t_zc_skills):
                _build_skills_tab('/var/lib/zeroclaw/.zeroclaw/workspace', 'zeroclaw', 'blue-8')

    # ══ PicoClaw Dashboard ════════════════════════════════════════════════════
    pc_content = ui.column().classes('w-full q-px-sm q-pt-sm')
    pc_content.set_visibility(False)
    with pc_content:
        with ui.row().classes('w-full items-center q-mb-xs'):
            ui.label(T['pc_dashboard']).classes('text-h6 text-purple-8')
            ui.button(icon='refresh', on_click=lambda: ui.navigate.reload()) \
                .props('flat round dense color=purple-8').tooltip(T['tooltip_reload'])
        with ui.tabs().classes('w-full bg-purple-1') as pc_sub_tabs:
            t_pc_wiz  = ui.tab(T['pc_tab_wizard'],     icon='auto_fix_high')
            t_pc_cfg  = ui.tab(T['tab_configuration'], icon='settings')
            t_pc_pair = ui.tab(T['tab_pair_device'],   icon='devices')
            t_pc_char   = ui.tab(T['tab_characters'],    icon='face')
            t_pc_skills = ui.tab(T['tab_skills'],        icon='extension')

        with ui.tab_panels(pc_sub_tabs, value=t_pc_cfg).classes('w-full'):

            # ── PicoClaw › Wizard ──────────────────────────────────────────
            with ui.tab_panel(t_pc_wiz):
                ui.label('🧙 Quick Setup Wizard').classes('text-h6 text-purple-8 q-mb-xs')
                ui.label(
                    'Configure provider, tools and security in 3 steps. '
                    'Click Apply — then restart PicoClaw to activate.'
                ).classes('text-caption text-grey-6 q-mb-md')

                # ── Pre-load current config values to populate wizard ─────
                _pc_wiz_cur_conf  = load_picoclaw_config()
                _pc_wiz_cur_ad    = _pc_wiz_cur_conf.get('agents', {}).get('defaults', {})
                _pc_wiz_cur_ml    = _pc_wiz_cur_conf.get('model_list', [])
                _pc_wiz_cur_sec   = load_picoclaw_security()
                _pc_wiz_init_prov  = _pc_wiz_cur_ad.get('provider', _pc_ph_provs[0] if _pc_ph_provs else '')
                _pc_wiz_init_mname = _pc_wiz_cur_ad.get('model_name', None)
                _pc_wiz_init_model = _pc_wiz_cur_ad.get('model', '')
                _pc_wiz_cur_mle    = next(
                    (e for e in _pc_wiz_cur_ml if e.get('model_name') == _pc_wiz_init_mname), {})
                _pc_wiz_init_api_base = (
                    _pc_wiz_cur_mle.get('api_base', '') or
                    _pc_ph_pid_base.get(_pc_wiz_init_prov, ''))
                _pc_wiz_init_auth  = _pc_wiz_cur_mle.get('auth_method', 'apikey')
                _pc_wiz_sec_ml     = _pc_wiz_cur_sec.get('model_list', {})
                _pc_wiz_init_keys  = (
                    _pc_wiz_sec_ml.get(_pc_wiz_init_mname or '', {}).get('api_keys') or
                    _pc_wiz_sec_ml.get(f'{_pc_wiz_init_mname}:0', {}).get('api_keys') or [])
                _pc_wiz_init_api_key = _pc_wiz_init_keys[0] if _pc_wiz_init_keys else ''
                # Ensure current provider/model_name appear in option lists
                _pc_wiz_prov_opts  = list(_pc_ph_provs)
                if _pc_wiz_init_prov and _pc_wiz_init_prov not in _pc_wiz_prov_opts:
                    _pc_wiz_prov_opts = [_pc_wiz_init_prov] + _pc_wiz_prov_opts
                if not _pc_wiz_prov_opts:
                    _pc_wiz_prov_opts = [PROVIDER_IDS[0]]
                if _pc_wiz_init_prov not in _pc_wiz_prov_opts:
                    _pc_wiz_init_prov = _pc_wiz_prov_opts[0]
                _pc_wiz_mname_opts = list(_pc_ph_map.keys())
                if _pc_wiz_init_mname and _pc_wiz_init_mname not in _pc_wiz_mname_opts:
                    _pc_wiz_mname_opts = [_pc_wiz_init_mname] + _pc_wiz_mname_opts
                if not _pc_wiz_mname_opts:
                    _pc_wiz_mname_opts = ['']
                if _pc_wiz_init_mname not in _pc_wiz_mname_opts:
                    _pc_wiz_init_mname = _pc_wiz_mname_opts[0]

                with ui.stepper(value='pc_wiz_prov').props('vertical animated').classes('w-full') as _pc_wiz:

                    # ── Step 1: Provider + Model ────────────────────────────
                    with ui.step('pc_wiz_prov', title='1  Provider & Model', icon='cloud'):
                        ui.label('Pick a known model — fields fill automatically.').classes('text-caption text-grey-6 q-mb-sm')

                        ui.label('⚡ Quick pick').classes('text-caption text-purple-7')
                        pc_wiz_quick = ui.select(
                            options=list(_pc_ph_map.keys()),
                            label='Known model',
                            value=None,
                            clearable=True,
                            with_input=True,
                        ).classes('w-full q-mb-xs')
                        ui.separator().classes('q-my-xs')

                        pc_wiz_prov      = ui.select(_pc_wiz_prov_opts, label='provider',
                            value=_pc_wiz_init_prov).classes('w-full q-mb-sm')
                        pc_wiz_model_name= ui.select(
                            options=_pc_wiz_mname_opts,
                            label='model_name',
                            value=_pc_wiz_init_mname,
                            with_input=True,
                            new_value_mode='add-unique',
                        ).classes('w-full q-mb-sm')
                        pc_wiz_model     = ui.input('model  (actual model id sent to provider)', value=_pc_wiz_init_model).classes('w-full q-mb-sm')
                        pc_wiz_api_base  = ui.input('api_base', value=_pc_wiz_init_api_base).classes('w-full q-mb-sm')
                        _pc_wiz_auth_opts  = ['apikey', 'oauth']
                        pc_wiz_auth_method = ui.select(_pc_wiz_auth_opts, label='auth_method',
                            value=_pc_wiz_init_auth if _pc_wiz_init_auth in _pc_wiz_auth_opts else 'apikey',
                        ).classes('w-full q-mb-sm')
                        pc_wiz_api_key   = ui.input('api_key', value=_pc_wiz_init_api_key,
                            password=True, password_toggle_button=True).classes('w-full')

                        def _pc_wiz_fill_hint(e):
                            h = _pc_ph_map.get(e.value) if e.value else None
                            if not h: return
                            prov = h.get('provider', '')
                            if prov in _pc_wiz_prov_opts: pc_wiz_prov.set_value(prov)
                            pc_wiz_model_name.set_value(h.get('model_name', ''))
                            pc_wiz_model.set_value(h.get('model', ''))
                            pc_wiz_api_base.set_value(h.get('api_base', ''))
                            _am = h.get('auth_method', 'apikey')
                            pc_wiz_auth_method.set_value(_am if _am in _pc_wiz_auth_opts else 'apikey')
                        pc_wiz_quick.on_value_change(_pc_wiz_fill_hint)

                        def _pc_wiz_fill_prov(e):
                            prov = e.value or ''
                            if not prov: return
                            pc_wiz_api_base.set_value(_pc_ph_pid_base.get(prov, ''))
                            models = _pc_ph_pid_models.get(prov, [])
                            if models:
                                pc_wiz_model_name.set_options(models, value=models[0])
                                first = _pc_ph_map.get(models[0], {})
                                pc_wiz_model.set_value(first.get('model', ''))
                        pc_wiz_prov.on_value_change(_pc_wiz_fill_prov)

                        def _pc_wiz_fill_mname(e):
                            h = _pc_ph_map.get(e.value) if e.value else None
                            if not h: return
                            pc_wiz_model.set_value(h.get('model', ''))
                            pc_wiz_api_base.set_value(h.get('api_base', ''))
                        pc_wiz_model_name.on_value_change(_pc_wiz_fill_mname)

                        with ui.stepper_navigation():
                            ui.button('Next →', on_click=_pc_wiz.next).props('color=purple-8')

                    # ── Step 2: Tools & Security ─────────────────────────────
                    with ui.step('pc_wiz_tools_sec', title='2  Tools & Security', icon='security'):
                        ui.label('Enable tools and set workspace security.').classes('text-caption text-grey-6 q-mb-sm')

                        ui.label('🔧 Tools').classes('text-subtitle2 text-purple-8 q-mb-xs')
                        pc_wiz_tool_web_search = ui.checkbox('Web Search', value=True)
                        pc_wiz_tool_exec       = ui.checkbox('exec  (shell command execution)', value=True)

                        ui.separator().classes('q-my-sm')
                        ui.label('🔒 Security').classes('text-subtitle2 text-purple-8 q-mb-xs')
                        pc_wiz_sec_restrict   = ui.checkbox('restrict_to_workspace', value=True)
                        pc_wiz_sec_allow_read = ui.checkbox('allow_read_outside_workspace', value=False)

                        with ui.stepper_navigation():
                            ui.button('← Back', on_click=_pc_wiz.previous).props('flat color=grey-7')
                            ui.button('Next →', on_click=_pc_wiz.next).props('color=purple-8')

                    # ── Step 3: Apply ────────────────────────────────────────
                    with ui.step('pc_wiz_apply', title='3  Apply', icon='check_circle'):
                        ui.label('Review and apply the provider settings.').classes('text-caption text-grey-6 q-mb-sm')

                        def _pc_wiz_summary():
                            return (
                                f'provider: {pc_wiz_prov.value}\n'
                                f'auth_method: {pc_wiz_auth_method.value}\n'
                                f'model_name: {pc_wiz_model_name.value or "(unchanged)"}\n'
                                f'model: {pc_wiz_model.value or "(unchanged)"}\n'
                                f'api_base: {pc_wiz_api_base.value or "(provider default)"}\n'
                                f'Web Search:                  {"on" if pc_wiz_tool_web_search.value else "off"}\n'
                                f'exec:                        {"on" if pc_wiz_tool_exec.value else "off"}\n'
                                f'restrict_to_workspace:        {pc_wiz_sec_restrict.value}\n'
                                f'allow_read_outside_workspace: {pc_wiz_sec_allow_read.value}'
                            )
                        pc_wiz_summary_lbl = ui.label('').classes('text-caption text-grey-7 q-mb-sm')

                        def _pc_wiz_refresh_summary():
                            pc_wiz_summary_lbl.set_text(_pc_wiz_summary())
                        _pc_wiz.on('transition', lambda _: _pc_wiz_refresh_summary())

                        def _pc_wiz_apply():
                            data = load_picoclaw_config()
                            sec  = load_picoclaw_security()
                            # Normalize :N-suffixed stale keys (e.g. "MiniMax-M2.5:0" → "MiniMax-M2.5")
                            _sec_ml_raw = sec.get('model_list', {})
                            _sec_ml_norm: dict = {}
                            for _k, _v in _sec_ml_raw.items():
                                _ck = _k.rsplit(':', 1)[0] if _k.rsplit(':', 1)[-1].isdigit() else _k
                                if _ck not in _sec_ml_norm or (
                                        _v.get('api_keys') and not _sec_ml_norm[_ck].get('api_keys')):
                                    _sec_ml_norm[_ck] = _v
                            sec['model_list'] = _sec_ml_norm
                            ad = data.setdefault('agents', {}).setdefault('defaults', {})
                            if pc_wiz_prov.value:       ad['provider']    = pc_wiz_prov.value
                            if pc_wiz_model_name.value: ad['model_name']  = pc_wiz_model_name.value
                            # security
                            ad['restrict_to_workspace']        = pc_wiz_sec_restrict.value
                            ad['allow_read_outside_workspace'] = pc_wiz_sec_allow_read.value
                            # tools
                            data.setdefault('tools', {}).setdefault('web', {})['enabled']  = pc_wiz_tool_web_search.value
                            data.setdefault('tools', {}).setdefault('exec', {})['enabled'] = pc_wiz_tool_exec.value
                            # Inject model into model_list if not already present
                            mname = pc_wiz_model_name.value
                            if mname:
                                ml = data.setdefault('model_list', [])
                                existing = [e for e in ml if e.get('model_name') == mname]
                                if not existing:
                                    entry = {'model_name': mname, 'model': pc_wiz_model.value,
                                             'api_base': pc_wiz_api_base.value,
                                             'auth_method': pc_wiz_auth_method.value}
                                    ml.append(entry)
                                else:
                                    existing[0]['api_base'] = pc_wiz_api_base.value
                                    existing[0]['auth_method'] = pc_wiz_auth_method.value
                                if pc_wiz_api_key.value:
                                    sec.setdefault('model_list', {}).setdefault(mname, {})['api_keys'] = [pc_wiz_api_key.value]
                            try:
                                save_picoclaw_config(data)
                                save_picoclaw_security(sec)
                                ok_cfg, err_cfg = deploy_picoclaw_config()
                                ok_sec, err_sec = deploy_picoclaw_security()
                                deploy_errs = [e for e in [err_cfg, err_sec] if e]
                                if deploy_errs:
                                    ui.notify(f'⚠️ Saved locally but deploy failed: {"; ".join(deploy_errs)}', type='warning')
                                else:
                                    ui.notify('✅ PicoClaw config saved & deployed — restart PicoClaw to activate', type='positive')
                            except Exception as ex:
                                ui.notify(f'❌ Save failed: {ex}', type='negative')

                        with ui.stepper_navigation():
                            ui.button('← Back', on_click=_pc_wiz.previous).props('flat color=grey-7')
                            ui.button('✅ Apply & Save', on_click=_pc_wiz_apply).props('color=green-8')

            # ── PicoClaw › Configuration ───────────────────────────────────
            with ui.tab_panel(t_pc_cfg):
                pc_conf = load_picoclaw_config()
                pc_sec  = load_picoclaw_security()

                # shortcuts
                pc_session   = pc_conf.get('session',   {})
                pc_agents    = pc_conf.get('agents',    {}).get('defaults', {})
                pc_channels  = pc_conf.get('channel_list', {}) or pc_conf.get('channels', {})
                pc_model_list= pc_conf.get('model_list', [])
                pc_gateway   = pc_conf.get('gateway',   {})
                pc_tools     = pc_conf.get('tools',     {})
                pc_web_tools = pc_tools.get('web',      {})
                pc_heartbeat = pc_conf.get('heartbeat', {})
                pc_devices   = pc_conf.get('devices',   {})
                pc_voice     = pc_conf.get('voice',     {})
                pc_build     = pc_conf.get('build_info',{})

                # security.yml shortcuts
                pc_sec_models   = pc_sec.get('model_list', {})
                pc_sec_channels = pc_sec.get('channel_list', {}) or pc_sec.get('channels', {})
                pc_sec_web      = pc_sec.get('web',        {})
                pc_sec_skills   = pc_sec.get('skills',     {})

                pc_model_panels = {}  # idx → widget dict
                pc_model_cards  = {}  # idx → card element (for show/hide)
                _pc_sel_ref  = [None]  # forward ref → _pc_model_sel (set in Models tab)
                _pc_show_ref = [None]  # forward ref → _pc_show_model (set in Models tab)

                def _make_pc_sel_opt(i, mname):
                    return f'[{i}] {mname or "(new)"}'

                def _pc_idx_from_opt(opt_str):
                    try:
                        return int(str(opt_str).split(']')[0].lstrip('[').strip())
                    except (ValueError, AttributeError):
                        return None

                def pc_build_model_card(container, idx, entry):
                    _mname    = str(entry.get('model_name', ''))
                    _sec_keys = pc_sec_models.get(_mname, {}).get('api_keys', []) or []
                    _keys_txt = '\n'.join(str(k) for k in _sec_keys)
                    with container:
                        with ui.card().classes('w-full q-mb-sm') as card:
                            with ui.row().classes('w-full items-center justify-between'):
                                ui.label(f'model [{idx}]').classes('text-caption text-purple-7 text-bold')
                                def _rm(i=idx, c=card):
                                    pc_model_panels.pop(i, None)
                                    pc_model_cards.pop(i, None)
                                    c.delete()
                                    sel = _pc_sel_ref[0]
                                    if sel:
                                        new_opts = [o for o in sel.options if _pc_idx_from_opt(o) != i]
                                        new_val  = new_opts[0] if new_opts else None
                                        sel.set_options(new_opts, value=new_val)
                                        if new_val is not None and _pc_show_ref[0]:
                                            t = _pc_idx_from_opt(new_val)
                                            if t is not None: _pc_show_ref[0](t)
                                ui.button(icon='delete', on_click=_rm).props('flat round dense color=negative')
                            ui.label('⚡ Quick pick').classes('text-caption text-purple-7')
                            _card_quick = ui.select(
                                options=list(_pc_ph_map.keys()),
                                label='Known model (auto-fill)',
                                value=_mname if _mname in _pc_ph_map else None,
                                clearable=True, with_input=True,
                            ).classes('w-full q-mb-xs')
                            ui.separator().classes('q-my-xs')
                            widgets = {}
                            _card_mname_opts = list(_pc_ph_map.keys())
                            if _mname and _mname not in _card_mname_opts:
                                _card_mname_opts = [_mname] + _card_mname_opts
                            widgets['model_name'] = ui.select(
                                options=_card_mname_opts,
                                label='model_name',
                                value=_mname or None,
                                with_input=True, new_value_mode='add-unique',
                            ).classes('w-full')
                            widgets['model']     = ui.input('model',    value=str(entry.get('model',''))).classes('w-full')
                            widgets['api_base']  = ui.input('api_base', value=str(entry.get('api_base',''))).classes('w-full')
                            ui.label('api_keys  (one per line → security.yml)').classes('text-caption text-grey-6 q-mt-xs')
                            widgets['api_keys']  = ui.textarea(value=_keys_txt).classes('w-full').props('outlined rows=3 label=api_keys')
                            cur_auth  = str(entry.get('auth_method', 'apikey'))
                            auth_opts = ['apikey', 'oauth']
                            widgets['auth_method'] = ui.select(auth_opts, label='auth_method',
                                value=cur_auth if cur_auth in auth_opts else 'apikey').classes('w-full')
                            pc_model_panels[idx] = widgets
                            pc_model_cards[idx]  = card
                            card.set_visibility(False)

                            # Quick-pick fills all fields
                            def _card_fill(e, _w=widgets, _qk=_card_quick):
                                h = _pc_ph_map.get(e.value) if e.value else None
                                if not h: return
                                _w['model_name'].set_value(h.get('model_name', ''))
                                _w['model'].set_value(h.get('model', ''))
                                _w['api_base'].set_value(h.get('api_base', ''))
                                am = h.get('auth_method', 'apikey')
                                _w['auth_method'].set_value(am if am in auth_opts else 'apikey')
                            _card_quick.on_value_change(_card_fill)

                            # model_name select → fill model field + update selector label
                            def _card_mname(e, _w=widgets, _idx=idx):
                                h = _pc_ph_map.get(e.value) if e.value else None
                                if h and h.get('model'): _w['model'].set_value(h['model'])
                                sel = _pc_sel_ref[0]
                                if sel:
                                    new_opts = [
                                        _make_pc_sel_opt(_idx, e.value) if _pc_idx_from_opt(o) == _idx else o
                                        for o in sel.options
                                    ]
                                    sel.set_options(new_opts, value=sel.value)
                            widgets['model_name'].on_value_change(_card_mname)

                def pc_collect_and_save():
                    data = load_picoclaw_config()
                    sec  = load_picoclaw_security()

                    # ── Remove V3-invalid legacy keys that may be in the file ──
                    data.get('agents', {}).get('defaults', {}).pop('model', None)
                    data.get('session', {}).pop('dm_scope', None)

                    # session  (dm_scope is removed in V3; do not re-write it)
                    # (no session fields currently saved from the General tab)

                    # agents.defaults  (note: 'model' is V0-legacy — not written)
                    ad = data.setdefault('agents', {}).setdefault('defaults', {})
                    ad['workspace']                  = pc_w_workspace.value
                    ad['restrict_to_workspace']      = pc_w_restrict.value
                    ad['allow_read_outside_workspace']= pc_w_allow_read_outside.value
                    ad['provider']                   = pc_w_provider.value
                    ad['model_name']                 = pc_w_model_name.value
                    ad['max_tokens']                 = to_int(pc_w_max_tokens.value, 8192)
                    ad['max_tool_iterations']        = to_int(pc_w_max_iter.value, 50)
                    ad['summarize_message_threshold']= to_int(pc_w_sum_threshold.value, 20)
                    ad['summarize_token_percent']    = to_int(pc_w_sum_percent.value, 75)

                    # model_list → config.json (no api_keys); api_keys → security.yml
                    # Rebuild sec['model_list'] from scratch so stale names (e.g. MiniMax-M2.5:0)
                    # that are no longer in the UI are pruned. Existing keys in the file are
                    # preserved when the widget textarea is empty (user didn't change them).
                    # Normalize :N-suffixed stale keys before lookup
                    _old_sec_ml: dict = {}
                    for _k, _v in sec.get('model_list', {}).items():
                        _ck = _k.rsplit(':', 1)[0] if _k.rsplit(':', 1)[-1].isdigit() else _k
                        if _ck not in _old_sec_ml or (
                                _v.get('api_keys') and not _old_sec_ml[_ck].get('api_keys')):
                            _old_sec_ml[_ck] = _v
                    _new_sec_ml: dict = {}
                    data['model_list'] = []
                    _seen_mnames: set = set()
                    for w in pc_model_panels.values():
                        mname = w['model_name'].value
                        keys = [k.strip() for k in w['api_keys'].value.splitlines() if k.strip()]
                        if mname not in _seen_mnames:
                            _seen_mnames.add(mname)
                            data['model_list'].append({
                                'model_name':  mname,
                                'model':       w['model'].value,
                                'api_base':    w['api_base'].value,
                                'auth_method': w['auth_method'].value,
                            })
                        # Widget has real keys → use them; widget empty → preserve file value
                        if keys:
                            _new_sec_ml.setdefault(mname, {})['api_keys'] = keys
                        else:
                            old_keys = _old_sec_ml.get(mname, {}).get('api_keys') or []
                            _new_sec_ml.setdefault(mname, {})['api_keys'] = old_keys
                    # Replace entire section — removes any stale/duplicate names
                    sec['model_list'] = _new_sec_ml

                    # gateway
                    data.setdefault('gateway', {})['host'] = pc_w_gw_host.value
                    data['gateway']['port']                 = to_int(pc_w_gw_port.value, 18790)

                    # channels – enable flags + non-secret fields → config.json; secrets → security.yml
                    data.pop('channels', None)   # remove legacy key
                    sec.pop('channels', None)    # remove legacy key
                    ch     = data.setdefault('channel_list', {})
                    sec_ch = sec.setdefault('channel_list', {})
                    def _ch_cfg(name, **kw):
                        ch.setdefault(name, {}).update(kw)
                    def _ch_sec(name, **kw):
                        if kw: sec_ch.setdefault(name, {}).update(kw)

                    _ch_cfg('pico',     enabled=pc_w_ch_pico_en.value,
                            ping_interval=to_int(pc_w_ch_pico_ping.value, 30),
                            max_connections=to_int(pc_w_ch_pico_maxconn.value, 100))
                    _ch_sec('pico',     token=pc_w_ch_pico_token.value)

                    _ch_cfg('qq',       enabled=pc_w_ch_qq_en.value, app_id=pc_w_ch_qq_appid.value)
                    _ch_sec('qq',       app_secret=pc_w_ch_qq_secret.value)

                    _ch_cfg('telegram', enabled=pc_w_ch_tg_en.value,
                            base_url=pc_w_ch_tg_base.value, proxy=pc_w_ch_tg_proxy.value)
                    _ch_sec('telegram', token=pc_w_ch_tg_token.value)

                    _ch_cfg('discord',  enabled=pc_w_ch_dc_en.value, mention_only=pc_w_ch_dc_mention.value)
                    _ch_sec('discord',  token=pc_w_ch_dc_token.value)

                    _ch_cfg('whatsapp', enabled=pc_w_ch_wa_en.value,
                            bridge_url=pc_w_ch_wa_url.value, use_native=pc_w_ch_wa_native.value)

                    _ch_cfg('feishu',   enabled=pc_w_ch_fs_en.value, app_id=pc_w_ch_fs_appid.value)
                    _ch_sec('feishu',   app_secret=pc_w_ch_fs_secret.value,
                            encrypt_key=pc_w_ch_fs_encrypt.value,
                            verification_token=pc_w_ch_fs_verify.value)

                    _ch_cfg('slack',    enabled=pc_w_ch_sl_en.value)
                    _ch_sec('slack',    bot_token=pc_w_ch_sl_bot.value, app_token=pc_w_ch_sl_app.value)

                    _ch_cfg('matrix',   enabled=pc_w_ch_mx_en.value,
                            homeserver=pc_w_ch_mx_home.value, user_id=pc_w_ch_mx_user.value)
                    _ch_sec('matrix',   access_token=pc_w_ch_mx_token.value)

                    _ch_cfg('dingtalk',  enabled=pc_w_ch_dt_en.value, client_id=pc_w_ch_dt_id.value)
                    _ch_sec('dingtalk',  client_secret=pc_w_ch_dt_secret.value)

                    _ch_cfg('maixcam',  enabled=pc_w_ch_mc_en.value,
                            host=pc_w_ch_mc_host.value, port=to_int(pc_w_ch_mc_port.value, 18790))

                    _ch_cfg('irc',      enabled=pc_w_ch_irc_en.value,
                            server=pc_w_ch_irc_server.value, nick=pc_w_ch_irc_nick.value, tls=pc_w_ch_irc_tls.value)

                    _ch_cfg('onebot',   enabled=pc_w_ch_ob_en.value, ws_url=pc_w_ch_ob_ws.value)
                    _ch_sec('onebot',   access_token=pc_w_ch_ob_token.value)

                    _ch_cfg('line',     enabled=pc_w_ch_line_en.value)
                    _ch_sec('line',     channel_secret=pc_w_ch_line_secret.value,
                            channel_access_token=pc_w_ch_line_cat.value)

                    _ch_cfg('wecom',    enabled=pc_w_ch_wc_en.value)
                    _ch_sec('wecom',    token=pc_w_ch_wc_token.value,
                            encoding_aes_key=pc_w_ch_wc_aes.value)

                    # tools
                    t = data.setdefault('tools', {})
                    t['allow_read_paths']  = lines_to_list(pc_w_t_read_paths.value) or None
                    t['allow_write_paths'] = lines_to_list(pc_w_t_write_paths.value) or None

                    web = t.setdefault('web', {})
                    web['enabled']           = pc_w_t_web_en.value
                    web['fetch_limit_bytes'] = to_int(pc_w_t_fetch_limit.value, 10485760)
                    web.setdefault('duckduckgo', {})['enabled']   = pc_w_t_ddg.value
                    web.setdefault('brave',      {})['enabled']   = pc_w_t_brave.value
                    web.setdefault('tavily',     {})['enabled']   = pc_w_t_tavily.value
                    web.setdefault('perplexity', {})['enabled']   = pc_w_t_perp.value
                    web.setdefault('searxng',    {})['enabled']   = pc_w_t_searxng.value
                    web['searxng']['base_url']                     = pc_w_t_searxng_url.value

                    # web search api_keys → security.yml (plural array per spec)
                    sec_web = sec.setdefault('web', {})
                    def _web_sec(name, widget):
                        keys = [k.strip() for k in widget.value.splitlines() if k.strip()]
                        sec_web.setdefault(name, {})['api_keys'] = keys
                    _web_sec('brave',      pc_w_t_brave_key)
                    _web_sec('tavily',     pc_w_t_tavily_key)
                    _web_sec('perplexity', pc_w_t_perp_key)

                    # skills auth tokens → security.yml
                    sec_sk = sec.setdefault('skills', {})
                    sec_sk.setdefault('clawhub', {})['auth_token'] = pc_w_t_clawhub_auth.value
                    sec_sk.setdefault('github',  {})['token']       = pc_w_t_github_token.value

                    t.setdefault('exec',  {})['enabled']          = pc_w_t_exec.value
                    t['exec']['timeout_seconds']                   = to_int(pc_w_t_exec_timeout.value, 60)
                    t['exec']['allow_remote']                      = pc_w_t_exec_remote.value
                    t.setdefault('cron',  {})['enabled']          = pc_w_t_cron.value
                    t['cron']['allow_command']                     = pc_w_t_cron_cmd.value
                    t.setdefault('skills',{}).setdefault('registries',{}).setdefault('clawhub',{})['enabled'] = pc_w_t_skills.value
                    t.setdefault('mcp',   {})['enabled']          = pc_w_t_mcp.value
                    t.setdefault('spawn', {})['enabled']          = pc_w_t_spawn.value
                    t.setdefault('subagent',{})['enabled']        = pc_w_t_subagent.value
                    t.setdefault('web_fetch',{})['enabled']       = pc_w_t_web_fetch.value
                    t.setdefault('send_file',{})['enabled']       = pc_w_t_send_file.value
                    t.setdefault('read_file',{})['enabled']       = pc_w_t_read_file.value
                    t['read_file']['max_read_file_size']           = to_int(pc_w_t_read_max.value, 65536)
                    t.setdefault('write_file',{})['enabled']      = pc_w_t_write_file.value
                    t.setdefault('edit_file', {})['enabled']      = pc_w_t_edit_file.value
                    t.setdefault('append_file',{})['enabled']     = pc_w_t_append_file.value
                    t.setdefault('list_dir',  {})['enabled']      = pc_w_t_list_dir.value
                    t.setdefault('message',   {})['enabled']      = pc_w_t_message.value
                    t.setdefault('i2c',       {})['enabled']      = pc_w_t_i2c.value
                    t.setdefault('spi',       {})['enabled']      = pc_w_t_spi.value
                    mc_cfg = t.setdefault('media_cleanup', {})
                    mc_cfg['enabled']          = pc_w_t_mc_en.value
                    mc_cfg['max_age_minutes']  = to_int(pc_w_t_mc_age.value, 30)
                    mc_cfg['interval_minutes'] = to_int(pc_w_t_mc_interval.value, 5)

                    # heartbeat
                    data.setdefault('heartbeat', {})['enabled']  = pc_w_hb_en.value
                    data['heartbeat']['interval']                  = to_int(pc_w_hb_interval.value, 30)

                    # devices
                    data.setdefault('devices', {})['enabled']     = pc_w_dev_en.value
                    data['devices']['monitor_usb']                 = pc_w_dev_usb.value

                    # voice
                    data.setdefault('voice', {})['echo_transcription'] = pc_w_voice_echo.value

                    try:
                        save_picoclaw_config(data)
                        save_picoclaw_security(sec)
                        ui.notify(T['notify_saved'], type='positive')
                        return True
                    except Exception as e:
                        ui.notify(T['notify_save_fail'].format(e), type='negative')
                        return False

                def pc_do_save_restart():
                    """Save locally, deploy to /var/lib/picoclaw, then restart picoclaw.service."""
                    if not pc_collect_and_save():
                        return
                    ok_cfg, err_cfg = deploy_picoclaw_config()
                    ok_sec, err_sec = deploy_picoclaw_security()
                    deploy_errs = [e for e in [err_cfg, err_sec] if e]
                    if deploy_errs:
                        ui.notify(f'⚠️ Deploy failed: {"; ".join(deploy_errs)}', type='warning')
                        return
                    ok_svc, svc_err = restart_picoclaw_service()
                    if ok_svc:
                        ui.notify('✅ PicoClaw deployed & restarted', type='positive')
                    else:
                        ui.notify(f'⚠️ Restart failed: {svc_err or T["notify_sudo_required"]}', type='warning')

                # ── Config version badge ───────────────────────────────────
                _pc_cfg_ver = pc_conf.get('version', 0)
                _pc_bld_ver = pc_build.get('version', '')
                with ui.row().classes('items-center gap-2 q-px-sm q-pt-xs q-pb-none'):
                    _badge_color = 'green-7' if _pc_cfg_ver >= 3 else 'orange-7'
                    ui.badge(f'config v{_pc_cfg_ver}', color=_badge_color).props('outline')
                    if _pc_bld_ver:
                        ui.label(f'build {_pc_bld_ver}').classes('text-caption text-grey-5')

                # ── Upgrade banner (shown when config is below current version) ──
                if _pc_cfg_ver < 3:
                    _UPGRADE_SCRIPT = os.path.join(SCRIPT_DIR, 'scripts', 'upgrade_picoclaw_config.py')
                    with ui.card().classes('w-full q-pa-sm q-mb-sm bg-orange-1'):
                        with ui.row().classes('items-center gap-2'):
                            ui.icon('warning', color='orange-8').classes('text-h6')
                            ui.label(
                                f'Config is version {_pc_cfg_ver} — PicoClaw requires version 3. '
                                'Click Upgrade to convert the local config file in-place '
                                '(a timestamped .bak copy is kept automatically).'
                            ).classes('text-caption text-orange-9')
                        _upg_log = ui.textarea('').classes('w-full text-caption font-mono').props(
                            'outlined readonly rows=6 label="Upgrade output"')
                        _upg_log.set_visibility(False)

                        async def _run_pc_cfg_upgrade(_script=_UPGRADE_SCRIPT,
                                                      _path=PICOCLAW_CONFIG_PATH):
                            import asyncio as _aio
                            _upg_log.set_visibility(True)
                            _upg_log.set_value('⏳ Running upgrade script…\n')
                            _upg_btn.props('disabled loading')
                            try:
                                proc = await _aio.create_subprocess_exec(
                                    'python3', _script, _path, '--verbose',
                                    stdout=_aio.subprocess.PIPE,
                                    stderr=_aio.subprocess.STDOUT,
                                )
                                buf = '⏳ Running upgrade script…\n'
                                async for line in proc.stdout:
                                    buf += line.decode(errors='replace')
                                    _upg_log.set_value(buf)
                                await proc.wait()
                                if proc.returncode == 0:
                                    _upg_log.set_value(
                                        buf + f'\n✅ Done.\n'
                                              f'   Upgraded : {_path}\n'
                                              f'   Backup   : {_path}.bak.<timestamp>'
                                    )
                                    ui.notify('✅ Config upgraded — reloading page…',
                                              type='positive')
                                    ui.navigate.reload()
                                else:
                                    _upg_log.set_value(buf + '\n⚠️ Script exited with error.')
                                    ui.notify('⚠️ Upgrade script returned an error',
                                              type='warning')
                            except Exception as _ex:
                                _upg_log.set_value(f'❌ {_ex}')
                                ui.notify(f'❌ {_ex}', type='negative')
                            finally:
                                _upg_btn.props(remove='disabled loading')

                        _upg_btn = ui.button(
                            'Upgrade config to v3', icon='upgrade',
                            on_click=_run_pc_cfg_upgrade,
                        ).props('color=orange-8 size=sm')

                with ui.tabs().classes('w-full bg-purple-1') as pc_cfg_tabs:
                    t_pc_gen   = ui.tab(T['pc_tab_general'],  icon='tune')
                    t_pc_models= ui.tab(T['pc_tab_models'],   icon='cloud')
                    t_pc_ch    = ui.tab(T['pc_tab_channels'], icon='forum')
                    t_pc_tools = ui.tab(T['pc_tab_tools'],    icon='construction')
                    t_pc_sys   = ui.tab(T['pc_tab_system'],   icon='computer')

                with ui.tab_panels(pc_cfg_tabs, value=t_pc_gen).classes('w-full'):

                    # ── General ──────────────────────────────────────────────
                    with ui.tab_panel(t_pc_gen):
                        ui.label(T['pc_section_agent_def']).classes('text-subtitle2 text-grey-7 q-mt-sm')
                        pc_w_workspace          = ui.input('workspace', value=str(pc_agents.get('workspace','/var/lib/picoclaw/.picoclaw/workspace'))).classes('w-full')
                        pc_w_restrict           = ui.checkbox('restrict_to_workspace',       value=bool(pc_agents.get('restrict_to_workspace', False)))
                        pc_w_allow_read_outside = ui.checkbox('allow_read_outside_workspace', value=bool(pc_agents.get('allow_read_outside_workspace', False)))

                        _pc_cur_prov  = str(pc_agents.get('provider', 'qwen'))
                        _pc_prov_opts = list(_pc_ph_provs)
                        if _pc_cur_prov and _pc_cur_prov not in _pc_prov_opts:
                            _pc_prov_opts = [_pc_cur_prov] + _pc_prov_opts
                        pc_w_provider = ui.select(
                            options=_pc_prov_opts,
                            label='provider',
                            value=_pc_cur_prov or (_pc_prov_opts[0] if _pc_prov_opts else None),
                            with_input=True,
                            new_value_mode='add-unique',
                        ).classes('w-full')

                        _pc_cur_mname = str(pc_agents.get('model_name', 'qwen3.5-plus'))
                        _pc_init_mnames = _pc_ph_pid_models.get(_pc_cur_prov, list(_pc_ph_map.keys()))
                        _pc_mname_opts  = list(_pc_init_mnames) if _pc_cur_mname in _pc_init_mnames \
                            else ([_pc_cur_mname] + list(_pc_init_mnames))
                        pc_w_model_name = ui.select(
                            options=_pc_mname_opts,
                            label='model_name',
                            value=_pc_cur_mname,
                            with_input=True,
                            new_value_mode='add-unique',
                        ).classes('w-full')

                        # Auto-fill: provider change → update model_name list
                        def _pc_gen_prov_change(e):
                            prov = e.value or ''
                            mnames = _pc_ph_pid_models.get(prov, list(_pc_ph_map.keys()))
                            cur = pc_w_model_name.value
                            new_val = cur if cur in mnames else (mnames[0] if mnames else cur)
                            pc_w_model_name.set_options(list(mnames), value=new_val)
                        pc_w_provider.on_value_change(_pc_gen_prov_change)
                        pc_w_max_tokens         = ui.number('max_tokens',                 value=pc_agents.get('max_tokens', 8192),  min=256,  step=512).classes('w-full')
                        pc_w_max_iter           = ui.number('max_tool_iterations',         value=pc_agents.get('max_tool_iterations', 50), min=1, step=5).classes('w-full')
                        pc_w_sum_threshold      = ui.number('summarize_message_threshold', value=pc_agents.get('summarize_message_threshold', 20), min=1, step=1).classes('w-full')
                        pc_w_sum_percent        = ui.number('summarize_token_percent',     value=pc_agents.get('summarize_token_percent', 75), min=1, max=100, step=5).classes('w-full')

                        ui.separator().classes('q-my-sm')
                        ui.label(T['pc_section_gateway']).classes('text-subtitle2 text-grey-7')
                        pc_w_gw_host = ui.input('host', value=str(pc_gateway.get('host','0.0.0.0'))).classes('w-full')
                        pc_w_gw_port = ui.number('port', value=pc_gateway.get('port', 18790), min=1024, max=65535, step=1).classes('w-full')

                    # ── Models ───────────────────────────────────────────────
                    with ui.tab_panel(t_pc_models):
                        ui.label(T['pc_section_models']).classes('text-subtitle2 text-grey-7 q-mt-sm')
                        ui.label(T['pc_hint_models']).classes('text-caption text-grey-5')
                        # ── model selector ───────────────────────────────────
                        with ui.row().classes('w-full items-center gap-2 q-mb-sm'):
                            _pc_sel_opts = [_make_pc_sel_opt(i, e.get('model_name', ''))
                                            for i, e in enumerate(pc_model_list)]
                            _pc_model_sel = ui.select(
                                options=_pc_sel_opts,
                                label='Select model to edit',
                                value=_pc_sel_opts[0] if _pc_sel_opts else None,
                            ).classes('flex-1')
                            _pc_sel_ref[0] = _pc_model_sel
                        pc_model_container = ui.column().classes('w-full')
                        for i, entry in enumerate(pc_model_list):
                            pc_build_model_card(pc_model_container, i, entry)
                        def _pc_show_model(target_idx):
                            for _i, _c in pc_model_cards.items():
                                _c.set_visibility(_i == target_idx)
                        _pc_show_ref[0] = _pc_show_model
                        def _on_pc_sel(e):
                            t = _pc_idx_from_opt(e.value)
                            if t is not None: _pc_show_model(t)
                        _pc_model_sel.on_value_change(_on_pc_sel)
                        if pc_model_list:
                            _pc_show_model(0)
                        ui.separator().classes('q-my-sm')
                        def _pc_add_model():
                            new_idx = max(pc_model_panels.keys(), default=-1) + 1
                            pc_build_model_card(pc_model_container, new_idx, {})
                            new_opt = _make_pc_sel_opt(new_idx, '')
                            _pc_model_sel.set_options(list(_pc_model_sel.options) + [new_opt], value=new_opt)
                            _pc_show_model(new_idx)
                        ui.button(T['pc_btn_add_model'], on_click=_pc_add_model).props('outline color=purple')

                    # ── Channels ─────────────────────────────────────────────
                    with ui.tab_panel(t_pc_ch):
                        ui.label(T['pc_section_channels']).classes('text-subtitle2 text-grey-7 q-mt-sm')

                        def _pico_ch(name): return pc_channels.get(name, {})

                        with ui.expansion('🔵 Pico (native)', icon='wifi').classes('w-full'):
                            pc_w_ch_pico_en      = ui.checkbox('enabled',         value=bool(_pico_ch('pico').get('enabled', True)))
                            pc_w_ch_pico_token   = ui.input('token (→ security.yml)', value=str(pc_sec_channels.get('pico',{}).get('token',''))).classes('w-full')
                            pc_w_ch_pico_ping    = ui.number('ping_interval',     value=_pico_ch('pico').get('ping_interval', 30),   min=5,  step=5).classes('w-full')
                            pc_w_ch_pico_maxconn = ui.number('max_connections',   value=_pico_ch('pico').get('max_connections', 100), min=1, step=10).classes('w-full')

                        with ui.expansion('📱 QQ', icon='chat').classes('w-full'):
                            pc_w_ch_qq_en     = ui.checkbox('enabled',    value=bool(_pico_ch('qq').get('enabled', False)))
                            pc_w_ch_qq_appid  = ui.input('app_id',        value=str(_pico_ch('qq').get('app_id',''))).classes('w-full')
                            pc_w_ch_qq_secret = ui.input('app_secret (→ security.yml)', value=str(pc_sec_channels.get('qq',{}).get('app_secret','')),
                                password=True, password_toggle_button=True).classes('w-full')

                        with ui.expansion('✈️ Telegram', icon='send').classes('w-full'):
                            pc_w_ch_tg_en    = ui.checkbox('enabled', value=bool(_pico_ch('telegram').get('enabled', False)))
                            pc_w_ch_tg_token = ui.input('token (→ security.yml)', value=str(pc_sec_channels.get('telegram',{}).get('token','')),
                                password=True, password_toggle_button=True).classes('w-full')
                            pc_w_ch_tg_base  = ui.input('base_url',   value=str(_pico_ch('telegram').get('base_url',''))).classes('w-full')
                            pc_w_ch_tg_proxy = ui.input('proxy',      value=str(_pico_ch('telegram').get('proxy',''))).classes('w-full')

                        with ui.expansion('🎮 Discord', icon='discord').classes('w-full'):
                            pc_w_ch_dc_en      = ui.checkbox('enabled',      value=bool(_pico_ch('discord').get('enabled', False)))
                            pc_w_ch_dc_token   = ui.input('token (→ security.yml)', value=str(pc_sec_channels.get('discord',{}).get('token','')),
                                password=True, password_toggle_button=True).classes('w-full')
                            pc_w_ch_dc_mention = ui.checkbox('mention_only', value=bool(_pico_ch('discord').get('mention_only', False)))

                        with ui.expansion('💬 WhatsApp', icon='smartphone').classes('w-full'):
                            pc_w_ch_wa_en     = ui.checkbox('enabled',    value=bool(_pico_ch('whatsapp').get('enabled', False)))
                            pc_w_ch_wa_url    = ui.input('bridge_url',    value=str(_pico_ch('whatsapp').get('bridge_url','ws://localhost:3001'))).classes('w-full')
                            pc_w_ch_wa_native = ui.checkbox('use_native', value=bool(_pico_ch('whatsapp').get('use_native', False)))

                        with ui.expansion('🪶 Feishu / Lark', icon='language').classes('w-full'):
                            pc_w_ch_fs_en      = ui.checkbox('enabled',              value=bool(_pico_ch('feishu').get('enabled', False)))
                            pc_w_ch_fs_appid   = ui.input('app_id',                  value=str(_pico_ch('feishu').get('app_id',''))).classes('w-full')
                            pc_w_ch_fs_secret  = ui.input('app_secret (→ security.yml)', value=str(pc_sec_channels.get('feishu',{}).get('app_secret','')),
                                password=True, password_toggle_button=True).classes('w-full')
                            pc_w_ch_fs_encrypt = ui.input('encrypt_key (→ security.yml)', value=str(pc_sec_channels.get('feishu',{}).get('encrypt_key','')),
                                password=True, password_toggle_button=True).classes('w-full')
                            pc_w_ch_fs_verify  = ui.input('verification_token (→ security.yml)', value=str(pc_sec_channels.get('feishu',{}).get('verification_token',''))).classes('w-full')

                        with ui.expansion('💼 Slack', icon='workspaces').classes('w-full'):
                            pc_w_ch_sl_en  = ui.checkbox('enabled',    value=bool(_pico_ch('slack').get('enabled', False)))
                            pc_w_ch_sl_bot = ui.input('bot_token (→ security.yml)', value=str(pc_sec_channels.get('slack',{}).get('bot_token','')),
                                password=True, password_toggle_button=True).classes('w-full')
                            pc_w_ch_sl_app = ui.input('app_token (→ security.yml)', value=str(pc_sec_channels.get('slack',{}).get('app_token','')),
                                password=True, password_toggle_button=True).classes('w-full')

                        with ui.expansion('🔷 Matrix', icon='grid_on').classes('w-full'):
                            pc_w_ch_mx_en    = ui.checkbox('enabled',      value=bool(_pico_ch('matrix').get('enabled', False)))
                            pc_w_ch_mx_home  = ui.input('homeserver',      value=str(_pico_ch('matrix').get('homeserver','https://matrix.org'))).classes('w-full')
                            pc_w_ch_mx_user  = ui.input('user_id',         value=str(_pico_ch('matrix').get('user_id',''))).classes('w-full')
                            pc_w_ch_mx_token = ui.input('access_token (→ security.yml)', value=str(pc_sec_channels.get('matrix',{}).get('access_token','')),
                                password=True, password_toggle_button=True).classes('w-full')

                        with ui.expansion('🔔 DingTalk', icon='notifications').classes('w-full'):
                            pc_w_ch_dt_en     = ui.checkbox('enabled',       value=bool(_pico_ch('dingtalk').get('enabled', False)))
                            pc_w_ch_dt_id     = ui.input('client_id',        value=str(_pico_ch('dingtalk').get('client_id',''))).classes('w-full')
                            pc_w_ch_dt_secret = ui.input('client_secret (→ security.yml)', value=str(pc_sec_channels.get('dingtalk',{}).get('client_secret','')),
                                password=True, password_toggle_button=True).classes('w-full')

                        with ui.expansion('📷 MaixCam', icon='videocam').classes('w-full'):
                            pc_w_ch_mc_en   = ui.checkbox('enabled', value=bool(_pico_ch('maixcam').get('enabled', False)))
                            pc_w_ch_mc_host = ui.input('host',        value=str(_pico_ch('maixcam').get('host','0.0.0.0'))).classes('w-full')
                            pc_w_ch_mc_port = ui.number('port',       value=_pico_ch('maixcam').get('port', 18790), min=1024, max=65535, step=1).classes('w-full')

                        with ui.expansion('💬 IRC', icon='terminal').classes('w-full'):
                            pc_w_ch_irc_en     = ui.checkbox('enabled', value=bool(_pico_ch('irc').get('enabled', False)))
                            pc_w_ch_irc_server = ui.input('server',     value=str(_pico_ch('irc').get('server',''))).classes('w-full')
                            pc_w_ch_irc_nick   = ui.input('nick',       value=str(_pico_ch('irc').get('nick',''))).classes('w-full')
                            pc_w_ch_irc_tls    = ui.checkbox('tls',     value=bool(_pico_ch('irc').get('tls', False)))

                        with ui.expansion('🤖 OneBot', icon='smart_toy').classes('w-full'):
                            pc_w_ch_ob_en    = ui.checkbox('enabled',      value=bool(_pico_ch('onebot').get('enabled', False)))
                            pc_w_ch_ob_ws    = ui.input('ws_url',          value=str(_pico_ch('onebot').get('ws_url','ws://127.0.0.1:3001'))).classes('w-full')
                            pc_w_ch_ob_token = ui.input('access_token (→ security.yml)', value=str(pc_sec_channels.get('onebot',{}).get('access_token','')),
                                password=True, password_toggle_button=True).classes('w-full')

                        with ui.expansion('🟢 LINE', icon='message').classes('w-full'):
                            pc_w_ch_line_en     = ui.checkbox('enabled',               value=bool(_pico_ch('line').get('enabled', False)))
                            pc_w_ch_line_secret = ui.input('channel_secret (→ security.yml)', value=str(pc_sec_channels.get('line',{}).get('channel_secret','')),
                                password=True, password_toggle_button=True).classes('w-full')
                            pc_w_ch_line_cat    = ui.input('channel_access_token (→ security.yml)', value=str(pc_sec_channels.get('line',{}).get('channel_access_token','')),
                                password=True, password_toggle_button=True).classes('w-full')

                        with ui.expansion('🏢 WeCom', icon='business').classes('w-full'):
                            pc_w_ch_wc_en    = ui.checkbox('enabled',          value=bool(_pico_ch('wecom').get('enabled', False)))
                            pc_w_ch_wc_token = ui.input('token (→ security.yml)', value=str(pc_sec_channels.get('wecom',{}).get('token','')),
                                password=True, password_toggle_button=True).classes('w-full')
                            pc_w_ch_wc_aes   = ui.input('encoding_aes_key (→ security.yml)', value=str(pc_sec_channels.get('wecom',{}).get('encoding_aes_key','')),
                                password=True, password_toggle_button=True).classes('w-full')

                    # ── Tools ────────────────────────────────────────────────
                    with ui.tab_panel(t_pc_tools):
                        ui.label(T['pc_section_tools']).classes('text-subtitle2 text-grey-7 q-mt-sm')
                        ui.label(T['pc_lbl_read_paths']).classes('text-caption text-grey-6')
                        _rp = pc_tools.get('allow_read_paths') or []
                        pc_w_t_read_paths  = ui.textarea(value='\n'.join(_rp)).classes('w-full').props('outlined rows=3')
                        ui.label(T['pc_lbl_write_paths']).classes('text-caption text-grey-6')
                        _wp = pc_tools.get('allow_write_paths') or []
                        pc_w_t_write_paths = ui.textarea(value='\n'.join(_wp)).classes('w-full').props('outlined rows=3')

                        ui.separator().classes('q-my-sm')
                        with ui.expansion(T['pc_exp_web_search'], icon='search').classes('w-full'):
                            pc_w_t_web_en      = ui.checkbox('web.enabled',        value=bool(pc_web_tools.get('enabled', True)))
                            pc_w_t_fetch_limit = ui.number('fetch_limit_bytes',    value=pc_web_tools.get('fetch_limit_bytes', 10485760), min=1024, step=1048576).classes('w-full')
                            pc_w_t_ddg         = ui.checkbox('duckduckgo.enabled', value=bool(pc_web_tools.get('duckduckgo',{}).get('enabled', True)))
                            pc_w_t_brave       = ui.checkbox('brave.enabled',      value=bool(pc_web_tools.get('brave',{}).get('enabled', False)))
                            pc_w_t_brave_key   = ui.input('brave api_keys[0] (-> security.yml)',
                                value=str((pc_sec_web.get('brave',{}).get('api_keys',[]) or [''])[0]),
                                password=True, password_toggle_button=True).classes('w-full')
                            pc_w_t_tavily      = ui.checkbox('tavily.enabled',     value=bool(pc_web_tools.get('tavily',{}).get('enabled', False)))
                            pc_w_t_tavily_key  = ui.input('tavily api_keys[0] (-> security.yml)',
                                value=str((pc_sec_web.get('tavily',{}).get('api_keys',[]) or [''])[0]),
                                password=True, password_toggle_button=True).classes('w-full')
                            pc_w_t_perp        = ui.checkbox('perplexity.enabled', value=bool(pc_web_tools.get('perplexity',{}).get('enabled', False)))
                            pc_w_t_perp_key    = ui.input('perplexity api_keys[0] (-> security.yml)',
                                value=str((pc_sec_web.get('perplexity',{}).get('api_keys',[]) or [''])[0]),
                                password=True, password_toggle_button=True).classes('w-full')
                            pc_w_t_searxng     = ui.checkbox('searxng.enabled',    value=bool(pc_web_tools.get('searxng',{}).get('enabled', False)))
                            pc_w_t_searxng_url = ui.input('searxng.base_url',      value=str(pc_web_tools.get('searxng',{}).get('base_url',''))).classes('w-full')

                        with ui.expansion(T['pc_exp_exec'], icon='terminal').classes('w-full'):
                            pc_t_exec = pc_tools.get('exec', {})
                            pc_w_t_exec         = ui.checkbox('exec.enabled',       value=bool(pc_t_exec.get('enabled', True)))
                            pc_w_t_exec_remote  = ui.checkbox('allow_remote',       value=bool(pc_t_exec.get('allow_remote', True)))
                            pc_w_t_exec_timeout = ui.number('timeout_seconds',      value=pc_t_exec.get('timeout_seconds', 60), min=5, step=5).classes('w-full')

                        with ui.expansion(T['pc_exp_cron'], icon='schedule').classes('w-full'):
                            pc_t_cron = pc_tools.get('cron', {})
                            pc_w_t_cron     = ui.checkbox('cron.enabled',     value=bool(pc_t_cron.get('enabled', True)))
                            pc_w_t_cron_cmd = ui.checkbox('allow_command',    value=bool(pc_t_cron.get('allow_command', True)))

                        with ui.expansion(T['pc_exp_skills_mcp'], icon='hub').classes('w-full'):
                            pc_w_t_skills  = ui.checkbox('skills.clawhub.enabled',  value=bool(pc_tools.get('skills',{}).get('registries',{}).get('clawhub',{}).get('enabled', True)))
                            pc_w_t_mcp     = ui.checkbox('mcp.enabled',             value=bool(pc_tools.get('mcp',{}).get('enabled', False)))

                        with ui.expansion('🔑 Skills Auth Tokens (-> security.yml)', icon='key').classes('w-full'):
                            ui.label('clawhub.auth_token').classes('text-caption text-grey-6')
                            pc_w_t_clawhub_auth = ui.input('clawhub auth_token',
                                value=str(pc_sec_skills.get('clawhub',{}).get('auth_token','')),
                                password=True, password_toggle_button=True).classes('w-full')
                            ui.label('github.token').classes('text-caption text-grey-6')
                            pc_w_t_github_token = ui.input('github token',
                                value=str(pc_sec_skills.get('github',{}).get('token','')),
                                password=True, password_toggle_button=True).classes('w-full')

                        with ui.expansion(T['pc_exp_file_tools'], icon='folder').classes('w-full'):
                            pc_w_t_read_file   = ui.checkbox('read_file.enabled',   value=bool(pc_tools.get('read_file',{}).get('enabled', True)))
                            pc_w_t_read_max    = ui.number('read_file.max_read_file_size', value=pc_tools.get('read_file',{}).get('max_read_file_size', 65536), min=1024, step=4096).classes('w-full')
                            pc_w_t_write_file  = ui.checkbox('write_file.enabled',  value=bool(pc_tools.get('write_file',{}).get('enabled', True)))
                            pc_w_t_edit_file   = ui.checkbox('edit_file.enabled',   value=bool(pc_tools.get('edit_file',{}).get('enabled', True)))
                            pc_w_t_append_file = ui.checkbox('append_file.enabled', value=bool(pc_tools.get('append_file',{}).get('enabled', True)))
                            pc_w_t_list_dir    = ui.checkbox('list_dir.enabled',    value=bool(pc_tools.get('list_dir',{}).get('enabled', True)))
                            pc_w_t_send_file   = ui.checkbox('send_file.enabled',   value=bool(pc_tools.get('send_file',{}).get('enabled', True)))
                            pc_w_t_message     = ui.checkbox('message.enabled',     value=bool(pc_tools.get('message',{}).get('enabled', True)))
                            pc_w_t_web_fetch   = ui.checkbox('web_fetch.enabled',   value=bool(pc_tools.get('web_fetch',{}).get('enabled', True)))
                            pc_w_t_spawn       = ui.checkbox('spawn.enabled',       value=bool(pc_tools.get('spawn',{}).get('enabled', True)))
                            pc_w_t_subagent    = ui.checkbox('subagent.enabled',    value=bool(pc_tools.get('subagent',{}).get('enabled', True)))
                            pc_w_t_i2c         = ui.checkbox('i2c.enabled',         value=bool(pc_tools.get('i2c',{}).get('enabled', False)))
                            pc_w_t_spi         = ui.checkbox('spi.enabled',         value=bool(pc_tools.get('spi',{}).get('enabled', False)))

                        with ui.expansion(T['pc_exp_media_cleanup'], icon='cleaning_services').classes('w-full'):
                            pc_t_mc = pc_tools.get('media_cleanup', {})
                            pc_w_t_mc_en       = ui.checkbox('enabled',          value=bool(pc_t_mc.get('enabled', True)))
                            pc_w_t_mc_age      = ui.number('max_age_minutes',    value=pc_t_mc.get('max_age_minutes', 30),   min=1, step=5).classes('w-full')
                            pc_w_t_mc_interval = ui.number('interval_minutes',   value=pc_t_mc.get('interval_minutes', 5),   min=1, step=1).classes('w-full')

                    # ── System ───────────────────────────────────────────────
                    with ui.tab_panel(t_pc_sys):
                        ui.label(T['pc_section_heartbeat']).classes('text-subtitle2 text-grey-7 q-mt-sm')
                        pc_w_hb_en       = ui.checkbox('heartbeat.enabled',  value=bool(pc_heartbeat.get('enabled', True)))
                        pc_w_hb_interval = ui.number('interval (secs)',       value=pc_heartbeat.get('interval', 30), min=5, step=5).classes('w-full')

                        ui.separator().classes('q-my-sm')
                        ui.label(T['pc_section_devices']).classes('text-subtitle2 text-grey-7')
                        pc_w_dev_en  = ui.checkbox('devices.enabled',     value=bool(pc_devices.get('enabled', False)))
                        pc_w_dev_usb = ui.checkbox('monitor_usb',         value=bool(pc_devices.get('monitor_usb', True)))

                        ui.separator().classes('q-my-sm')
                        ui.label(T['pc_section_voice']).classes('text-subtitle2 text-grey-7')
                        pc_w_voice_echo = ui.checkbox('echo_transcription', value=bool(pc_voice.get('echo_transcription', False)))

                        ui.separator().classes('q-my-sm')
                        ui.label(T['pc_section_build']).classes('text-subtitle2 text-grey-7')
                        with ui.card().classes('w-full bg-grey-2 q-pa-sm'):
                            for k, v in pc_build.items():
                                ui.label(f'{k}: {v}').classes('text-caption text-mono')

                ui.separator()
                with ui.row().classes('w-full gap-2 q-pa-sm'):
                    ui.button(T['pc_btn_save'],         on_click=pc_collect_and_save).props('elevated').classes('flex-1 bg-purple-8 text-white')
                    ui.button(T['pc_btn_save_restart'],  on_click=pc_do_save_restart).props('elevated').classes('flex-1 bg-deep-purple-8 text-white')

            # ── PicoClaw › Pair Device ─────────────────────────────────────
            with ui.tab_panel(t_pc_pair):
                with ui.card().classes('w-full q-pa-md'):
                    ui.label(T['pc_pair_title']).classes('text-h6 text-purple-8')
                    ui.label(T['pc_pair_hint']).classes('text-caption text-grey-6 q-mt-xs')

                    def _run_pico_setup_for_pair():
                        ok, msg = setup_pico_channel_token()
                        if ok:
                            ui.notify(T['pc_pair_setup_ok'], type='positive')
                            ui.timer(0.8, lambda: ui.navigate.reload(), once=True)
                        else:
                            ui.notify(f"{T['pc_pair_setup_fail']}: {msg}", type='warning')

                    # Pico auth token comes directly from .security.yml.
                    _sec_tok  = ''
                    _sec_err  = ''
                    _sec_tok, _sec_err = _read_security_yml_token('pico')
                    pico_token = _sec_tok
                    pico_port = int(pc_gateway.get('port', 18790) or 18790)
                    pico_host = _get_lan_ip() or request.url.hostname or 'localhost'
                    pico_scheme = request.url.scheme or 'http'
                    pico_url = f'{pico_scheme}://{pico_host}:{pico_port}?token={pico_token}' if pico_token else ''
                    pico_qr_url = f'https://quickchart.io/qr?size=260&margin=1&text={quote(pico_url, safe="")}' if pico_url else ''

                    if _sec_err:
                        ui.label(f'⚠️ {_sec_err}').classes('text-warning text-caption q-mt-xs')
                        ui.label('Run pico setup to generate token, then reload this page.').classes('text-warning text-caption')
                    if pico_token:
                        ui.input('Pico token  (.security.yml: channel_list.pico.settings.token)', value=pico_token).props('readonly').classes('w-full q-mt-sm')
                        ui.input(T['pc_pair_url'], value=pico_url).props('readonly').classes('w-full')

                        with ui.row().classes('w-full items-start gap-4 q-mt-sm'):
                            with ui.card().classes('q-pa-sm items-center bg-white'):
                                ui.label(T['pc_pair_qr']).classes('text-caption text-grey-7 q-mb-xs')
                                ui.image(pico_qr_url).classes('w-56 h-56')

                            with ui.column().classes('gap-2 q-mt-md'):
                                def _copy_pico_url(url=pico_url):
                                    ui.clipboard.write(url)
                                    ui.notify(T['pc_pair_copy_ok'], type='positive')

                                def _show_pico_qr(url=pico_url, token=pico_token):
                                    try:
                                        import importlib
                                        import clawberry_paircode as _cp
                                        importlib.reload(_cp)
                                        _cp.request_picoclaw_qr_display(url, token)
                                        ui.notify(T['pc_pair_queue_ok'], type='positive')
                                    except Exception as exc:
                                        ui.notify(f'❌ {exc}', type='negative')

                                ui.button(T['pc_pair_copy_url'], on_click=_copy_pico_url).props('outline color=purple-8')
                                ui.button(T['pc_pair_show_display'], on_click=_show_pico_qr).props('elevated color=purple-8')
                    else:
                        ui.label(T['pc_pair_missing_token']).classes('text-negative q-mt-sm')
                        ui.button(T['pc_pair_btn_setup'], on_click=_run_pico_setup_for_pair).props('elevated color=purple-8').classes('q-mt-sm')

            # ── PicoClaw › Characters ──────────────────────────────────────
            with ui.tab_panel(t_pc_char):
                _build_character_tab('/var/lib/picoclaw/.picoclaw/workspace', 'picoclaw', 'purple-8')

            # ── PicoClaw › Skills ─────────────────────────────────────────
            with ui.tab_panel(t_pc_skills):
                _build_skills_tab('/var/lib/picoclaw/.picoclaw/workspace', 'picoclaw', 'purple-8')

    # ══ OpenClaw Dashboard ════════════════════════════════════════════════════
    oc_content = ui.column().classes('w-full q-px-sm q-pt-sm')
    oc_content.set_visibility(False)
    with oc_content:
        with ui.row().classes('w-full items-center q-mb-xs'):
            ui.label(T['oc_dashboard']).classes('text-h6 text-teal-8')
            ui.button(icon='refresh', on_click=lambda: ui.navigate.reload()) \
                .props('flat round dense color=teal-8').tooltip(T['tooltip_reload'])

        # ── Service disabled banner ──────────────────────────────────────
        _oc_svc_enabled = openclaw_service_is_enabled()
        oc_disabled_banner = ui.card().classes('w-full q-pa-md q-mb-sm bg-orange-1')
        oc_disabled_banner.set_visibility(not _oc_svc_enabled)
        with oc_disabled_banner:
            ui.label('⚠️ OpenClaw service is not enabled').classes('text-subtitle2 text-orange-9')
            ui.label(
                'The openclaw.service is currently disabled. '
                'Enable and start it to use the OpenClaw gateway and dashboard.'
            ).classes('text-caption text-grey-7 q-mb-sm')
            oc_enable_result = ui.label('').classes('text-caption q-mb-xs')

            def _oc_enable_service():
                ok, err = enable_openclaw_user_service()
                if ok:
                    oc_enable_result.set_text('✅ Service enabled and started — reloading…')
                    oc_enable_result.classes(remove='text-negative', add='text-positive')
                    ui.notify('✅ openclaw.service enabled & started', type='positive')
                    ui.timer(1.5, lambda: ui.navigate.reload(), once=True)
                else:
                    msg = f'❌ Enable failed: {err}'
                    oc_enable_result.set_text(msg)
                    oc_enable_result.classes(remove='text-positive', add='text-negative')
                    ui.notify(msg, type='negative')

            ui.button('▶ Enable & Start openclaw.service', on_click=_oc_enable_service) \
                .props('color=orange-9 elevated')

        # ── Main dashboard tabs (shown only when service is enabled) ─────
        with ui.tabs().classes('w-full bg-teal-1') as oc_sub_tabs:
            t_oc_wiz    = ui.tab(T['oc_tab_wizard'],        icon='auto_fix_high')
            t_oc_cfg    = ui.tab(T['oc_tab_configuration'], icon='settings')
            t_oc_pair   = ui.tab(T['oc_tab_pair_device'],   icon='devices')
            t_oc_doctor = ui.tab(T['oc_tab_doctor'],         icon='medical_services')
            t_oc_char   = ui.tab(T['tab_characters'],        icon='face')
            t_oc_skills = ui.tab(T['tab_skills'],            icon='extension')

        oc_sub_tabs.set_visibility(_oc_svc_enabled)

        with ui.tab_panels(oc_sub_tabs, value=t_oc_wiz).classes('w-full') as oc_tab_panels:
            pass  # populated below

        oc_tab_panels.set_visibility(_oc_svc_enabled)

        with oc_tab_panels:

            # ── OpenClaw › Wizard ──────────────────────────────────────────
            with ui.tab_panel(t_oc_wiz):
                ui.label('🧙 Quick Setup Wizard').classes('text-h6 text-teal-8 q-mb-xs')
                ui.label(
                    'Configure gateway and primary model in a few steps. '
                    'Click Apply — then restart OpenClaw to activate.'
                ).classes('text-caption text-grey-6 q-mb-md')

                _oc_wiz_conf  = load_openclaw_config()
                _oc_wiz_gw    = _oc_wiz_conf.get('gateway', {})
                _oc_wiz_auth  = _oc_wiz_gw.get('auth', {})
                _oc_wiz_ad    = _oc_wiz_conf.get('agents', {}).get('defaults', {})
                _oc_wiz_model = _oc_model_ref_text(_oc_wiz_ad.get('model', ''))
                _oc_wiz_tools = _oc_wiz_conf.get('tools', {})
                _oc_wiz_provider_models = _oc_provider_models(_oc_wiz_conf)

                with ui.stepper(value='oc_wiz_gw').props('vertical animated').classes('w-full') as _oc_wiz:

                    # ── Step 1: Gateway ─────────────────────────────────────
                    with ui.step('oc_wiz_gw', title='1  Gateway', icon='router'):
                        ui.label('Set how OpenClaw listens and authenticates.').classes('text-caption text-grey-6 q-mb-sm')

                        _oc_wiz_bind_opts = ['loopback', 'lan', 'tailnet', 'auto']
                        _oc_wiz_cur_bind  = str(_oc_wiz_gw.get('bind', 'lan'))
                        oc_wiz_bind = ui.select(_oc_wiz_bind_opts, label='gateway.bind',
                            value=_oc_wiz_cur_bind if _oc_wiz_cur_bind in _oc_wiz_bind_opts else 'lan').classes('w-full q-mb-sm')

                        oc_wiz_port = ui.number('gateway.port',
                            value=int(_oc_wiz_gw.get('port', 18789) or 18789),
                            min=1024, max=65535, step=1).classes('w-full q-mb-sm')

                        _oc_wiz_authmode_opts = ['token', 'password', 'none']
                        _oc_wiz_cur_am = str(_oc_wiz_auth.get('mode', 'token'))
                        oc_wiz_auth_mode = ui.select(_oc_wiz_authmode_opts, label='gateway.auth.mode',
                            value=_oc_wiz_cur_am if _oc_wiz_cur_am in _oc_wiz_authmode_opts else 'token').classes('w-full q-mb-sm')

                        oc_wiz_token = ui.input('gateway.auth.token',
                            value=str(_oc_wiz_auth.get('token', '')),
                            password=True, password_toggle_button=True).classes('w-full q-mb-sm')

                        def _oc_wiz_read_live():
                            tok, err = _read_openclaw_deploy_token()
                            if tok:
                                oc_wiz_token.set_value(tok)
                                ui.notify('✅ Token read from deploy path', type='positive')
                            else:
                                ui.notify(f'⚠️ {err or "Token not found"}', type='warning')
                        ui.button(T['oc_token_live'], on_click=_oc_wiz_read_live).props('outline color=teal-8 size=sm').classes('q-mb-sm')

                        with ui.stepper_navigation():
                            ui.button('Next →', on_click=_oc_wiz.next).props('color=teal-8')

                    # ── Step 2: Model & Provider ────────────────────────────
                    with ui.step('oc_wiz_model', title='2  Provider & Model', icon='cloud'):
                        ui.label('Pick a provider and model for agents.').classes('text-caption text-grey-6 q-mb-sm')

                        # Derive initial values from config (OpenClaw schema)
                        _oc_wiz_init_primary = str(_oc_wiz_model)
                        # Provider is derived from the model reference: "provider/model-id"
                        _oc_wiz_init_prov_key = ''
                        if '/' in _oc_wiz_init_primary:
                            _oc_wiz_init_prov_key = _oc_wiz_init_primary.split('/', 1)[0]
                        _oc_wiz_init_model = ''
                        if '/' in _oc_wiz_init_primary:
                            _oc_wiz_init_model = _oc_wiz_init_primary.split('/', 1)[1]
                        else:
                            _oc_wiz_init_model = _oc_wiz_init_primary
                        # Read existing apiKey/baseUrl from models.providers.<name> (OpenClaw schema)
                        _oc_wiz_init_api_key = str(
                            _oc_wiz_provider_models.get(_oc_wiz_init_prov_key, {}).get('apiKey', ''))
                        _oc_wiz_init_base_url = str(
                            _oc_wiz_provider_models.get(_oc_wiz_init_prov_key, {}).get('baseUrl')
                            or _oc_wiz_provider_models.get(_oc_wiz_init_prov_key, {}).get('base_url')
                            or _oc_ph_pid_base.get(_oc_wiz_init_prov_key, ''))

                        # Build option lists — ensure current values appear
                        _oc_wiz_prov_opts = list(_oc_ph_provs)
                        if _oc_wiz_init_prov_key and _oc_wiz_init_prov_key not in _oc_wiz_prov_opts:
                            _oc_wiz_prov_opts = [_oc_wiz_init_prov_key] + _oc_wiz_prov_opts
                        if not _oc_wiz_prov_opts:
                            _oc_wiz_prov_opts = ['']
                        if _oc_wiz_init_prov_key not in _oc_wiz_prov_opts:
                            _oc_wiz_init_prov_key = _oc_wiz_prov_opts[0]

                        _oc_wiz_mname_opts = list(_oc_ph_map.keys())
                        if _oc_wiz_init_model and _oc_wiz_init_model not in _oc_wiz_mname_opts:
                            _oc_wiz_mname_opts = [_oc_wiz_init_model] + _oc_wiz_mname_opts
                        if not _oc_wiz_mname_opts:
                            _oc_wiz_mname_opts = ['']
                        if _oc_wiz_init_model not in _oc_wiz_mname_opts:
                            _oc_wiz_init_model = _oc_wiz_mname_opts[0]

                        ui.label('⚡ Quick pick').classes('text-caption text-teal-7')
                        oc_wiz_quick = ui.select(
                            options=list(_oc_ph_map.keys()),
                            label='Known model',
                            value=None,
                            clearable=True,
                            with_input=True,
                        ).classes('w-full q-mb-xs')
                        ui.separator().classes('q-my-xs')

                        oc_wiz_prov = ui.select(
                            _oc_wiz_prov_opts,
                            label='provider',
                            value=_oc_wiz_init_prov_key,
                            with_input=True,
                        ).classes('w-full q-mb-sm')

                        oc_wiz_model_primary = ui.input(
                            'agents.defaults.model  (provider/model)',
                            value=_oc_wiz_init_primary,
                        ).classes('w-full q-mb-sm')

                        oc_wiz_api_key = ui.input(
                            'Provider API Key',
                            value=_oc_wiz_init_api_key,
                            password=True, password_toggle_button=True,
                        ).classes('w-full q-mb-sm')

                        oc_wiz_base_url = ui.input(
                            'models.providers.<name>.baseUrl',
                            value=_oc_wiz_init_base_url,
                        ).classes('w-full q-mb-sm')

                        oc_wiz_workspace = ui.input('agents.defaults.workspace',
                            value=str(_oc_wiz_ad.get('workspace', '/var/lib/openclaw/.openclaw/workspace'))).classes('w-full q-mb-sm')

                        _oc_wiz_tp_opts = ['minimal', 'coding', 'messaging', 'full']
                        _oc_wiz_cur_tp  = str(_oc_wiz_tools.get('profile', 'coding'))
                        oc_wiz_tools_profile = ui.select(_oc_wiz_tp_opts, label='tools.profile',
                            value=_oc_wiz_cur_tp if _oc_wiz_cur_tp in _oc_wiz_tp_opts else 'coding').classes('w-full q-mb-sm')

                        # Quick-pick autofill
                        def _oc_wiz_fill_hint(e):
                            h = _oc_ph_map.get(e.value) if e.value else None
                            if not h: return
                            prov = h.get('provider', '')
                            if prov in _oc_wiz_prov_opts:
                                oc_wiz_prov.set_value(prov)
                            oc_wiz_model_primary.set_value(h.get('primary', h.get('provider', '') + '/' + h.get('model', '')))
                            if h.get('api_base') and not oc_wiz_base_url.value:
                                oc_wiz_base_url.set_value(h.get('api_base'))
                            if h.get('api_key_required', True) is False:
                                oc_wiz_api_key.set_value('')
                        oc_wiz_quick.on_value_change(_oc_wiz_fill_hint)

                        # Provider selection autofill
                        def _oc_wiz_fill_prov(e):
                            prov = e.value or ''
                            if not prov: return
                            models = _oc_ph_pid_models.get(prov, [])
                            if models:
                                first = _oc_ph_map.get(models[0], {})
                                oc_wiz_model_primary.set_value(first.get('primary', prov + '/' + first.get('model', '')))
                                if first.get('api_base') and not oc_wiz_base_url.value:
                                    oc_wiz_base_url.set_value(first.get('api_base'))
                        oc_wiz_prov.on_value_change(_oc_wiz_fill_prov)

                        with ui.stepper_navigation():
                            ui.button('← Back', on_click=_oc_wiz.previous).props('flat color=grey-7')
                            ui.button('Next →', on_click=_oc_wiz.next).props('color=teal-8')

                    # ── Step 3: Apply ───────────────────────────────────────
                    with ui.step('oc_wiz_apply', title='3  Apply', icon='check_circle'):
                        ui.label('Review and apply settings.').classes('text-caption text-grey-6 q-mb-sm')

                        def _oc_wiz_summary():
                            return (
                                f'gateway.bind:            {oc_wiz_bind.value}\n'
                                f'gateway.port:            {int(oc_wiz_port.value or 18789)}\n'
                                f'gateway.auth.mode:       {oc_wiz_auth_mode.value}\n'
                                f'gateway.auth.token:      {"(set)" if oc_wiz_token.value else "(empty)"}\n'
                                f'agents.defaults.model:   {oc_wiz_model_primary.value or "(unchanged)"}\n'
                                f'models.providers.{oc_wiz_prov.value or "?"}.baseUrl:  {oc_wiz_base_url.value or "(provider default)"}\n'
                                f'workspace:               {oc_wiz_workspace.value}\n'
                                f'tools.profile:           {oc_wiz_tools_profile.value}'
                            )
                        oc_wiz_summary_lbl = ui.label('').classes('text-caption text-grey-7 q-mb-sm')

                        def _oc_wiz_refresh_summary():
                            oc_wiz_summary_lbl.set_text(_oc_wiz_summary())
                        _oc_wiz.on('transition', lambda _: _oc_wiz_refresh_summary())

                        def _oc_wiz_apply():
                            data = load_openclaw_config()
                            data.setdefault('gateway', {}).setdefault('auth', {})
                            data['gateway']['bind']       = oc_wiz_bind.value or 'lan'
                            data['gateway']['port']       = int(oc_wiz_port.value or 18789)
                            data['gateway']['auth']['mode']  = oc_wiz_auth_mode.value or 'token'
                            if oc_wiz_token.value:
                                data['gateway']['auth']['token'] = oc_wiz_token.value
                            ad = data.setdefault('agents', {}).setdefault('defaults', {})
                            # OpenClaw does NOT have agents.defaults.provider —
                            # the provider is derived from the provider/model-id format.
                            if oc_wiz_model_primary.value:
                                ad['model'] = oc_wiz_model_primary.value
                            if oc_wiz_workspace.value:
                                ad['workspace'] = oc_wiz_workspace.value
                            data.setdefault('tools', {})['profile'] = oc_wiz_tools_profile.value or 'coding'
                            # Save provider config into models.providers.<name> (OpenClaw schema)
                            _prov = oc_wiz_prov.value or ''
                            if _prov and (oc_wiz_api_key.value or oc_wiz_base_url.value):
                                data.setdefault('models', {}).setdefault('providers', {}).setdefault(_prov, {})
                                if oc_wiz_base_url.value:
                                    data['models']['providers'][_prov]['baseUrl'] = oc_wiz_base_url.value
                                if oc_wiz_api_key.value:
                                    data['models']['providers'][_prov]['apiKey'] = oc_wiz_api_key.value
                            try:
                                save_openclaw_config(data)
                                ok_cfg, err_cfg = deploy_openclaw_config()
                                if not ok_cfg:
                                    ui.notify(f'⚠️ Saved locally but deploy failed: {err_cfg}', type='warning')
                                else:
                                    ui.notify('✅ OpenClaw config saved & deployed — restart OpenClaw to activate', type='positive')
                            except Exception as ex:
                                ui.notify(f'❌ {ex}', type='negative')

                        with ui.stepper_navigation():
                            ui.button('← Back', on_click=_oc_wiz.previous).props('flat color=grey-7')
                            ui.button('✅ Apply & Deploy', on_click=_oc_wiz_apply).props('color=teal-8 elevated')

            # ── OpenClaw › Configuration ───────────────────────────────────
            with ui.tab_panel(t_oc_cfg):
                oc_conf    = load_openclaw_config()
                oc_gateway = oc_conf.get('gateway', {})
                oc_auth    = oc_gateway.get('auth', {})
                oc_agents  = oc_conf.get('agents', {}).get('defaults', {})
                oc_session = oc_conf.get('session', {})
                oc_tools   = oc_conf.get('tools', {})
                oc_ctrl_ui = oc_gateway.get('controlUi', {})
                oc_ts      = oc_gateway.get('tailscale', {})
                oc_provider_models = _oc_provider_models(oc_conf)

                # ── Gateway ──────────────────────────────────────────────
                with ui.card().classes('w-full q-pa-md q-mb-sm'):
                    ui.label(T['oc_section_gateway']).classes('text-subtitle2 text-grey-7 q-mt-sm')

                    _oc_mode_opts = ['local', 'remote']
                    _oc_cur_mode  = str(oc_gateway.get('mode', 'local'))
                    oc_w_mode = ui.select(_oc_mode_opts, label='gateway.mode',
                        value=_oc_cur_mode if _oc_cur_mode in _oc_mode_opts else 'local',
                        with_input=True).classes('w-full')

                    oc_w_port = ui.number('gateway.port', value=int(oc_gateway.get('port', 18789) or 18789),
                        min=1024, max=65535, step=1).classes('w-full')

                    _oc_bind_opts = ['loopback', 'lan', 'tailnet', 'auto']
                    _oc_cur_bind  = str(oc_gateway.get('bind', 'lan'))
                    oc_w_bind = ui.select(_oc_bind_opts, label='gateway.bind',
                        value=_oc_cur_bind if _oc_cur_bind in _oc_bind_opts else 'lan',
                        with_input=True).classes('w-full')

                    ui.separator().classes('q-my-xs')
                    ui.label('Auth').classes('text-caption text-grey-6')

                    _oc_authmode_opts = ['token', 'password', 'none']
                    _oc_cur_authmode  = str(oc_auth.get('mode', 'token'))
                    oc_w_auth_mode = ui.select(_oc_authmode_opts, label='gateway.auth.mode',
                        value=_oc_cur_authmode if _oc_cur_authmode in _oc_authmode_opts else 'token').classes('w-full')

                    oc_w_auth_token = ui.input('gateway.auth.token',
                        value=str(oc_auth.get('token', '')),
                        password=True, password_toggle_button=True).classes('w-full')

                    def _oc_read_live_token():
                        tok, err = _read_openclaw_deploy_token()
                        if tok:
                            oc_w_auth_token.set_value(tok)
                            ui.notify(f'✅ Token read from deploy path', type='positive')
                        else:
                            ui.notify(f'⚠️ {err or "Token not found"}', type='warning')
                    ui.button(T['oc_token_live'], on_click=_oc_read_live_token).props('outline color=teal-8 size=sm').classes('q-mt-xs')

                    ui.separator().classes('q-my-xs')
                    ui.label('controlUi.allowedOrigins (one per line)').classes('text-caption text-grey-6')
                    _oc_origins = oc_ctrl_ui.get('allowedOrigins') or []
                    oc_w_allowed_origins = ui.textarea(value='\n'.join(_oc_origins)).classes('w-full').props('outlined rows=3')

                    ui.separator().classes('q-my-xs')
                    ui.label('Tailscale').classes('text-caption text-grey-6')
                    _oc_ts_opts = ['off', 'serve', 'funnel']
                    _oc_cur_ts  = str(oc_ts.get('mode', 'off'))
                    oc_w_ts_mode = ui.select(_oc_ts_opts, label='gateway.tailscale.mode',
                        value=_oc_cur_ts if _oc_cur_ts in _oc_ts_opts else 'off').classes('w-full')
                    oc_w_ts_reset = ui.checkbox('gateway.tailscale.resetOnExit',
                        value=bool(oc_ts.get('resetOnExit', False)))

                # ── Agents & Session ─────────────────────────────────────
                with ui.card().classes('w-full q-pa-md q-mb-sm'):
                    ui.label(T['oc_section_agents']).classes('text-subtitle2 text-grey-7')

                    oc_w_workspace = ui.input('agents.defaults.workspace',
                        value=str(oc_agents.get('workspace', '/var/lib/openclaw/.openclaw/workspace'))).classes('w-full')

                    ui.separator().classes('q-my-xs')
                    ui.label('Provider & Model').classes('text-caption text-grey-6')

                    # Derive current defaults from the OpenClaw model reference
                    _oc_model_primary = _oc_model_ref_text(oc_agents.get('model', ''))
                    _oc_cfg_init_prov = ''
                    if '/' in _oc_model_primary:
                        _oc_cfg_init_prov = _oc_model_primary.split('/', 1)[0]
                    _oc_cfg_prov_opts = list(_oc_ph_provs)
                    if _oc_cfg_init_prov and _oc_cfg_init_prov not in _oc_cfg_prov_opts:
                        _oc_cfg_prov_opts = [_oc_cfg_init_prov] + _oc_cfg_prov_opts
                    if not _oc_cfg_prov_opts:
                        _oc_cfg_prov_opts = ['']
                    if _oc_cfg_init_prov not in _oc_cfg_prov_opts:
                        _oc_cfg_init_prov = _oc_cfg_prov_opts[0]
                    # Read existing apiKey/baseUrl from models.providers.<name> (OpenClaw schema)
                    _oc_cfg_init_api_key = str(
                        oc_provider_models.get(_oc_cfg_init_prov, {}).get('apiKey', ''))
                    _oc_cfg_init_base_url = str(
                        oc_provider_models.get(_oc_cfg_init_prov, {}).get('baseUrl')
                        or oc_provider_models.get(_oc_cfg_init_prov, {}).get('base_url')
                        or _oc_ph_pid_base.get(_oc_cfg_init_prov, ''))

                    oc_w_cfg_quick = ui.select(
                        options=list(_oc_ph_map.keys()),
                        label='⚡ Quick pick model',
                        value=None,
                        clearable=True,
                        with_input=True,
                    ).classes('w-full q-mb-xs')

                    oc_w_provider = ui.select(
                        _oc_cfg_prov_opts,
                        label='provider',
                        value=_oc_cfg_init_prov,
                        with_input=True,
                    ).classes('w-full')

                    oc_w_model_primary = ui.input(
                        'agents.defaults.model  (provider/model)',
                        value=_oc_model_primary,
                    ).classes('w-full')

                    oc_w_api_key = ui.input(
                        'Provider API Key',
                        value=_oc_cfg_init_api_key,
                        password=True, password_toggle_button=True,
                    ).classes('w-full')

                    oc_w_base_url = ui.input(
                        'models.providers.<name>.baseUrl',
                        value=_oc_cfg_init_base_url,
                    ).classes('w-full')

                    # Quick-pick autofill
                    def _oc_cfg_fill_hint(e):
                        h = _oc_ph_map.get(e.value) if e.value else None
                        if not h: return
                        prov = h.get('provider', '')
                        if prov in _oc_cfg_prov_opts:
                            oc_w_provider.set_value(prov)
                        oc_w_model_primary.set_value(h.get('primary', prov + '/' + h.get('model', '')))
                        if h.get('api_base') and not oc_w_base_url.value:
                            oc_w_base_url.set_value(h.get('api_base'))
                        if h.get('api_key_required', True) is False:
                            oc_w_api_key.set_value('')
                    oc_w_cfg_quick.on_value_change(_oc_cfg_fill_hint)

                    # Provider selection autofill
                    def _oc_cfg_fill_prov(e):
                        prov = e.value or ''
                        if not prov: return
                        models = _oc_ph_pid_models.get(prov, [])
                        if models:
                            first = _oc_ph_map.get(models[0], {})
                            oc_w_model_primary.set_value(first.get('primary', prov + '/' + first.get('model', '')))
                            if first.get('api_base') and not oc_w_base_url.value:
                                oc_w_base_url.set_value(first.get('api_base'))
                        # Load existing apiKey for newly selected provider (OpenClaw schema)
                        existing_key = str(oc_provider_models.get(prov, {}).get('apiKey', ''))
                        if existing_key:
                            oc_w_api_key.set_value(existing_key)
                    oc_w_provider.on_value_change(_oc_cfg_fill_prov)

                    ui.separator().classes('q-my-xs')
                    _oc_dm_opts = ['per-channel-peer', 'global', 'per-channel']
                    _oc_cur_dm  = str(oc_session.get('dmScope', 'per-channel-peer'))
                    oc_w_dm_scope = ui.select(_oc_dm_opts, label='session.dmScope',
                        value=_oc_cur_dm if _oc_cur_dm in _oc_dm_opts else 'per-channel-peer').classes('w-full')

                # ── Tools ────────────────────────────────────────────────
                with ui.card().classes('w-full q-pa-md q-mb-sm'):
                    ui.label(T['oc_section_tools']).classes('text-subtitle2 text-grey-7')

                    _oc_tp_opts = ['minimal', 'coding', 'messaging', 'full']
                    _oc_cur_tp  = str(oc_tools.get('profile', 'coding'))
                    oc_w_tools_profile = ui.select(_oc_tp_opts, label='tools.profile',
                        value=_oc_cur_tp if _oc_cur_tp in _oc_tp_opts else 'coding',
                        with_input=True).classes('w-full')

                # ── Action buttons ───────────────────────────────────────
                def oc_collect_and_save():
                    """Collect all widget values back into the openclaw config dict and save locally."""
                    data = load_openclaw_config()
                    data.setdefault('gateway', {})
                    data.setdefault('agents', {}).setdefault('defaults', {})
                    data.setdefault('session', {})
                    data.setdefault('tools', {})

                    data['gateway']['mode']  = oc_w_mode.value or 'local'
                    data['gateway']['port']  = int(oc_w_port.value or 18789)
                    data['gateway']['bind']  = oc_w_bind.value or 'lan'
                    data['gateway'].setdefault('auth', {})
                    data['gateway']['auth']['mode']  = oc_w_auth_mode.value or 'token'
                    data['gateway']['auth']['token'] = oc_w_auth_token.value or ''
                    data['gateway'].setdefault('controlUi', {})
                    origins_raw = [l.strip() for l in oc_w_allowed_origins.value.splitlines() if l.strip()]
                    data['gateway']['controlUi']['allowedOrigins'] = origins_raw
                    data['gateway'].setdefault('tailscale', {})
                    data['gateway']['tailscale']['mode']        = oc_w_ts_mode.value or 'off'
                    data['gateway']['tailscale']['resetOnExit'] = oc_w_ts_reset.value

                    data['agents']['defaults']['workspace'] = oc_w_workspace.value or ''
                    # OpenClaw does NOT have agents.defaults.provider
                    data['agents']['defaults']['model']    = oc_w_model_primary.value or ''

                    # Save provider config into models.providers.<name> (OpenClaw schema)
                    _cfg_prov = oc_w_provider.value or ''
                    if _cfg_prov and (oc_w_api_key.value or oc_w_base_url.value):
                        data.setdefault('models', {}).setdefault('providers', {}).setdefault(_cfg_prov, {})
                        if oc_w_base_url.value:
                            data['models']['providers'][_cfg_prov]['baseUrl'] = oc_w_base_url.value
                        if oc_w_api_key.value:
                            data['models']['providers'][_cfg_prov]['apiKey'] = oc_w_api_key.value

                    data['session']['dmScope'] = oc_w_dm_scope.value or 'per-channel-peer'
                    data['tools']['profile']   = oc_w_tools_profile.value or 'coding'

                    try:
                        save_openclaw_config(data)
                        ui.notify(T['oc_notify_saved'], type='positive')
                        return True
                    except Exception as e:
                        ui.notify(T['oc_notify_save_fail'].format(e), type='negative')
                        return False

                def oc_do_save_restart():
                    if not oc_collect_and_save():
                        return
                    ok_cfg, err_cfg = deploy_openclaw_config()
                    if not ok_cfg:
                        ui.notify(f'⚠️ Deploy failed: {err_cfg}', type='warning')
                        return
                    ok_svc, svc_err = restart_openclaw_service()
                    if ok_svc:
                        ui.notify('✅ OpenClaw deployed & restarted', type='positive')
                    else:
                        ui.notify(f'⚠️ Restart failed: {svc_err or T["notify_sudo_required"]}', type='warning')

                ui.separator()
                with ui.row().classes('w-full gap-2 q-pa-sm'):
                    ui.button(T['oc_btn_save'],         on_click=oc_collect_and_save).props('elevated').classes('flex-1 bg-teal-8 text-white')
                    ui.button(T['oc_btn_save_restart'],  on_click=oc_do_save_restart).props('elevated').classes('flex-1 bg-teal-10 text-white')

            # ── OpenClaw › Pair Device ─────────────────────────────────────
            with ui.tab_panel(t_oc_pair):
                oc_tok, oc_tok_err = _read_openclaw_deploy_token()
                oc_port = int(load_openclaw_config().get('gateway', {}).get('port', 18789) or 18789)
                oc_host   = _get_lan_ip() or request.url.hostname or 'localhost'
                oc_scheme = request.url.scheme or 'http'
                oc_url    = f'{oc_scheme}://{oc_host}:{oc_port}?token={oc_tok}' if oc_tok else ''
                oc_qr_url = f'https://quickchart.io/qr?size=260&margin=1&text={quote(oc_url, safe="")}' if oc_url else ''
                oc_tok_qr = f'https://quickchart.io/qr?size=260&margin=1&text={quote(oc_tok, safe="")}' if oc_tok else ''

                # ── QR / token card ──────────────────────────────────────
                with ui.card().classes('w-full q-pa-md q-mb-sm'):
                    ui.label(T['oc_pair_title']).classes('text-h6 text-teal-8')
                    if oc_tok_err:
                        ui.label(f'⚠️ {oc_tok_err}').classes('text-warning text-caption q-mt-xs')
                    if oc_tok:
                        ui.input('gateway.auth.token', value=oc_tok).props('readonly').classes('w-full q-mt-sm')
                        if oc_url:
                            ui.input('Pairing URL', value=oc_url).props('readonly').classes('w-full')

                        with ui.row().classes('w-full items-start gap-4 q-mt-sm'):
                            if oc_qr_url:
                                with ui.card().classes('q-pa-sm items-center bg-white'):
                                    ui.label(T['oc_pair_qr']).classes('text-caption text-grey-7 q-mb-xs')
                                    ui.image(oc_qr_url).classes('w-56 h-56')

                            with ui.card().classes('q-pa-sm items-center bg-white'):
                                ui.label(T['oc_gateway_token_qr']).classes('text-caption text-grey-7 q-mb-xs')
                                ui.image(oc_tok_qr).classes('w-56 h-56')

                            with ui.column().classes('gap-2 q-mt-md'):
                                def _copy_oc_url(url=oc_url):
                                    ui.clipboard.write(url)
                                    ui.notify('✅ URL copied', type='positive')
                                if oc_url:
                                    ui.button('📋 Copy URL', on_click=_copy_oc_url).props('outline color=teal-8')
                    else:
                        ui.label(T['oc_pair_missing_token']).classes('text-negative q-mt-sm')

                # ── Device lists ─────────────────────────────────────────
                def _oc_load_devices():
                    """Run `openclaw devices list --json` as the openclaw system user.
                    Passes --token and --url so the gateway auth token (operator.read scope)
                    is used instead of the CLI paired device (operator.pairing scope only)."""
                    gw_url = f'ws://localhost:{oc_port}'
                    cmd = ['sudo', '-u', 'openclaw', '-i',
                           'openclaw', 'devices', 'list', '--json']
                    if oc_tok:
                        cmd += ['--url', gw_url, '--token', oc_tok]
                    r = subprocess.run(cmd, capture_output=True, text=True)
                    if r.returncode != 0:
                        return None, r.stderr.strip() or f'exit {r.returncode}'
                    # The login shell may print banners before AND after the JSON.
                    # Find the first '{' or '[', then use raw_decode to consume
                    # exactly one JSON value and ignore any trailing shell output.
                    out = r.stdout
                    start = -1
                    for ch in ('{', '['):
                        idx = out.find(ch)
                        if idx != -1 and (start == -1 or idx < start):
                            start = idx
                    if start == -1:
                        return None, f'No JSON found in output: {out[:200]}'
                    try:
                        obj, _ = json.JSONDecoder().raw_decode(out, start)
                        return obj, ''
                    except Exception as e:
                        return None, f'Parse error: {e}  raw={out[start:start+200]}'

                async def _oc_approve_device(request_id: str, name_lbl, btn=None):
                    if btn:
                        btn.disable()
                    gw_url = f'ws://localhost:{oc_port}'
                    cmd = ['sudo', '-u', 'openclaw', '-i',
                           'openclaw', 'devices', 'approve', request_id]
                    if oc_tok:
                        cmd += ['--url', gw_url, '--token', oc_tok]
                    import asyncio as _asyncio
                    try:
                        r = await _asyncio.to_thread(
                            subprocess.run, cmd, capture_output=True, text=True, timeout=30
                        )
                    except subprocess.TimeoutExpired:
                        ui.notify('❌ Timed out waiting for approval response', type='negative')
                        if btn:
                            btn.enable()
                        return
                    except Exception as exc:
                        ui.notify(f'❌ Unexpected error: {exc}', type='negative')
                        if btn:
                            btn.enable()
                        return
                    if r.returncode == 0:
                        ui.notify(f'✅ Approved {request_id[:8]}…', type='positive')
                        name_lbl.set_text('✅ Approved')
                    else:
                        err = r.stderr.strip() or r.stdout.strip() or f'exit {r.returncode}'
                        ui.notify(f'❌ {err}', type='negative')
                        if btn:
                            btn.enable()

                def _oc_fmt_ts(ms):
                    if not ms:
                        return ''
                    try:
                        from datetime import datetime as _dt
                        return _dt.fromtimestamp(int(ms) / 1000).strftime('%Y-%m-%d %H:%M')
                    except Exception:
                        return str(ms)

                # ── Pending approvals ────────────────────────────────────
                with ui.card().classes('w-full q-pa-md q-mb-sm'):
                    with ui.row().classes('w-full items-center justify-between q-mb-sm'):
                        ui.label('⏳ Pending Approval').classes('text-subtitle1 text-bold text-orange-8')
                        def _oc_refresh_devices():
                            _pending_col.clear()
                            _paired_col.clear()
                            _oc_populate_devices()
                        ui.button(icon='refresh', on_click=_oc_refresh_devices) \
                            .props('flat round dense color=teal-8').tooltip('Refresh')

                    _pending_col = ui.column().classes('w-full gap-2')

                # ── Paired devices ───────────────────────────────────────
                with ui.card().classes('w-full q-pa-md'):
                    ui.label('📱 Paired Devices').classes('text-subtitle1 text-bold text-teal-8 q-mb-sm')
                    _paired_col = ui.column().classes('w-full gap-2')

                def _oc_populate_devices():
                    devs, err = _oc_load_devices()

                    # ── Pending ──────────────────────────────────────────
                    with _pending_col:
                        if err and devs is None:
                            ui.label(f'⚠️ {err}').classes('text-caption text-negative')
                        else:
                            pending = (devs or {}).get('pending', [])
                            if not pending:
                                ui.label('No pending requests').classes('text-caption text-grey-6')
                            for dev in pending:
                                rid   = dev.get('requestId', '')
                                dname = dev.get('displayName') or dev.get('deviceId', '?')[:16] + '…'
                                plat  = dev.get('platform', '')
                                fam   = dev.get('deviceFamily', '')
                                rip   = dev.get('remoteIp', '')
                                roles = ', '.join(dev.get('roles') or [])
                                ts    = _oc_fmt_ts(dev.get('ts'))
                                with ui.card().classes('w-full q-pa-sm bg-orange-1'):
                                    with ui.row().classes('w-full items-center justify-between'):
                                        with ui.column().classes('gap-0'):
                                            ui.label(f'📲 {dname}').classes('text-bold')
                                            ui.label(f'{fam or plat}  ·  {rip}  ·  {roles}').classes('text-caption text-grey-7')
                                            ui.label(f'Request ID: {rid}').classes('text-caption text-mono text-grey-6')
                                            ui.label(f'Requested: {ts}').classes('text-caption text-grey-6')
                                        _approve_lbl = ui.label('')
                                        _approve_btn = ui.button('✅ Approve').props('color=teal-8 size=sm')
                                        async def _on_approve(_rid=rid, _lbl=_approve_lbl, _btn=_approve_btn):
                                            await _oc_approve_device(_rid, _lbl, _btn)
                                        _approve_btn.on_click(_on_approve)

                    # ── Paired ───────────────────────────────────────────
                    with _paired_col:
                        if devs is not None:
                            paired = devs.get('paired', [])
                            if not paired:
                                ui.label('No paired devices').classes('text-caption text-grey-6')
                            for dev in paired:
                                dname   = dev.get('displayName') or dev.get('clientId', '?')
                                plat    = dev.get('platform', '')
                                fam     = dev.get('deviceFamily', '')
                                rip     = dev.get('remoteIp', '')
                                role    = dev.get('role', '')
                                roles   = ', '.join(dev.get('roles') or [])
                                created = _oc_fmt_ts(dev.get('createdAtMs'))
                                mode    = dev.get('clientMode', '')
                                with ui.card().classes('w-full q-pa-sm bg-teal-1'):
                                    ui.label(f'📱 {dname}').classes('text-bold')
                                    ui.label(f'{fam or plat}  ·  {rip}  ·  role: {role}  ·  mode: {mode}').classes('text-caption text-grey-7')
                                    ui.label(f'Roles: {roles}').classes('text-caption text-grey-6')
                                    if created:
                                        ui.label(f'Paired: {created}').classes('text-caption text-grey-6')

                _oc_populate_devices()

            # ── OpenClaw › Doctor ──────────────────────────────────────────
            with ui.tab_panel(t_oc_doctor):
                import asyncio as _asyncio_dr

                async def _oc_run_cmd_async(cmd):
                    try:
                        return await _asyncio_dr.to_thread(
                            subprocess.run, cmd,
                            capture_output=True, text=True, timeout=60
                        )
                    except subprocess.TimeoutExpired:
                        return None

                # ── Status card ──────────────────────────────────────────
                with ui.card().classes('w-full q-pa-md q-mb-sm'):
                    with ui.row().classes('w-full items-center justify-between q-mb-sm'):
                        ui.label('📊 OpenClaw Status').classes('text-subtitle1 text-bold text-teal-8')
                        _status_btn = ui.button('▶ Run openclaw status', icon='play_arrow').props('color=teal-8 elevated size=sm')

                    _status_out = ui.column().classes('w-full gap-1')

                    async def _oc_run_status(_btn=_status_btn, _out=_status_out):
                        _btn.disable()
                        _out.clear()
                        with _out:
                            ui.spinner('dots', size='sm').classes('text-teal-8')
                        oc_tok_s, _ = _read_openclaw_deploy_token()
                        oc_port_s = int(load_openclaw_config().get('gateway', {}).get('port', 18789) or 18789)
                        cmd = ['sudo', '-u', 'openclaw', '-i', 'openclaw', 'status', '--json']
                        if oc_tok_s:
                            cmd += ['--url', f'ws://localhost:{oc_port_s}', '--token', oc_tok_s]
                        r = await _oc_run_cmd_async(cmd)
                        _out.clear()
                        with _out:
                            if r is None:
                                ui.label('❌ Timed out').classes('text-negative text-caption')
                                _btn.enable()
                                return
                            if r.returncode != 0:
                                err = r.stderr.strip() or r.stdout.strip() or f'exit {r.returncode}'
                                ui.label(f'❌ {err}').classes('text-negative text-caption')
                                _btn.enable()
                                return
                            # Parse JSON output
                            raw = r.stdout
                            start = -1
                            for ch in ('{', '['):
                                idx = raw.find(ch)
                                if idx != -1 and (start == -1 or idx < start):
                                    start = idx
                            if start == -1:
                                ui.label(raw or '(no output)').classes('text-caption text-mono')
                                _btn.enable()
                                return
                            try:
                                obj, _ = json.JSONDecoder().raw_decode(raw, start)
                            except Exception:
                                ui.label(raw).classes('text-caption text-mono')
                                _btn.enable()
                                return
                            # Render parsed status fields
                            if isinstance(obj, dict):
                                for k, v in obj.items():
                                    color = 'text-positive' if str(v).lower() in ('true', 'active', 'ok', 'running', 'online') \
                                            else 'text-negative' if str(v).lower() in ('false', 'inactive', 'error', 'failed', 'offline') \
                                            else 'text-grey-8'
                                    with ui.row().classes('gap-2 items-center'):
                                        ui.label(f'{k}:').classes('text-caption text-grey-6 text-bold')
                                        ui.label(str(v)).classes(f'text-caption {color}')
                            else:
                                ui.label(str(obj)).classes('text-caption text-mono')
                            _btn.enable()

                    _status_btn.on_click(_oc_run_status)

                # ── Doctor card ───────────────────────────────────────────
                with ui.card().classes('w-full q-pa-md'):
                    with ui.row().classes('w-full items-center justify-between q-mb-sm'):
                        ui.label('🩺 OpenClaw Doctor').classes('text-subtitle1 text-bold text-teal-8')
                        _doctor_btn = ui.button('▶ Run openclaw doctor --fix', icon='build').props('color=orange-8 elevated size=sm')

                    ui.label('Checks and auto-fixes missing dependencies, config issues, and service health.') \
                        .classes('text-caption text-grey-7 q-mb-sm')

                    _doctor_out = ui.log(max_lines=200).classes('w-full').style(
                        'height:320px; font-size:12px; background:#1e1e1e; color:#d4d4d4; border-radius:4px;'
                    )

                    async def _oc_run_doctor(_btn=_doctor_btn, _log=_doctor_out):
                        _btn.disable()
                        _log.clear()

                        async def _svc(action: str) -> bool:
                            cmd = ['sudo', '-u', 'openclaw', '-i',
                                   'systemctl', '--user', action, 'openclaw']
                            _log.push('$ ' + ' '.join(cmd))
                            r = await _oc_run_cmd_async(cmd)
                            if r is None:
                                _log.push(f'❌ systemctl {action} timed out')
                                return False
                            out = (r.stdout or '').strip() + (r.stderr or '').strip()
                            if out:
                                _log.push(out)
                            if r.returncode != 0:
                                _log.push(f'❌ systemctl {action} failed (exit {r.returncode})')
                                return False
                            _log.push(f'✅ openclaw service {action}ped')
                            return True

                        # 1. Stop service
                        if not await _svc('stop'):
                            ui.notify('❌ Could not stop openclaw service', type='negative')
                            _btn.enable()
                            return

                        # 2. Run doctor --fix
                        cmd = ['sudo', '-u', 'openclaw', '-i', 'openclaw', 'doctor', '--fix']
                        _log.push('$ ' + ' '.join(cmd))
                        r = await _oc_run_cmd_async(cmd)
                        if r is None:
                            _log.push('❌ Timed out after 60 s')
                            ui.notify('❌ Doctor timed out', type='negative')
                        else:
                            output = (r.stdout or '') + (r.stderr or '')
                            for line in output.splitlines():
                                _log.push(line)
                            if r.returncode == 0:
                                ui.notify('✅ Doctor finished', type='positive')
                            else:
                                ui.notify(f'⚠️ Doctor exited {r.returncode}', type='warning')

                        # 3. Restart service regardless of doctor outcome
                        await _svc('start')
                        _btn.enable()

                    _doctor_btn.on_click(_oc_run_doctor)

            # ── OpenClaw › Characters ──────────────────────────────────────
            with ui.tab_panel(t_oc_char):
                _build_character_tab('/var/lib/openclaw/.openclaw/workspace', 'openclaw', 'teal-8')

            # ── OpenClaw › Skills ─────────────────────────────────────────
            with ui.tab_panel(t_oc_skills):
                _build_skills_tab('/var/lib/openclaw/.openclaw/workspace', 'openclaw', 'teal-8')

    # ── Sidebar navigation wiring ──────────────────────────────────────────────
    # ══ WiFi Setup ════════════════════════════════════════════════════════════
    wifi_content = ui.column().classes('w-full q-px-sm q-pt-sm')
    wifi_content.set_visibility(False)
    with wifi_content:
        ui.label(T['wifi_title']).classes('text-h6 text-teal-8 q-mb-xs')

        # ── Current WiFi status card ─────────────────────────────────────────
        with ui.card().classes('w-full q-pa-md q-mb-sm'):
            with ui.row().classes('w-full items-center justify-between'):
                ui.label(T['wifi_cur_status']).classes('text-subtitle1 text-bold')
                def _refresh_wifi_status():
                    import subprocess as _sp
                    lines = []

                    # ── Primary: nmcli (works with USB WiFi adapters) ────────
                    ssid = iface = ip4 = gw4 = signal = ''
                    try:
                        # nmcli -t -f DEVICE,TYPE,STATE,CONNECTION,SIGNAL dev
                        nm = _sp.run(
                            ['nmcli', '-t', '-f', 'DEVICE,TYPE,STATE,CONNECTION,SIGNAL', 'dev'],
                            capture_output=True, text=True
                        )
                        for row in nm.stdout.splitlines():
                            parts = row.split(':')
                            if len(parts) >= 3 and parts[1] == 'wifi' and parts[2] == 'connected':
                                iface  = parts[0]
                                ssid   = parts[3] if len(parts) > 3 else ''
                                signal = parts[4].strip() + ' %' if len(parts) > 4 and parts[4].strip() else ''
                                break
                    except Exception:
                        pass

                    # ── Fallback: iwgetid (classic wireless-tools) ───────────
                    if not ssid:
                        try:
                            iface_r = _sp.run(['iwgetid', '-r'], capture_output=True, text=True)
                            ssid    = iface_r.stdout.strip()
                            iface_n = _sp.run(['iwgetid'],       capture_output=True, text=True)
                            iface   = iface_n.stdout.split()[0] if iface_n.stdout.strip() else iface or 'wlan0'
                        except Exception:
                            pass

                    # ── Detect any active wifi interface if still unknown ─────
                    if not iface:
                        try:
                            ip_lnk = _sp.run(['ip', '-o', 'link', 'show'],
                                             capture_output=True, text=True)
                            for ln in ip_lnk.stdout.splitlines():
                                if 'wlan' in ln or 'wlp' in ln or 'wlx' in ln:
                                    iface = ln.split(':')[1].strip().split('@')[0]
                                    break
                        except Exception:
                            iface = 'wlan0'

                    # ── Connected / disconnected banner ──────────────────────
                    if ssid:
                        lines.append(f'🟢 Connected  |  SSID: {ssid}  |  Interface: {iface}')
                    else:
                        # Double-check: is there an IP on the wifi iface anyway?
                        try:
                            chk = _sp.run(['ip', '-4', 'addr', 'show', iface or 'wlan0'],
                                          capture_output=True, text=True)
                            has_ip = any(l.strip().startswith('inet ') for l in chk.stdout.splitlines())
                        except Exception:
                            has_ip = False
                        if has_ip:
                            lines.append(f'🟡 Connected (SSID unknown)  |  Interface: {iface or "wlan0"}')
                        else:
                            lines.append('🔴 Not connected to any WiFi network')

                    # ── IP address ───────────────────────────────────────────
                    try:
                        ip_r = _sp.run(['ip', '-4', 'addr', 'show', iface or 'wlan0'],
                                       capture_output=True, text=True)
                        for ln in ip_r.stdout.splitlines():
                            ln = ln.strip()
                            if ln.startswith('inet '):
                                ip4 = ln.split()[1]
                                lines.append(f'   IP Address : {ip4}')
                                break
                        else:
                            lines.append('   IP Address : (none)')
                    except Exception as exc:
                        lines.append(f'   IP Address : error ({exc})')

                    # ── Default gateway ──────────────────────────────────────
                    try:
                        gw_r = _sp.run(['ip', 'route', 'show', 'default'],
                                       capture_output=True, text=True)
                        for ln in gw_r.stdout.strip().splitlines():
                            parts = ln.split()
                            if len(parts) > 2:
                                lines.append(f'   Gateway    : {parts[2]}')
                                break
                    except Exception:
                        pass

                    # ── Signal (from nmcli or iwconfig fallback) ─────────────
                    if signal:
                        lines.append(f'   Signal     : {signal}')
                    else:
                        try:
                            iwc = _sp.run(['iwconfig', iface or 'wlan0'],
                                          capture_output=True, text=True)
                            for ln in iwc.stdout.splitlines():
                                if 'Signal level' in ln:
                                    sig = ln.strip().split('Signal level=')[-1].split()[0]
                                    lines.append(f'   Signal     : {sig}')
                                    break
                        except Exception:
                            pass

                    wifi_cur_lbl.set_text('\n'.join(lines))

                ui.button(icon='refresh', on_click=_refresh_wifi_status).props('flat round dense color=teal-8').tooltip(T['wifi_refresh_tip'])
            wifi_cur_lbl = ui.label('…').classes('text-caption text-mono q-mt-xs').style('white-space: pre')
            # Populate immediately when the card is built
            _refresh_wifi_status()

        # ── Captive portal card ───────────────────────────────────────────────
        with ui.card().classes('w-full q-pa-md'):
            ui.label(T['wifi_config_title']).classes('text-subtitle1 text-bold q-mb-xs')
            ui.label(T['wifi_config_hint']).classes('text-caption text-grey-6 q-mb-md')

            wifi_status_lbl = ui.label('').classes('text-caption text-grey-7 q-mb-sm')
            wifi_log_area   = ui.textarea().classes('w-full').props('outlined rows=8 readonly label="Output"')
            wifi_log_area.set_visibility(False)

            _wifi_proc = {'proc': None}

            def _start_wifi_setup():
                wifi_log_area.set_visibility(True)
                wifi_log_area.set_value('')
                wifi_status_lbl.set_text('⏳ Starting wifi-connect…')
                btn_wifi_start.props('disabled loading')
                btn_wifi_stop.props(remove='disabled')
                import threading, subprocess as _sp

                def _run():
                    # Stop dnsmasq if it's running so wifi-connect can bind port 53
                    _dnsmasq_was_active = _sp.run(
                        ['systemctl', 'is-active', '--quiet', 'dnsmasq'],
                        capture_output=True
                    ).returncode == 0
                    if _dnsmasq_was_active:
                        wifi_log_area.set_value(wifi_log_area.value + '[setup] Stopping dnsmasq…\n')
                        _sp.run(['sudo', '/usr/bin/systemctl', 'stop', 'dnsmasq'],
                                capture_output=True)
                    try:
                        proc = _sp.Popen(
                            ['sudo', '/opt/wifi-connect/wifi-connect',
                             '-u', '/opt/wifi-connect/web',
                             '-s', 'ClawBerry WiFi Setup'],
                            stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True
                        )
                        _wifi_proc['proc'] = proc
                        wifi_status_lbl.set_text('🟢 Running — connect to "ClawBerry WiFi Setup" AP')
                        for line in proc.stdout:
                            wifi_log_area.set_value(wifi_log_area.value + line)
                        proc.wait()
                        rc = proc.returncode
                        wifi_status_lbl.set_text(
                            f'✅ Finished (exit {rc})' if rc == 0 else f'⚠️ Exited with code {rc}'
                        )
                        _refresh_wifi_status()  # update current-status card
                    except Exception as exc:
                        wifi_status_lbl.set_text(f'❌ Error: {exc}')
                    finally:
                        _wifi_proc['proc'] = None
                        btn_wifi_start.props(remove='disabled loading')
                        btn_wifi_stop.props('disabled')
                        # Restore dnsmasq if it was running before
                        if _dnsmasq_was_active:
                            wifi_log_area.set_value(wifi_log_area.value + '[setup] Restoring dnsmasq…\n')
                            _sp.run(['sudo', '/usr/bin/systemctl', 'start', 'dnsmasq'],
                                    capture_output=True)

                threading.Thread(target=_run, daemon=True).start()

            def _stop_wifi_setup():
                proc = _wifi_proc.get('proc')
                if proc:
                    try:
                        proc.terminate()
                        wifi_status_lbl.set_text('🛑 Stopped')
                    except Exception as exc:
                        wifi_status_lbl.set_text(f'Stop failed: {exc}')
                btn_wifi_stop.props('disabled')

            with ui.row().classes('gap-2 q-mt-sm'):
                btn_wifi_start = ui.button(
                    T['wifi_btn_start'], on_click=_start_wifi_setup
                ).props('elevated color=teal-8')
                btn_wifi_stop = ui.button(
                    T['wifi_btn_stop'], on_click=_stop_wifi_setup
                ).props('outline color=negative disabled')

    # ══ Upgrade ═══════════════════════════════════════════════════════════════
    # ══ ClawBerry Proxy Panel ════════════════════════════════════════════════
    proxy_content = ui.column().classes('w-full q-px-sm q-pt-sm')
    proxy_content.set_visibility(False)
    with proxy_content:
        ui.label(T['proxy_title']).classes('text-h6 text-indigo-9 q-mb-xs')
        with ui.card().classes('w-full q-pa-md'):
            ui.label(T['proxy_card_title']).classes('text-subtitle1 text-bold q-mb-xs')
            ui.label(T['proxy_hint']).classes('text-caption text-grey-6 q-mb-sm')

            proxy_status_lbl  = ui.label('').classes('text-body2 q-mb-sm')
            proxy_enabled_lbl = ui.label('').classes('text-caption text-grey-7 q-mb-sm')

            def _proxy_get_status():
                active  = subprocess.run(['systemctl', 'is-active',  'clawberry-proxy.service'],
                                         capture_output=True, text=True).stdout.strip()
                enabled = subprocess.run(['systemctl', 'is-enabled', 'clawberry-proxy.service'],
                                         capture_output=True, text=True).stdout.strip()
                return active, enabled

            def _proxy_refresh():
                active, enabled = _proxy_get_status()
                status_txt  = T['proxy_active']   if active  == 'active'  else T['proxy_inactive']
                enabled_txt = T['proxy_enabled']  if enabled == 'enabled' else T['proxy_disabled']
                proxy_status_lbl.set_text(f"{T['proxy_status_lbl']}: {status_txt}  |  {enabled_txt}")

            def _proxy_enable():
                subprocess.run(['sudo', '/usr/bin/systemctl', 'enable', '--now', 'clawberry-proxy.service'],
                               capture_output=True)
                _proxy_refresh()
                ui.notify('clawberry-proxy enabled & started', color='positive')

            def _proxy_disable():
                subprocess.run(['sudo', '/usr/bin/systemctl', 'disable', '--now', 'clawberry-proxy.service'],
                               capture_output=True)
                _proxy_refresh()
                ui.notify('clawberry-proxy disabled & stopped', color='warning')

            def _proxy_restart():
                subprocess.run(['sudo', '/usr/bin/systemctl', 'restart', 'clawberry-proxy.service'],
                               capture_output=True)
                _proxy_refresh()
                ui.notify('clawberry-proxy restarted', color='positive')

            _proxy_refresh()

            with ui.row().classes('gap-2 q-mt-sm'):
                ui.button(T['proxy_btn_enable'],  on_click=_proxy_enable) \
                    .props('elevated color=positive')
                ui.button(T['proxy_btn_disable'], on_click=_proxy_disable) \
                    .props('elevated color=warning')
                ui.button(T['proxy_btn_restart'], on_click=_proxy_restart) \
                    .props('elevated color=indigo-7')
                ui.button(T['proxy_btn_refresh'], on_click=_proxy_refresh) \
                    .props('flat color=grey-7')

        # ── ClawProxy TTS Config ──────────────────────────────────────────────
        with ui.card().classes('w-full q-pa-md q-mt-sm'):
            ui.label(T.get('proxy_cp_cfg_title', '🎤 ClawProxy TTS Config')).classes('text-subtitle1 text-bold q-mb-xs')
            ui.label(T.get('proxy_cp_cfg_hint', 'Edit clawproxy TTS provider settings (config.toml). Deploy to /opt/clawproxy/ when done.')).classes('text-caption text-grey-6 q-mb-sm')

            cpc_default_provider = ui.select(
                ['mimotts', 'f5tts', 'qwen3tts'],
                label=T.get('proxy_cp_default_provider', 'Default TTS Provider'),
                value='mimotts'
            ).classes('w-full q-mb-xs')
            cpc_default_voice = ui.input(
                T.get('proxy_cp_default_voice', 'Default Voice'),
                value='Chloe'
            ).classes('w-full q-mb-sm')

            # ── mimotts ──
            with ui.expansion('🔊 MimoTTS', value=True).classes('w-full q-mb-xs'):
                cpc_mimo_key = ui.input('API Key', value='').classes('w-full q-mb-xs')
                cpc_mimo_url = ui.input('Base URL', value='https://token-plan-cn.xiaomimimo.com/v1').classes('w-full q-mb-xs')
                cpc_mimo_model = ui.input('Model', value='mimo-v2.5-tts').classes('w-full')

            # ── f5tts ──
            with ui.expansion('🔊 F5-TTS', value=False).classes('w-full q-mb-xs'):
                cpc_f5_url = ui.input('Base URL', value='http://apicn.aiworm.cn:8010').classes('w-full q-mb-xs')
                cpc_f5_key = ui.input('API Key', value='token1').classes('w-full q-mb-xs')
                cpc_f5_speed = ui.number('Speed (0.5–2.0)', value=0.8, min=0.5, max=2.0, step=0.05).classes('w-full')

            # ── qwen3tts ──
            with ui.expansion('🔊 Qwen3-TTS', value=False).classes('w-full q-mb-xs'):
                cpc_qwen_url = ui.input('Base URL', value='http://apicn.aiworm.cn:8012').classes('w-full q-mb-xs')
                cpc_qwen_speed = ui.number('Speed (0.5–2.0)', value=0.9, min=0.5, max=2.0, step=0.05).classes('w-full')

            cpc_status = ui.label('').classes('text-caption q-mt-xs')

            def _cp_cfg_load():
                """Load clawproxy config.toml and populate UI fields."""
                try:
                    c = load_clawproxy_config()
                except Exception as e:
                    ui.notify(f'Failed to parse config.toml: {e}', type='negative')
                    return
                tts = c.get('tts', {}) if c else {}
                cpc_default_provider.set_value(tts.get('default_provider', 'mimotts'))
                cpc_default_voice.set_value(tts.get('default_voice', 'Chloe'))
                mimo = tts.get('mimotts', {})
                cpc_mimo_key.set_value(mimo.get('api_key', ''))
                cpc_mimo_url.set_value(mimo.get('base_url', 'https://token-plan-cn.xiaomimimo.com/v1'))
                cpc_mimo_model.set_value(mimo.get('model', 'mimo-v2.5-tts'))
                f5 = tts.get('f5tts', {})
                cpc_f5_url.set_value(f5.get('base_url', 'http://apicn.aiworm.cn:8010'))
                cpc_f5_key.set_value(f5.get('api_key', 'token1'))
                cpc_f5_speed.set_value(float(f5.get('speed', 0.8)))
                qwen = tts.get('qwen3tts', {})
                cpc_qwen_url.set_value(qwen.get('base_url', 'http://apicn.aiworm.cn:8012'))
                cpc_qwen_speed.set_value(float(qwen.get('speed', 0.9)))
                cpc_status.set_text(T.get('proxy_cp_loaded', '✅ Loaded from local config'))

            def _cp_cfg_save():
                """Save UI values to local clawproxy config.toml."""
                try:
                    c = tomlkit.document()
                    tts = tomlkit.table()
                    tts['default_provider'] = cpc_default_provider.value
                    tts['default_voice'] = cpc_default_voice.value
                    mimo = tomlkit.table()
                    mimo['api_key'] = cpc_mimo_key.value or ''
                    mimo['base_url'] = cpc_mimo_url.value or ''
                    mimo['model'] = cpc_mimo_model.value or ''
                    tts['mimotts'] = mimo
                    f5 = tomlkit.table()
                    f5['base_url'] = cpc_f5_url.value or ''
                    f5['api_key'] = cpc_f5_key.value or ''
                    f5['speed'] = cpc_f5_speed.value
                    tts['f5tts'] = f5
                    qwen = tomlkit.table()
                    qwen['base_url'] = cpc_qwen_url.value or ''
                    qwen['speed'] = cpc_qwen_speed.value
                    tts['qwen3tts'] = qwen
                    c['tts'] = tts
                    save_clawproxy_config(c)
                    cpc_status.set_text(T.get('proxy_cp_saved_ok', '✅ Config saved locally'))
                    ui.notify(T.get('proxy_cp_saved_ok', '✅ clawproxy config.toml saved'), type='positive')
                except Exception as e:
                    cpc_status.set_text(str(e))
                    ui.notify(T.get('proxy_cp_save_err', '❌ Save failed: {}').format(e), type='negative')

            def _cp_cfg_deploy():
                """Save local config, deploy to /opt/clawproxy/config.toml, then restart clawberry-proxy."""
                _cp_cfg_save()
                ok, msg = deploy_clawproxy_config()
                if not ok:
                    cpc_status.set_text(msg)
                    ui.notify(T.get('proxy_cp_deploy_err', '❌ Deploy failed: {}').format(msg), type='negative')
                    return
                # Restart clawberry-proxy so it picks up the new config
                rr = subprocess.run(
                    ['sudo', '/usr/bin/systemctl', 'restart', 'clawberry-proxy.service'],
                    capture_output=True, text=True
                )
                if rr.returncode != 0:
                    err = rr.stderr.strip() or f'systemctl restart failed (exit {rr.returncode})'
                    cpc_status.set_text(f'Config deployed but restart failed: {err}')
                    ui.notify(f'Config deployed but restart failed: {err}', type='warning')
                else:
                    cpc_status.set_text(T.get('proxy_cp_deploy_ok', '✅ Deployed & service restarted'))
                    ui.notify(T.get('proxy_cp_deploy_ok', '✅ clawproxy config deployed & service restarted'), type='positive')

            with ui.row().classes('gap-2 q-mt-sm'):
                ui.button(T.get('proxy_cp_btn_load', '📂 Load'), on_click=_cp_cfg_load).props('flat color=blue-8')
                ui.button(T.get('proxy_cp_btn_save', '💾 Save'), on_click=_cp_cfg_save).props('elevated color=indigo-7')
                ui.button(T.get('proxy_cp_btn_deploy', '🚀 Deploy'), on_click=_cp_cfg_deploy).props('elevated color=positive')

            # Auto-load on first view
            _cp_cfg_load()

    upgrade_content = ui.column().classes('w-full q-px-sm q-pt-sm')
    upgrade_content.set_visibility(False)
    with upgrade_content:
        ui.label(T['upgrade_title']).classes('text-h6 text-orange-9 q-mb-xs')

    upgrade_content_inner = upgrade_content
    with upgrade_content_inner:
        with ui.card().classes('w-full q-pa-md'):
            ui.label(T['upgrade_card_title']).classes('text-subtitle1 text-bold q-mb-xs')
            ui.label(T['upgrade_hint']).classes('text-caption text-grey-6 q-mb-sm')

            # ── Config-file sync options ──────────────────────────────────
            ui.label(T['upgrade_sync_hint']).classes('text-caption text-grey-7 q-mb-xs')
            with ui.row().classes('gap-4 q-mb-sm'):
                upg_chk_pc_cfg = ui.checkbox(
                    T['upgrade_chk_pc_cfg'], value=False)
                upg_chk_pc_sec = ui.checkbox(
                    T['upgrade_chk_pc_sec'], value=False)
                upg_chk_zc_cfg = ui.checkbox(
                    T['upgrade_chk_zc_cfg'], value=False)

            upg_status_lbl = ui.label('').classes('text-caption text-grey-7 q-mb-sm')
            upg_log_area   = ui.textarea().classes('w-full font-mono').props('outlined rows=16 readonly label="Output"')
            upg_log_area.set_visibility(False)

            _upg_proc = {'proc': None}

            def _start_upgrade():
                # Build a single -config flag with comma-separated targets
                _cmd = ['sudo', 'bash', '/usr/local/bin/clawberry-workspace-sync.sh']
                _checked = [
                    upg_chk_pc_cfg.value,
                    upg_chk_pc_sec.value,
                    upg_chk_zc_cfg.value,
                ]
                if all(_checked):
                    _cmd += ['-config', 'all']
                elif any(_checked):
                    _targets = []
                    if upg_chk_pc_cfg.value: _targets.append('config.json')
                    if upg_chk_pc_sec.value: _targets.append('security.yml')
                    if upg_chk_zc_cfg.value: _targets.append('config.toml')
                    _cmd += ['-config', ','.join(_targets)]

                upg_log_area.set_visibility(True)
                upg_log_area.set_value('')
                _flags = ' '.join(_cmd[4:]) or '(no config files)'
                upg_status_lbl.set_text(f'⏳ Running upgrade script… flags: {_flags}')
                btn_upg_run.props('disabled loading')
                import threading, subprocess as _sp

                def _run_upg(_cmd=_cmd):
                    try:
                        proc = _sp.Popen(
                            _cmd,
                            stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True,
                            bufsize=1,
                        )
                        _upg_proc['proc'] = proc
                        for line in proc.stdout:
                            upg_log_area.set_value(upg_log_area.value + line)
                        proc.wait()
                        rc = proc.returncode
                        upg_status_lbl.set_text(
                            f'✅ Upgrade complete (exit {rc})' if rc == 0
                            else f'⚠️ Script exited with code {rc}'
                        )
                    except Exception as exc:
                        upg_status_lbl.set_text(f'❌ Error: {exc}')
                    finally:
                        _upg_proc['proc'] = None
                        btn_upg_run.props(remove='disabled loading')

                threading.Thread(target=_run_upg, daemon=True).start()

            btn_upg_run = ui.button(
                T['upgrade_btn_run'], on_click=_start_upgrade
            ).props('elevated color=orange-9')

    # ── Sidebar navigation wiring ──────────────────────────────────────────────
    def _switch_dash(name):
        zc_content.set_visibility(name == 'zeroclaw')
        pc_content.set_visibility(name == 'picoclaw')
        oc_content.set_visibility(name == 'openclaw')
        wifi_content.set_visibility(name == 'wifi')
        proxy_content.set_visibility(name == 'proxy')
        upgrade_content.set_visibility(name == 'upgrade')
        btn_zc._props['color']      = 'blue-8'        if name == 'zeroclaw' else 'grey-7'
        btn_pc._props['color']      = 'purple-8'      if name == 'picoclaw' else 'grey-7'
        btn_oc._props['color']      = 'teal-8'        if name == 'openclaw' else 'grey-7'
        btn_wifi._props['color']    = 'teal-8'        if name == 'wifi'     else 'grey-7'
        btn_proxy._props['color']   = 'indigo-7'      if name == 'proxy'    else 'grey-7'
        btn_upgrade._props['color'] = 'orange-9'      if name == 'upgrade'  else 'grey-7'
        btn_zc.update()
        btn_pc.update()
        btn_oc.update()
        btn_wifi.update()
        btn_proxy.update()
        btn_upgrade.update()

    btn_zc.on('click',      lambda: _switch_dash('zeroclaw'))
    btn_pc.on('click',      lambda: _switch_dash('picoclaw'))
    btn_oc.on('click',      lambda: _switch_dash('openclaw'))
    btn_wifi.on('click',    lambda: _switch_dash('wifi'))
    btn_proxy.on('click',   lambda: (_proxy_refresh(), _switch_dash('proxy')))
    btn_upgrade.on('click', lambda: _switch_dash('upgrade'))


ui.run(title='ClawBoard', port=8080, reload=False, host='0.0.0.0',
       storage_secret='clawboard-dashboard-secret',show=False)
