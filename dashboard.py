from nicegui import ui, app
from fastapi import Request
import tomlkit, os, sys, subprocess, hashlib, hmac, secrets, json, time as _time
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
    """Read channels.<section>.token from .security.yml using sudo.
    Returns (token, error_message).  error_message is '' on success."""
    raw, err = _sudo_read_file(PICOCLAW_SECURITY_YML)
    if err:
        return '', err
    try:
        import yaml as _yaml
        data = _yaml.safe_load(raw)
        tok = (data or {}).get('channels', {}).get(section, {}).get('token', '')
        return str(tok).strip(), ''
    except ImportError:
        pass
    # Fallback: simple line-by-line parser for the known structure
    in_channels = False
    in_section  = False
    indent_ch   = None   # indent of channel keys (e.g. 2)
    indent_sec  = None   # indent of section sub-keys
    for line in raw.splitlines():
        stripped = line.lstrip()
        indent   = len(line) - len(stripped)
        if stripped.startswith('channels:'):
            in_channels = True
            indent_ch   = None
            continue
        if not in_channels:
            continue
        if indent_ch is None and stripped and not stripped.startswith('#'):
            indent_ch = indent
        if indent_ch is not None and indent == indent_ch:
            in_section = stripped.startswith(f'{section}:')
            indent_sec = None
            continue
        if in_section:
            if indent_sec is None and stripped and not stripped.startswith('#'):
                indent_sec = indent
            if indent_sec is not None and indent == indent_sec:
                if stripped.startswith('token:'):
                    tok = stripped[len('token:'):].strip().strip('"\'')
                    return tok, ''
    return '', f'channels.{section}.token not found in {PICOCLAW_SECURITY_YML}'

def load_picoclaw_config():
    """Load picoclaw config.json; return empty dict on failure."""
    try:
        with open(PICOCLAW_CONFIG_PATH, 'r') as f:
            return json.load(f)
    except Exception:
        return {}

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
    'openrouter', 'anthropic', 'openai', 'ollama', 'gemini', 'venice',
    'vercel', 'cloudflare', 'moonshot', 'kimi-code', 'synthetic', 'opencode',
    'opencode-go', 'zai', 'glm', 'minimax', 'bedrock', 'qianfan', 'doubao',
    'qwen', 'dashscope', 'groq', 'mistral', 'xai', 'deepseek', 'together',
    'fireworks', 'novita', 'perplexity', 'cohere', 'copilot', 'lmstudio',
    'llamacpp', 'sglang', 'vllm', 'osaurus', 'nvidia',
    'custom:https://', 'anthropic-custom:https://',
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

def load_config():
    """Load from the live deploy path first; fall back to local config/config.toml.
    Uses tomlkit so that save_config preserves blank lines, comments and key order."""
    for path in [DEPLOY_CONFIG_PATH, CONFIG_PATH]:
        try:
            with open(path, 'r') as f:
                return tomlkit.load(f)
        except Exception:
            continue
    return tomlkit.document()

def save_config(conf):
    with open(CONFIG_PATH, 'w') as f:
        f.write(tomlkit.dumps(conf))

def deploy_config():
    """Backup CONFIG_PATH → .bak, then deploy to DEPLOY_CONFIG_PATH via sudo tee.
    Uses: sudo /usr/bin/tee /var/lib/zeroclaw/.zeroclaw/config.toml
    Requires the matching sudoers rule in daemon/sudoers.d-clawboard.
    Returns (ok: bool, message: str)."""
    import shutil
    # Step 1: backup local copy
    bak = CONFIG_PATH + '.bak'
    try:
        shutil.copy2(CONFIG_PATH, bak)
    except Exception as e:
        return False, f'Backup failed: {e}'
    # Step 2: read local config content
    try:
        with open(CONFIG_PATH, 'r') as f:
            content = f.read()
    except Exception as e:
        return False, f'Read failed: {e}'
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

    conf = load_config()
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
                    ui.label(f'[model_providers.{alias}]').classes('text-caption text-blue-7 text-bold')
                    def _rm(a=alias, c=card):
                        provider_panels.pop(a, None); c.delete()
                    ui.button(icon='delete', on_click=_rm).props('flat round dense color=negative')
                w_name = ui.select(PROVIDER_IDS, label=T['lbl_provider_name'],
                    value=mp_data.get('name', alias) if mp_data.get('name', alias) in PROVIDER_IDS else PROVIDER_IDS[0]
                ).classes('w-full')
                w_base_url    = ui.input(T['lbl_provider_base_url'], value=str(mp_data.get('base_url', ''))).classes('w-full')
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
                    ui.label(f'[channels_config.{ch_key}]').classes('text-caption text-green-7 text-bold')
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
        conf['api_key']               = w_api_key.value
        conf['default_provider']      = w_default_provider.value
        conf['default_model']         = w_default_model.value
        conf['default_temperature']   = to_float(w_temperature.value, 0.7)
        conf['provider_timeout_secs'] = to_int(w_prov_timeout.value, 120)
        conf.setdefault('secrets',  {})['encrypt'] = w_secrets_encrypt.value
        conf.setdefault('identity', {})['format']  = w_identity_format.value

        conf['model_providers'] = {}
        for alias, wmap in provider_panels.items():
            entry = {'name': wmap['name'].value, 'base_url': wmap['base_url'].value,
                     'requires_openai_auth': wmap['requires_openai_auth'].value}
            if wmap['api_key'].value: entry['api_key'] = wmap['api_key'].value
            conf['model_providers'][alias] = entry

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
        sk['prompt_injection_mode'] = w_skills_mode.value

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

        ch_conf = conf.setdefault('channels_config', {})
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
        except Exception as e:
            ui.notify(T['notify_save_fail'].format(e), type='negative')

    def do_save_restart():
        try:
            # 1. Collect form values → save to config/config.toml
            collect()
            save_config(conf)
            # 2. Backup config/config.toml → config/config.toml.bak
            #    then sudo-copy to DEPLOY_CONFIG_PATH
            ok_deploy, deploy_err = deploy_config()
            if not ok_deploy:
                ui.notify(f'⚠️ Saved locally but deploy failed: {deploy_err}', type='warning')
                return
            # 3. Restart the service
            ok_svc, svc_err = restart_service()
            if ok_svc:
                ui.notify(T['notify_saved_restarted'], type='positive')
            else:
                ui.notify(T['notify_restart_fail'].format(svc_err or T['notify_sudo_required']), type='warning')
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
    ch_conf_top  = conf.get('channels_config', {})
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
        btn_wifi    = ui.button('📶 WiFi Setup',   icon='wifi'    ).props('flat align=left color=grey-7').classes('w-full')
        btn_upgrade = ui.button('⬆ Upgrade',         icon='system_update_alt').props('flat align=left color=grey-7').classes('w-full')

    # ══ ZeroClaw Dashboard ════════════════════════════════════════════════════
    zc_content = ui.column().classes('w-full q-px-sm q-pt-sm')
    with zc_content:
        ui.label('🦾 ZeroClaw Dashboard').classes('text-h6 text-blue-9 q-mb-xs')
        with ui.tabs().classes('w-full bg-blue-1') as zc_sub_tabs:
            t_zc_wiz  = ui.tab(T['tab_wizard'],        icon='auto_fix_high')
            t_zc_cfg  = ui.tab(T['tab_configuration'], icon='settings')
            t_zc_pair = ui.tab(T['tab_pair_device'],   icon='devices')

        with ui.tab_panels(zc_sub_tabs, value=t_zc_cfg).classes('w-full'):

            # ── ZeroClaw › Wizard ──────────────────────────────────────────
            with ui.tab_panel(t_zc_wiz):
                ui.label('🧙 Quick Setup Wizard').classes('text-h6 text-blue-9 q-mb-xs')
                ui.label(
                    'Configure an AI provider and a messaging channel in 3 steps. '
                    'Click Apply at the end — then restart ZeroClaw to activate.'
                ).classes('text-caption text-grey-6 q-mb-md')

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
                            value='default').classes('w-full q-mb-sm')
                        wiz_prov_key = ui.input(
                            'API Key',
                            password=True, password_toggle_button=True).classes('w-full q-mb-sm')
                        wiz_prov_base = ui.input(
                            'base_url  (leave blank to use provider default)',
                            value='').classes('w-full q-mb-sm')
                        wiz_def_model = ui.input(
                            'default_model  (e.g. anthropic/claude-sonnet-4-6)',
                            value=str(top.get('default_model', ''))).classes('w-full')

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
                            slug = (h.get('model_name') or '').lower() \
                                .replace(' ', '_').replace('-', '_').replace('.', '_') \
                                .split('(')[0].rstrip('_')
                            if slug:
                                wiz_prov_alias.set_value(slug)
                            if h.get('model'):
                                wiz_def_model.set_value(h['model'])
                            # Set base_url LAST so hint value wins over any cascade
                            wiz_prov_base.set_value(h.get('api_base', ''))

                        def _fill_from_pid(e):
                            """Fill base_url and alias from provider-ID selection."""
                            pid = e.value
                            if not pid:
                                return
                            wiz_prov_base.set_value(_pid_base.get(pid, ''))
                            wiz_prov_alias.set_value(pid.split(':')[0])

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
                                f'Provider:      {wiz_prov_id.value}',
                                f'Alias:         {wiz_prov_alias.value or "default"}',
                                f'API Key:       {masked if key else "(not set)"}',
                                f'base_url:      {wiz_prov_base.value or "(provider default)"}',
                                f'default_model: {wiz_def_model.value or "(unchanged)"}',
                                f'Channel:       {ch_label}',
                            ]
                            wiz_summary.set_text('\n'.join(lines))

                        with ui.stepper_navigation():
                            ui.button('← Back', on_click=_wiz.previous).props('flat color=grey-7')
                            ui.button('Next →',
                                on_click=lambda: (_wiz_refresh_summary(), _wiz.next())
                            ).props('color=blue-8')

                    # ── Step 3: Apply ───────────────────────────────────────
                    with ui.step('wiz_apply', title='3  Apply', icon='check_circle'):
                        ui.label(
                            'Review the summary below, then click Apply & Save.'
                        ).classes('text-caption text-grey-6 q-mb-sm')
                        wiz_summary = ui.label('…').classes(
                            'text-caption text-mono bg-grey-2 q-pa-sm w-full q-mb-sm'
                        ).style('white-space: pre; border-radius: 4px;')

                        def _wiz_apply():
                            _wiz_refresh_summary()
                            alias = (wiz_prov_alias.value or 'default').strip()
                            # ── provider
                            prov_entry: dict = {
                                'name': wiz_prov_id.value,
                                'requires_openai_auth': False,
                            }
                            if wiz_prov_key.value:  prov_entry['api_key']  = wiz_prov_key.value
                            if wiz_prov_base.value: prov_entry['base_url'] = wiz_prov_base.value
                            conf.setdefault('model_providers', {})[alias] = prov_entry
                            conf['default_provider'] = wiz_prov_id.value
                            if wiz_def_model.value.strip():
                                conf['default_model'] = wiz_def_model.value.strip()
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
                                conf.setdefault('channels_config', {})[ch_key] = ch_entry
                            try:
                                save_config(conf)
                                ui.notify('✅ Wizard applied — restart ZeroClaw to activate', type='positive')
                            except Exception as _e:
                                ui.notify(f'❌ Save failed: {_e}', type='negative')

                        with ui.stepper_navigation():
                            ui.button('← Back', on_click=_wiz.previous).props('flat color=grey-7')
                            ui.button('✅ Apply & Save', on_click=_wiz_apply).props('color=green-8')

            # ── ZeroClaw › Configuration ───────────────────────────────────
            with ui.tab_panel(t_zc_cfg):
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
                        w_api_key = ui.input(T['lbl_api_key'], value=str(top.get('api_key', '')),
                            password=True, password_toggle_button=True).classes('w-full')
                        cur_prov = str(top.get('default_provider', 'dashscope'))
                        _eff_prov = cur_prov if cur_prov in PROVIDER_IDS else PROVIDER_IDS[0]
                        w_default_provider = ui.select(PROVIDER_IDS, label='default_provider',
                            value=_eff_prov).classes('w-full')
                        _cur_def_model = str(top.get('default_model', 'anthropic/claude-sonnet-4-6'))
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
                            value=top.get('default_temperature', 0.7), min=0.0, max=2.0, step=0.1).classes('w-full')
                        w_prov_timeout = ui.number('provider_timeout_secs',
                            value=top.get('provider_timeout_secs', 120), min=5, step=5).classes('w-full')
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
                        for alias, mp_data in conf.get('model_providers', {}).items():
                            build_provider_card(provider_container, alias, mp_data)
                        ui.separator().classes('q-my-sm')
                        ui.label('➕ Add provider').classes('text-caption text-blue-7')
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
                                'Alias  [model_providers.<alias>]  — auto-filled, edit for duplicates',
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
                                'name':     pid,
                                'base_url': _ph_pid_base.get(pid, ''),
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
                        cur_tm = obs.get('runtime_trace_mode', 'none')
                        w_obs_trace_mode = ui.select(['none', 'rolling', 'full'], label='runtime_trace_mode',
                            value=cur_tm if cur_tm in ['none','rolling','full'] else 'none').classes('w-full')
                        w_obs_otel_endpoint = ui.input('otel_endpoint', value=str(obs.get('otel_endpoint', 'http://localhost:4318'))).classes('w-full')
                        w_obs_otel_service  = ui.input('otel_service_name', value=str(obs.get('otel_service_name', 'zeroclaw'))).classes('w-full')
                        w_obs_trace_path    = ui.input('runtime_trace_path', value=str(obs.get('runtime_trace_path', 'state/runtime-trace.jsonl'))).classes('w-full')
                        w_obs_trace_max     = ui.number('runtime_trace_max_entries', value=obs.get('runtime_trace_max_entries', 200), min=10, step=50).classes('w-full')
                        ui.separator().classes('q-my-sm')
                        ui.label(T['section_skills']).classes('text-subtitle2 text-grey-7')
                        w_skills_open = ui.checkbox('open_skills_enabled', value=skills.get('open_skills_enabled', False))
                        cur_pm = skills.get('prompt_injection_mode', 'full')
                        w_skills_mode = ui.select(['full', 'compact'], label='prompt_injection_mode',
                            value=cur_pm if cur_pm in ['full','compact'] else 'full').classes('w-full')

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
                        cur_tn = tunnel.get('provider', 'none')
                        w_tunnel = ui.select(['none', 'cloudflare', 'ngrok'], label='tunnel.provider',
                            value=cur_tn if cur_tn in ['none','cloudflare','ngrok'] else 'none').classes('w-full')
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
                        with ui.expansion('🔒 Dashboard Access', icon='vpn_key').classes('w-full'):
                            ui.label('Change Password').classes('text-subtitle2 q-mt-xs')
                            w_cur_pw  = ui.input('Current password', password=True, password_toggle_button=True).classes('w-full')
                            w_new_pw  = ui.input('New password',     password=True, password_toggle_button=True).classes('w-full')
                            w_new_pw2 = ui.input('Confirm new',      password=True, password_toggle_button=True).classes('w-full')
                            def do_change_pw():
                                d = _load_auth()
                                if not d or not _verify_pw(w_cur_pw.value, d['password_hash']):
                                    ui.notify('❌ Current password incorrect', type='negative'); return
                                if len(w_new_pw.value) < 6:
                                    ui.notify('Min 6 characters', type='warning'); return
                                if w_new_pw.value != w_new_pw2.value:
                                    ui.notify('Passwords do not match', type='warning'); return
                                d['password_hash'] = _hash_pw(w_new_pw.value)
                                _save_auth(d)
                                ui.notify('✅ Password changed', type='positive')
                                w_cur_pw.set_value(''); w_new_pw.set_value(''); w_new_pw2.set_value('')
                            ui.button('Change Password', on_click=do_change_pw).props('outline color=blue').classes('q-mb-sm')
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
                            cur_wsp = web_search.get('provider', 'duckduckgo')
                            w_ws_provider = ui.select(['duckduckgo', 'google', 'bing'], label='provider',
                                value=cur_wsp if cur_wsp in ['duckduckgo','google','bing'] else 'duckduckgo').classes('w-full')
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
                    ui.button(T['btn_save_restart'], on_click=do_save_restart).props('elevated').classes('flex-1 bg-green text-white')

            # ── ZeroClaw › Pair Device ─────────────────────────────────────
            with ui.tab_panel(t_zc_pair):
                # ── Gateway pair code ──────────────────────────────────────
                with ui.card().classes('w-full q-mb-sm'):
                    ui.label('🔑 Gateway Pair Code').classes('text-subtitle1 text-bold q-mb-xs')
                    ui.label(
                        'Generate a new pair code for the ZeroClaw gateway. '
                        'The code will also be sent to the 2.13″ e-ink display.'
                    ).classes('text-caption text-grey-6 q-mb-sm')
                    paircode_box = ui.card().classes('w-full bg-grey-2 q-pa-md items-center')
                    with paircode_box:
                        paircode_lbl = ui.label('…').classes(
                            'text-h4 text-bold text-blue-9 text-center letter-spacing-wide'
                        )
                        paircode_status = ui.label('Loading…').classes('text-caption text-grey-6 text-center q-mt-xs')

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
                    paircode_status.set_text('Press a button below to load or generate a pair code')

                    with ui.row().classes('w-full gap-2 q-mt-sm'):
                        ui.button('🔄 Refresh Code + Show on Display', on_click=lambda: _fetch_paircode(new=False, push_display=True)).props(
                            'outline color=blue-8'
                        ).classes('flex-1')
                        ui.button('🔑 Generate New Code + Show on Display', on_click=lambda: _fetch_paircode(new=True, push_display=True)).props(
                            'elevated color=blue-8'
                        ).classes('flex-1')

                # ── Paired Devices ─────────────────────────────────────────
                with ui.card().classes('w-full q-mb-sm'):
                    ui.label('📱 Paired Devices').classes('text-subtitle1 text-bold q-mb-xs')
                    device_list = ui.column().classes('w-full')
                    def _refresh_devices():
                        device_list.clear()
                        d2 = _load_auth()
                        devs = d2.get('paired_devices', []) if d2 else []
                        if not devs:
                            with device_list:
                                ui.label('No paired devices').classes('text-caption text-grey-5')
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
                        ui.notify('✅ Invite link copied to clipboard', type='positive')
                    ui.button('🔗 Generate Invite Link', on_click=_gen_invite).props('outline color=green')

    # ══ PicoClaw Dashboard ════════════════════════════════════════════════════
    pc_content = ui.column().classes('w-full q-px-sm q-pt-sm')
    pc_content.set_visibility(False)
    with pc_content:
        ui.label('🐾 PicoClaw Dashboard').classes('text-h6 text-purple-8 q-mb-xs')
        with ui.tabs().classes('w-full bg-purple-1') as pc_sub_tabs:
            t_pc_wiz  = ui.tab(T['pc_tab_wizard'],     icon='auto_fix_high')
            t_pc_cfg  = ui.tab(T['tab_configuration'], icon='settings')
            t_pc_pair = ui.tab(T['tab_pair_device'],   icon='devices')

        with ui.tab_panels(pc_sub_tabs, value=t_pc_cfg).classes('w-full'):

            # ── PicoClaw › Wizard ──────────────────────────────────────────
            with ui.tab_panel(t_pc_wiz):
                ui.label('🧙 Quick Setup Wizard').classes('text-h6 text-purple-8 q-mb-xs')
                ui.label(
                    'Configure an AI provider in 2 steps. '
                    'Click Apply — then restart PicoClaw to activate.'
                ).classes('text-caption text-grey-6 q-mb-md')

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

                        pc_wiz_prov      = ui.select(_pc_ph_provs or [''], label='provider',
                            value=_pc_ph_provs[0] if _pc_ph_provs else '').classes('w-full q-mb-sm')
                        pc_wiz_model_name= ui.select(
                            options=list(_pc_ph_map.keys()),
                            label='model_name',
                            value=None,
                            with_input=True,
                            new_value_mode='add-unique',
                        ).classes('w-full q-mb-sm')
                        pc_wiz_model     = ui.input('model  (actual model id sent to provider)', value='').classes('w-full q-mb-sm')
                        pc_wiz_api_base  = ui.input('api_base', value='').classes('w-full q-mb-sm')
                        pc_wiz_api_key   = ui.input('api_key', password=True, password_toggle_button=True).classes('w-full')

                        def _pc_wiz_fill_hint(e):
                            h = _pc_ph_map.get(e.value) if e.value else None
                            if not h: return
                            prov = h.get('provider', '')
                            if prov in _pc_ph_provs: pc_wiz_prov.set_value(prov)
                            pc_wiz_model_name.set_value(h.get('model_name', ''))
                            pc_wiz_model.set_value(h.get('model', ''))
                            pc_wiz_api_base.set_value(h.get('api_base', ''))
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

                    # ── Step 2: Apply ────────────────────────────────────────
                    with ui.step('pc_wiz_apply', title='2  Apply', icon='check_circle'):
                        ui.label('Review and apply the provider settings.').classes('text-caption text-grey-6 q-mb-sm')

                        def _pc_wiz_summary():
                            return (
                                f'provider: {pc_wiz_prov.value}\n'
                                f'model_name: {pc_wiz_model_name.value or "(unchanged)"}\n'
                                f'model: {pc_wiz_model.value or "(unchanged)"}\n'
                                f'api_base: {pc_wiz_api_base.value or "(provider default)"}'
                            )
                        pc_wiz_summary_lbl = ui.label('').classes('text-caption text-grey-7 q-mb-sm')

                        def _pc_wiz_refresh_summary():
                            pc_wiz_summary_lbl.set_text(_pc_wiz_summary())
                        _pc_wiz.on('transition', lambda _: _pc_wiz_refresh_summary())

                        def _pc_wiz_apply():
                            data = load_picoclaw_config()
                            sec  = load_picoclaw_security()
                            ad = data.setdefault('agents', {}).setdefault('defaults', {})
                            if pc_wiz_prov.value:       ad['provider']    = pc_wiz_prov.value
                            if pc_wiz_model_name.value: ad['model_name']  = pc_wiz_model_name.value
                            if pc_wiz_model.value:      ad['model']       = pc_wiz_model.value
                            # Inject model into model_list if not already present
                            mname = pc_wiz_model_name.value
                            if mname:
                                ml = data.setdefault('model_list', [])
                                existing = [e for e in ml if e.get('model_name') == mname]
                                if not existing:
                                    entry = {'model_name': mname, 'model': pc_wiz_model.value,
                                             'api_base': pc_wiz_api_base.value}
                                    ml.append(entry)
                                else:
                                    existing[0]['api_base'] = pc_wiz_api_base.value
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
                pc_channels  = pc_conf.get('channels',  {})
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
                pc_sec_channels = pc_sec.get('channels',   {})
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

                    # session
                    data.setdefault('session', {})['dm_scope'] = pc_w_dm_scope.value

                    # agents.defaults
                    ad = data.setdefault('agents', {}).setdefault('defaults', {})
                    ad['workspace']                  = pc_w_workspace.value
                    ad['restrict_to_workspace']      = pc_w_restrict.value
                    ad['allow_read_outside_workspace']= pc_w_allow_read_outside.value
                    ad['provider']                   = pc_w_provider.value
                    ad['model_name']                 = pc_w_model_name.value
                    ad['model']                      = pc_w_model.value
                    ad['max_tokens']                 = to_int(pc_w_max_tokens.value, 8192)
                    ad['max_tool_iterations']        = to_int(pc_w_max_iter.value, 50)
                    ad['summarize_message_threshold']= to_int(pc_w_sum_threshold.value, 20)
                    ad['summarize_token_percent']    = to_int(pc_w_sum_percent.value, 75)

                    # model_list → config.json (no api_keys); api_keys → security.yml
                    # Rebuild sec['model_list'] from scratch so stale names (e.g. MiniMax-M2.5:0)
                    # that are no longer in the UI are pruned. Existing keys in the file are
                    # preserved when the widget textarea is empty (user didn't change them).
                    _old_sec_ml = dict(sec.get('model_list', {}))
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
                    ch     = data.setdefault('channels', {})
                    sec_ch = sec.setdefault('channels', {})
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

                with ui.tabs().classes('w-full bg-purple-1') as pc_cfg_tabs:
                    t_pc_gen   = ui.tab(T['pc_tab_general'],  icon='tune')
                    t_pc_models= ui.tab(T['pc_tab_models'],   icon='cloud')
                    t_pc_ch    = ui.tab(T['pc_tab_channels'], icon='forum')
                    t_pc_tools = ui.tab(T['pc_tab_tools'],    icon='construction')
                    t_pc_sys   = ui.tab(T['pc_tab_system'],   icon='computer')

                with ui.tab_panels(pc_cfg_tabs, value=t_pc_gen).classes('w-full'):

                    # ── General ──────────────────────────────────────────────
                    with ui.tab_panel(t_pc_gen):
                        ui.label(T['pc_section_session']).classes('text-subtitle2 text-grey-7 q-mt-sm')
                        dm_opts = ['per-channel-peer', 'global', 'per-channel']
                        cur_dm  = pc_session.get('dm_scope', 'per-channel-peer')
                        pc_w_dm_scope = ui.select(dm_opts, label='session.dm_scope',
                            value=cur_dm if cur_dm in dm_opts else dm_opts[0]).classes('w-full')

                        ui.separator().classes('q-my-sm')
                        ui.label(T['pc_section_agent_def']).classes('text-subtitle2 text-grey-7')
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

                        _pc_cur_model = str(pc_agents.get('model', 'qwen3.5-plus'))
                        pc_w_model = ui.input('model  (actual model id sent to provider)',
                            value=_pc_cur_model).classes('w-full')

                        # Auto-fill: provider change → update model_name list + api_base hint label
                        def _pc_gen_prov_change(e):
                            prov = e.value or ''
                            mnames = _pc_ph_pid_models.get(prov, list(_pc_ph_map.keys()))
                            cur = pc_w_model_name.value
                            new_val = cur if cur in mnames else (mnames[0] if mnames else cur)
                            pc_w_model_name.set_options(list(mnames), value=new_val)
                            h = _pc_ph_map.get(new_val, {})
                            if h.get('model'): pc_w_model.set_value(h['model'])
                        pc_w_provider.on_value_change(_pc_gen_prov_change)

                        # Auto-fill: model_name change → update model field
                        def _pc_gen_mname_change(e):
                            h = _pc_ph_map.get(e.value) if e.value else None
                            if h and h.get('model'): pc_w_model.set_value(h['model'])
                        pc_w_model_name.on_value_change(_pc_gen_mname_change)
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
                    # Compose runtime token: "pico-" + pid.Token + security.yml pico.token
                    _pid_tok  = ''
                    _pid_err  = ''
                    _sec_tok  = ''
                    _sec_err  = ''
                    _pid_tok, _pid_err = _read_picoclaw_pid_token()
                    _sec_tok, _sec_err = _read_security_yml_token('pico')
                    if _pid_tok and _sec_tok:
                        pico_token = f'pico-{_pid_tok}{_sec_tok}'
                    elif _pid_tok:
                        pico_token = f'pico-{_pid_tok}'  # fallback: no config token
                    else:
                        pico_token = _sec_tok             # fallback: no pid file
                    pico_port = int(pc_gateway.get('port', 18790) or 18790)
                    pico_host = request.url.hostname or 'localhost'
                    pico_scheme = request.url.scheme or 'http'
                    pico_url = f'{pico_scheme}://{pico_host}:{pico_port}?token={pico_token}' if pico_token else ''
                    pico_qr_url = f'https://quickchart.io/qr?size=260&margin=1&text={quote(pico_url, safe="")}' if pico_url else ''

                    if _pid_err:
                        ui.label(f'⚠️ {_pid_err}').classes('text-warning text-caption q-mt-xs')
                    if _sec_err:
                        ui.label(f'⚠️ {_sec_err}').classes('text-warning text-caption q-mt-xs')
                    if pico_token:
                        ui.input('Runtime token  (pico- + pid.Token + pico.token)', value=pico_token).props('readonly').classes('w-full q-mt-sm')
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

    # ── Sidebar navigation wiring ──────────────────────────────────────────────
    # ══ WiFi Setup ════════════════════════════════════════════════════════════
    wifi_content = ui.column().classes('w-full q-px-sm q-pt-sm')
    wifi_content.set_visibility(False)
    with wifi_content:
        ui.label('📶 WiFi Setup').classes('text-h6 text-teal-8 q-mb-xs')
        with ui.card().classes('w-full q-pa-md'):
            ui.label('Wireless Network Configuration').classes('text-subtitle1 text-bold q-mb-xs')
            ui.label(
                'Launch the wifi-connect captive portal. The device will broadcast a '
                '"ClawBerry WiFi Setup" access point. Connect to it with any device, '
                'then choose your network and enter the password.'
            ).classes('text-caption text-grey-6 q-mb-md')

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
                    except Exception as exc:
                        wifi_status_lbl.set_text(f'❌ Error: {exc}')
                    finally:
                        _wifi_proc['proc'] = None
                        btn_wifi_start.props(remove='disabled loading')
                        btn_wifi_stop.props('disabled')

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
                    '▶ Start WiFi Setup', on_click=_start_wifi_setup
                ).props('elevated color=teal-8')
                btn_wifi_stop = ui.button(
                    '■ Stop', on_click=_stop_wifi_setup
                ).props('outline color=negative disabled')

    # ══ Upgrade ═══════════════════════════════════════════════════════════════
    upgrade_content = ui.column().classes('w-full q-px-sm q-pt-sm')
    upgrade_content.set_visibility(False)
    with upgrade_content:
        ui.label('⬆ Upgrade').classes('text-h6 text-orange-9 q-mb-xs')
        with ui.card().classes('w-full q-pa-md'):
            ui.label('System Upgrade').classes('text-subtitle1 text-bold q-mb-xs')
            ui.label(
                'Run the ClawBerry workspace-sync upgrade script. '
                'Output is streamed below in real time.'
            ).classes('text-caption text-grey-6 q-mb-md')

            upg_status_lbl = ui.label('').classes('text-caption text-grey-7 q-mb-sm')
            upg_log_area   = ui.textarea().classes('w-full font-mono').props('outlined rows=16 readonly label="Output"')
            upg_log_area.set_visibility(False)

            _upg_proc = {'proc': None}

            def _start_upgrade():
                upg_log_area.set_visibility(True)
                upg_log_area.set_value('')
                upg_status_lbl.set_text('⏳ Running upgrade script…')
                btn_upg_run.props('disabled loading')
                import threading, subprocess as _sp

                def _run_upg():
                    try:
                        proc = _sp.Popen(
                            ['sudo', 'bash', '/usr/local/bin/clawberry-workspace-sync.sh'],
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
                '▶ Run Upgrade', on_click=_start_upgrade
            ).props('elevated color=orange-9')

    # ── Sidebar navigation wiring ──────────────────────────────────────────────
    def _switch_dash(name):
        zc_content.set_visibility(name == 'zeroclaw')
        pc_content.set_visibility(name == 'picoclaw')
        wifi_content.set_visibility(name == 'wifi')
        upgrade_content.set_visibility(name == 'upgrade')
        btn_zc._props['color']      = 'blue-8'   if name == 'zeroclaw' else 'grey-7'
        btn_pc._props['color']      = 'purple-8' if name == 'picoclaw' else 'grey-7'
        btn_wifi._props['color']    = 'teal-8'   if name == 'wifi'     else 'grey-7'
        btn_upgrade._props['color'] = 'orange-9' if name == 'upgrade'  else 'grey-7'
        btn_zc.update()
        btn_pc.update()
        btn_wifi.update()
        btn_upgrade.update()

    btn_zc.on('click',      lambda: _switch_dash('zeroclaw'))
    btn_pc.on('click',      lambda: _switch_dash('picoclaw'))
    btn_wifi.on('click',    lambda: _switch_dash('wifi'))
    btn_upgrade.on('click', lambda: _switch_dash('upgrade'))


ui.run(title='ClawBoard', port=8080, reload=False, host='0.0.0.0',
       storage_secret='clawboard-dashboard-secret',show=False)
