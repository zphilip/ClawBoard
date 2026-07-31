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

# ── Modular imports (Phase 1 extraction) ────────────────────────────────────
from dashboard.paths import (
    SCRIPT_DIR, PATHS, CONFIG_PATH, DEPLOY_CONFIG_PATH,
    PICOCLAW_CONFIG_PATH, PICOCLAW_PID_FILE,
    PICOCLAW_SECURITY_YML, PICOCLAW_SECURITY_YML_LOCAL,
    PICOCLAW_DEPLOY_CONFIG_PATH, PICOCLAW_DEPLOY_SECURITY_PATH,
    OPENCLAW_CONFIG_PATH, OPENCLAW_DEPLOY_CONFIG_PATH,
    CLAWPROXY_CONFIG_PATH, CLAWPROXY_CONFIG_EXAMPLE,
    CLAWPROXY_DEPLOY_CONFIG_PATH, CHARACTERS_DIR,
)
from dashboard.config_io import (
    _sudo_read_file, _read_picoclaw_pid_token, _read_security_yml_token,
    _parse_json5,
    load_picoclaw_config, load_openclaw_config,
    save_openclaw_config, deploy_openclaw_config, restart_openclaw_service,
    load_clawproxy_config, save_clawproxy_config, deploy_clawproxy_config,
    _OC_MACHINE, openclaw_service_status, openclaw_service_is_enabled,
    enable_openclaw_user_service, _read_openclaw_deploy_token,
    save_picoclaw_config, load_picoclaw_security, save_picoclaw_security,
    load_config, save_config, deploy_config,
    deploy_picoclaw_config, deploy_picoclaw_security,
)
from dashboard.auth import (
    AUTH_FILE, _load_auth, _save_auth, _hash_pw, _verify_pw, _is_authed, _logout,
)
from dashboard.provider_hints import (
    PROVIDER_IDS, CHANNEL_SCHEMAS, CHANNEL_LABELS,
    load_provider_hints, load_pc_provider_hints, load_oc_provider_hints,
    _oc_model_ref_text, _oc_provider_models,
)
from dashboard.panels_wifi import build_wifi_panel
from dashboard.panels_proxy import build_proxy_panel
from dashboard.panels_zeroclaw import build_zeroclaw_panel
from dashboard.panels_picoclaw import build_picoclaw_panel
from dashboard.panels_openclaw import build_openclaw_panel
from dashboard.panels_halite import build_halite_panel


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




CHANNEL_KEYS   = list(CHANNEL_SCHEMAS.keys())






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
    _ph_map    = {h['model_name']: h for h in _ph_hints if h.get('model_name')}
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
        # All form values are now collected by _pre_save() inside
        # build_zeroclaw_panel where the w_* widgets are in scope.
        # _pre_save writes directly into `conf` (mutable, shared by reference)
        # before do_save() / do_save_deploy() are called.
        pass

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
        btn_halite = ui.button('🏠 HA Lite',      icon='home'    ).props('flat align=left color=grey-7').classes('w-full')
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

    # ══ ZeroClaw panel ═══════════════════════════════════════════════════════
    zc_content = build_zeroclaw_panel(T, conf, zc_source, lang, other_lang,
        _ph_hints, _ph_map, _ph_models, _ph_pid_base, _ph_pid_models,
        provider_panels, channel_panels, do_status,
        _build_character_tab, _build_skills_tab,
        _loaded_from, to_int, to_float, lines_to_list,
        build_provider_card, build_channel_card,
        do_save, do_save_deploy, request)

    # ══ PicoClaw panel ═══════════════════════════════════════════════════════
    pc_content = build_picoclaw_panel(T, conf, lang, _ph_map, _ph_pid_base,
        _ph_pid_models, _pc_ph_hints, _pc_ph_map, _pc_ph_provs,
        _pc_ph_pid_base, _pc_ph_pid_models,
        _build_character_tab, _build_skills_tab,
        _get_lan_ip, restart_picoclaw_service, setup_pico_channel_token, request)

    # ══ OpenClaw panel ═══════════════════════════════════════════════════════
    oc_content = build_openclaw_panel(T, conf, lang, _ph_map, _ph_pid_base,
        _ph_pid_models, _oc_ph_hints, _oc_ph_map, _oc_ph_provs,
        _oc_ph_pid_base, _oc_ph_pid_models,
        _build_character_tab, _build_skills_tab, _get_lan_ip, request)

    # ══ WiFi panel ═══════════════════════════════════════════════════════════
    wifi_content = build_wifi_panel(T)

    # ══ Proxy & Upgrade panels ═══════════════════════════════════════════════
    proxy_content, upgrade_content, upgrade_content_inner, _proxy_refresh = build_proxy_panel(T)

    # ══ HA Lite panel ═══════════════════════════════════════════════════════════
    halite_content = build_halite_panel(T, conf, lang)

    # ── Sidebar navigation wiring ──────────────────────────────────────────────
    def _switch_dash(name):
        zc_content.set_visibility(name == 'zeroclaw')
        pc_content.set_visibility(name == 'picoclaw')
        oc_content.set_visibility(name == 'openclaw')
        halite_content.set_visibility(name == 'halite')
        wifi_content.set_visibility(name == 'wifi')
        proxy_content.set_visibility(name == 'proxy')
        upgrade_content.set_visibility(name == 'upgrade')
        btn_zc._props['color']      = 'blue-8'        if name == 'zeroclaw' else 'grey-7'
        btn_pc._props['color']      = 'purple-8'      if name == 'picoclaw' else 'grey-7'
        btn_oc._props['color']      = 'teal-8'        if name == 'openclaw' else 'grey-7'
        btn_halite._props['color']  = 'green-8'       if name == 'halite'   else 'grey-7'
        btn_wifi._props['color']    = 'teal-8'        if name == 'wifi'     else 'grey-7'
        btn_proxy._props['color']   = 'indigo-7'      if name == 'proxy'    else 'grey-7'
        btn_upgrade._props['color'] = 'orange-9'      if name == 'upgrade'  else 'grey-7'
        btn_zc.update()
        btn_pc.update()
        btn_oc.update()
        btn_halite.update()
        btn_wifi.update()
        btn_proxy.update()
        btn_upgrade.update()

    btn_zc.on('click',      lambda: _switch_dash('zeroclaw'))
    btn_pc.on('click',      lambda: _switch_dash('picoclaw'))
    btn_oc.on('click',      lambda: _switch_dash('openclaw'))
    btn_halite.on('click',  lambda: _switch_dash('halite'))
    btn_wifi.on('click',    lambda: _switch_dash('wifi'))
    btn_proxy.on('click',   lambda: (_proxy_refresh(), _switch_dash('proxy')))
    btn_upgrade.on('click', lambda: _switch_dash('upgrade'))


ui.run(title='ClawBoard', port=8080, reload=False, host='0.0.0.0',
       storage_secret='clawboard-dashboard-secret',show=False)
