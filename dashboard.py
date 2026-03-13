from nicegui import ui, app
from fastapi import Request
import toml, os, subprocess, hashlib, hmac, secrets, json, time as _time
from datetime import datetime
import locales.zh as zh_strings
import locales.en as en_strings

SCRIPT_DIR        = os.path.dirname(os.path.abspath(__file__))
PATHS             = [os.path.join(SCRIPT_DIR, 'config/config.toml'), 'config.toml']
CONFIG_PATH       = next((p for p in PATHS if os.path.exists(p)), PATHS[0])
DEPLOY_CONFIG_PATH = '/var/lib/zeroclaw/.zeroclaw/config.toml'  # real zeroclaw config

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
    """Load from the live deploy path first; fall back to local config/config.toml."""
    for path in [DEPLOY_CONFIG_PATH, CONFIG_PATH]:
        try:
            with open(path, 'r') as f:
                return toml.load(f)
        except Exception:
            continue
    return {}

def save_config(conf):
    with open(CONFIG_PATH, 'w') as f:
        toml.dump(conf, f)

def deploy_config():
    """Backup CONFIG_PATH → .bak, then sudo-copy to DEPLOY_CONFIG_PATH.
    Returns (ok: bool, message: str)."""
    import shutil
    # Step 1: backup local copy
    bak = CONFIG_PATH + '.bak'
    try:
        shutil.copy2(CONFIG_PATH, bak)
    except Exception as e:
        return False, f'Backup failed: {e}'
    # Step 2: ensure target directory exists, then copy
    r = subprocess.run(
        ['sudo', 'cp', CONFIG_PATH, DEPLOY_CONFIG_PATH],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        err = r.stderr.strip() or 'sudo cp failed (check sudoers)'
        return False, err
    return True, ''

def restart_service():
    r = subprocess.run(['sudo', 'systemctl', 'restart', 'zeroclaw.service'], capture_output=True, text=True)
    return r.returncode == 0, r.stderr.strip()

def service_status():
    r = subprocess.run(['systemctl', 'is-active', 'zeroclaw.service'], capture_output=True, text=True)
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
        conf['api_key']             = w_api_key.value
        conf['default_provider']    = w_default_provider.value
        conf['default_model']       = w_default_model.value
        conf['default_temperature'] = to_float(w_temperature.value, 0.7)
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
        for k in [k for k in list(ch_conf.keys()) if k not in ('cli', 'message_timeout_secs')]:
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
        ui.label(T['app_title']).classes('text-h6')
        with ui.row().classes('gap-1 items-center'):
            ui.button(T['lbl_lang_switch'],
                on_click=lambda: ui.navigate.to(f'/?lang={other_lang}')
            ).props('flat dense color=white no-caps')
            ui.button(icon='info', on_click=do_status).props('flat round dense color=white')
            ui.button(icon='logout', on_click=_logout).props('flat round dense color=white').tooltip('Logout')

    with ui.column().classes('w-full q-px-sm q-pt-sm'):
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

            # ══ General ══════════════════════════════════════════════════════
            with ui.tab_panel(t_gen):
                ui.label(T['section_api']).classes('text-subtitle2 text-grey-7 q-mt-sm')
                w_api_key = ui.input(T['lbl_api_key'], value=str(top.get('api_key', '')),
                    password=True, password_toggle_button=True).classes('w-full')
                cur_prov = str(top.get('default_provider', 'dashscope'))
                w_default_provider = ui.select(PROVIDER_IDS, label='default_provider',
                    value=cur_prov if cur_prov in PROVIDER_IDS else PROVIDER_IDS[0]).classes('w-full')
                w_default_model = ui.input('default_model',
                    value=str(top.get('default_model', 'anthropic/claude-sonnet-4-6'))).classes('w-full')
                w_temperature = ui.number('default_temperature',
                    value=top.get('default_temperature', 0.7), min=0.0, max=2.0, step=0.1).classes('w-full')
                ui.separator().classes('q-my-sm')
                ui.label(T['section_secrets']).classes('text-subtitle2 text-grey-7')
                w_secrets_encrypt = ui.checkbox('secrets.encrypt', value=bool(secrets_c.get('encrypt', True)))
                cur_id = str(identity.get('format', 'openclaw'))
                w_identity_format = ui.select(['openclaw', 'aieos'], label='identity.format',
                    value=cur_id if cur_id in ['openclaw','aieos'] else 'openclaw').classes('w-full')

            # ══ Providers ════════════════════════════════════════════════════
            with ui.tab_panel(t_prov):
                ui.label(T['section_providers']).classes('text-subtitle2 text-grey-7 q-mt-sm')
                ui.label(T['hint_providers']).classes('text-caption text-grey-5')
                provider_container = ui.column().classes('w-full')
                for alias, mp_data in conf.get('model_providers', {}).items():
                    build_provider_card(provider_container, alias, mp_data)
                ui.separator().classes('q-my-sm')
                with ui.row().classes('w-full gap-2 items-end'):
                    new_alias_input = ui.input(T['lbl_new_alias']).classes('flex-1')
                    def _add_provider():
                        alias = new_alias_input.value.strip()
                        if not alias: ui.notify(T['warn_alias_empty'], type='warning'); return
                        if alias in provider_panels: ui.notify(T['warn_alias_exists'].format(alias), type='warning'); return
                        build_provider_card(provider_container, alias, {}); new_alias_input.value = ''
                    ui.button(T['btn_add_provider'], on_click=_add_provider).props('outline color=blue')

            # ══ Autonomy ══════════════════════════════════════════════════════
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

            # ══ Agent ══════════════════════════════════════════════════════════
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

            # ══ Memory ═══════════════════════════════════════════════════════
            with ui.tab_panel(t_mem):
                ui.label(T['section_storage']).classes('text-subtitle2 text-grey-7 q-mt-sm')
                cur_mb = memory.get('backend', 'sqlite')
                w_mem_backend = ui.select(['sqlite', 'lucid', 'markdown', 'none'], label='backend',
                    value=cur_mb if cur_mb in ['sqlite','lucid','markdown','none'] else 'sqlite').classes('w-full')
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

            # ══ Comms ════════════════════════════════════════════════════════
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

            # ══ Channels ════════════════════════════════════════════════════
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

            # ══ Security ════════════════════════════════════════════════════
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
                    ui.separator()
                    ui.label('Paired Devices').classes('text-subtitle2 q-mt-xs')
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

            # ══ Features ════════════════════════════════════════════════════
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
                    w_hw_transport  = ui.select(['none', 'native', 'serial', 'probe'], label='transport',
                        value=cur_ht if cur_ht in ['none','native','serial','probe'] else 'none').classes('w-full')
                    w_hw_baud       = ui.number('baud_rate',           value=hardware.get('baud_rate', 115200), min=1200, step=9600).classes('w-full')
                    w_hw_datasheets = ui.checkbox('workspace_datasheets', value=bool(hardware.get('workspace_datasheets', False)))

            # ══ System ══════════════════════════════════════════════════════
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


ui.run(title='ClawBoard', port=8080, reload=False, host='0.0.0.0',
       storage_secret='clawboard-dashboard-secret',show=False)
