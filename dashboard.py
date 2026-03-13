from nicegui import ui
import toml
import os
import subprocess

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PATHS       = [os.path.join(SCRIPT_DIR, 'config/config.toml'), 'config.toml']
CONFIG_PATH = next((p for p in PATHS if os.path.exists(p)), PATHS[0])

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
    try:
        with open(CONFIG_PATH, 'r') as f:
            return toml.load(f)
    except Exception:
        return {}

def save_config(conf):
    with open(CONFIG_PATH, 'w') as f:
        toml.dump(conf, f)

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
def index():
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
                w_name = ui.select(PROVIDER_IDS, label='name / provider-id',
                    value=mp_data.get('name', alias) if mp_data.get('name', alias) in PROVIDER_IDS else PROVIDER_IDS[0]
                ).classes('w-full')
                w_base_url    = ui.input('base_url (optional override)', value=str(mp_data.get('base_url', ''))).classes('w-full')
                w_openai_auth = ui.checkbox('requires_openai_auth', value=bool(mp_data.get('requires_openai_auth', False)))
                w_api_key_mp  = ui.input('api_key (per-provider, optional)', value=str(mp_data.get('api_key', '')),
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
        # ── 通用 ──────────────────────────────────────────────────────────────
        conf['api_key']             = w_api_key.value
        conf['default_provider']    = w_default_provider.value
        conf['default_model']       = w_default_model.value
        conf['default_temperature'] = to_float(w_temperature.value, 0.7)
        conf.setdefault('secrets',  {})['encrypt'] = w_secrets_encrypt.value
        conf.setdefault('identity', {})['format']  = w_identity_format.value

        # ── Providers ─────────────────────────────────────────────────────────
        conf['model_providers'] = {}
        for alias, wmap in provider_panels.items():
            entry = {'name': wmap['name'].value, 'base_url': wmap['base_url'].value,
                     'requires_openai_auth': wmap['requires_openai_auth'].value}
            if wmap['api_key'].value: entry['api_key'] = wmap['api_key'].value
            conf['model_providers'][alias] = entry

        # ── Autonomy ──────────────────────────────────────────────────────────
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

        # ── Agent ─────────────────────────────────────────────────────────────
        ag = conf.setdefault('agent', {})
        ag['compact_context']      = w_agent_compact.value
        ag['parallel_tools']       = w_agent_parallel.value
        ag['max_tool_iterations']  = to_int(w_agent_max_iter.value, 10)
        ag['max_history_messages'] = to_int(w_agent_max_hist.value, 50)
        ag['tool_dispatcher']      = w_agent_tool_dispatcher.value

        # ── Observability ─────────────────────────────────────────────────────
        o = conf.setdefault('observability', {})
        o['backend']                 = w_obs_backend.value
        o['runtime_trace_mode']      = w_obs_trace_mode.value
        o['otel_endpoint']           = w_obs_otel_endpoint.value
        o['otel_service_name']       = w_obs_otel_service.value
        o['runtime_trace_path']      = w_obs_trace_path.value
        o['runtime_trace_max_entries'] = to_int(w_obs_trace_max.value, 200)

        # ── Skills ────────────────────────────────────────────────────────────
        sk = conf.setdefault('skills', {})
        sk['open_skills_enabled']  = w_skills_open.value
        sk['prompt_injection_mode'] = w_skills_mode.value

        # ── Memory ────────────────────────────────────────────────────────────
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

        # ── Gateway ───────────────────────────────────────────────────────────
        g = conf.setdefault('gateway', {})
        g['port']              = to_int(w_gw_port.value, 42617)
        g['host']              = w_gw_host.value
        g['require_pairing']   = w_gw_pairing.value
        g['allow_public_bind'] = w_gw_public.value

        conf.setdefault('tunnel', {})['provider'] = w_tunnel.value

        # ── Channels ──────────────────────────────────────────────────────────
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

        # ── Security ──────────────────────────────────────────────────────────
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
        so['enabled']           = w_sec_otp_enabled.value
        so['method']            = w_sec_otp_method.value
        so['token_ttl_secs']    = to_int(w_sec_otp_ttl.value, 30)
        so['cache_valid_secs']  = to_int(w_sec_otp_cache.value, 300)
        so['gated_actions']     = lines_to_list(w_sec_otp_actions.value)
        so['gated_domains']     = lines_to_list(w_sec_otp_domains.value)

        se = sec.setdefault('estop', {})
        se['enabled']               = w_sec_estop_enabled.value
        se['state_file']            = w_sec_estop_file.value
        se['require_otp_to_resume'] = w_sec_estop_otp.value

        # ── Reliability ───────────────────────────────────────────────────────
        r = conf.setdefault('reliability', {})
        r['provider_retries']             = to_int(w_rel_retries.value, 2)
        r['provider_backoff_ms']          = to_int(w_rel_backoff.value, 500)
        r['channel_initial_backoff_secs'] = to_int(w_rel_ch_backoff.value, 2)
        r['channel_max_backoff_secs']     = to_int(w_rel_ch_max.value, 60)

        # ── Scheduler ─────────────────────────────────────────────────────────
        s = conf.setdefault('scheduler', {})
        s['enabled']        = w_sched_enabled.value
        s['max_tasks']      = to_int(w_sched_tasks.value, 64)
        s['max_concurrent'] = to_int(w_sched_concurrent.value, 4)

        # ── Web Fetch ─────────────────────────────────────────────────────────
        wf = conf.setdefault('web_fetch', {})
        wf['enabled']          = w_wf_enabled.value
        wf['allowed_domains']  = lines_to_list(w_wf_domains.value)
        wf['blocked_domains']  = lines_to_list(w_wf_blocked.value)
        wf['max_response_size']= to_int(w_wf_max_size.value, 500000)
        wf['timeout_secs']     = to_int(w_wf_timeout.value, 30)

        # ── Web Search ────────────────────────────────────────────────────────
        ws = conf.setdefault('web_search', {})
        ws['enabled']     = w_ws_enabled.value
        ws['provider']    = w_ws_provider.value
        ws['max_results'] = to_int(w_ws_max.value, 5)
        ws['timeout_secs']= to_int(w_ws_timeout.value, 15)

        # ── HTTP Request ──────────────────────────────────────────────────────
        hr = conf.setdefault('http_request', {})
        hr['enabled']          = w_http_enabled.value
        hr['allowed_domains']  = lines_to_list(w_http_domains.value)
        hr['max_response_size']= to_int(w_http_max_size.value, 1000000)
        hr['timeout_secs']     = to_int(w_http_timeout.value, 30)

        # ── Browser ───────────────────────────────────────────────────────────
        br = conf.setdefault('browser', {})
        br['enabled']             = w_br_enabled.value
        br['allowed_domains']     = lines_to_list(w_br_domains.value)
        br['backend']             = w_br_backend.value
        br['native_headless']     = w_br_headless.value
        br['native_webdriver_url']= w_br_webdriver.value

        # ── Multimodal ────────────────────────────────────────────────────────
        mm = conf.setdefault('multimodal', {})
        mm['max_images']         = to_int(w_mm_images.value, 4)
        mm['max_image_size_mb']  = to_int(w_mm_image_size.value, 5)
        mm['allow_remote_fetch'] = w_mm_remote.value

        # ── Cost ──────────────────────────────────────────────────────────────
        c = conf.setdefault('cost', {})
        c['enabled']           = w_cost_enabled.value
        c['daily_limit_usd']   = to_float(w_cost_daily.value, 10.0)
        c['monthly_limit_usd'] = to_float(w_cost_monthly.value, 100.0)
        c['warn_at_percent']   = to_int(w_cost_warn.value, 80)
        c['allow_override']    = w_cost_override.value

        # ── Composio ──────────────────────────────────────────────────────────
        cp = conf.setdefault('composio', {})
        cp['enabled']   = w_comp_enabled.value
        cp['entity_id'] = w_comp_entity.value

        # ── Hooks ─────────────────────────────────────────────────────────────
        conf.setdefault('hooks', {})['enabled'] = w_hooks_enabled.value

        # ── Hardware ──────────────────────────────────────────────────────────
        hw = conf.setdefault('hardware', {})
        hw['enabled']             = w_hw_enabled.value
        hw['transport']           = w_hw_transport.value
        hw['baud_rate']           = to_int(w_hw_baud.value, 115200)
        hw['workspace_datasheets']= w_hw_datasheets.value

        # ── Transcription ─────────────────────────────────────────────────────
        tr = conf.setdefault('transcription', {})
        tr['enabled']          = w_tr_enabled.value
        tr['api_url']          = w_tr_url.value
        tr['model']            = w_tr_model.value
        tr['max_duration_secs']= to_int(w_tr_max_duration.value, 120)

        # ── Heartbeat ─────────────────────────────────────────────────────────
        hb = conf.setdefault('heartbeat', {})
        hb['enabled']          = w_hb_enabled.value
        hb['interval_minutes'] = to_int(w_hb_interval.value, 30)

        # ── Cron ──────────────────────────────────────────────────────────────
        cr = conf.setdefault('cron', {})
        cr['enabled']         = w_cron_enabled.value
        cr['max_run_history'] = to_int(w_cron_max_history.value, 50)

    def do_save():
        try:
            collect(); save_config(conf)
            ui.notify('✅ 配置已保存', type='positive')
        except Exception as e:
            ui.notify(f'❌ 保存失败: {e}', type='negative')

    def do_save_restart():
        try:
            collect(); save_config(conf)
            ok, err = restart_service()
            if ok:  ui.notify('✅ 已保存，zeroclaw.service 已重启', type='positive')
            else:   ui.notify(f'⚠️ 已保存，重启失败: {err or "需要 sudo 权限"}', type='warning')
        except Exception as e:
            ui.notify(f'❌ 操作失败: {e}', type='negative')

    def do_status():
        st = service_status()
        ui.notify(f'zeroclaw.service: {st}', type='positive' if st == 'active' else 'negative')

    # ── Shortcuts ────────────────────────────────────────────────────────────
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
        ui.label('⚙️ ZeroClaw ClawBoard').classes('text-h6')
        ui.button(icon='info', on_click=do_status).props('flat round dense color=white')

    with ui.column().classes('w-full q-px-sm q-pt-sm'):
        with ui.tabs().classes('w-full bg-blue-1') as tabs:
            t_gen   = ui.tab('通用',      icon='tune')
            t_prov  = ui.tab('Providers', icon='cloud')
            t_auto  = ui.tab('自主',      icon='psychology')
            t_agent = ui.tab('Agent',     icon='smart_toy')
            t_mem   = ui.tab('记忆',      icon='memory')
            t_comm  = ui.tab('通信',      icon='hub')
            t_ch    = ui.tab('Channels',  icon='forum')
            t_sec   = ui.tab('安全',      icon='security')
            t_feat  = ui.tab('功能',      icon='extension')
            t_sys   = ui.tab('系统',      icon='computer')

        with ui.tab_panels(tabs, value=t_gen).classes('w-full'):

            # ══ 通用 ════════════════════════════════════════════════════════
            with ui.tab_panel(t_gen):
                ui.label('API 与模型').classes('text-subtitle2 text-grey-7 q-mt-sm')
                w_api_key = ui.input('API Key (global default)', value=str(top.get('api_key', '')),
                    password=True, password_toggle_button=True).classes('w-full')
                cur_prov = str(top.get('default_provider', 'dashscope'))
                w_default_provider = ui.select(PROVIDER_IDS, label='default_provider',
                    value=cur_prov if cur_prov in PROVIDER_IDS else PROVIDER_IDS[0]).classes('w-full')
                w_default_model = ui.input('default_model',
                    value=str(top.get('default_model', 'anthropic/claude-sonnet-4-6'))).classes('w-full')
                w_temperature = ui.number('default_temperature',
                    value=top.get('default_temperature', 0.7), min=0.0, max=2.0, step=0.1).classes('w-full')

                ui.separator().classes('q-my-sm')
                ui.label('加密 & 身份').classes('text-subtitle2 text-grey-7')
                w_secrets_encrypt = ui.checkbox('secrets.encrypt', value=bool(secrets_c.get('encrypt', True)))
                cur_id = str(identity.get('format', 'openclaw'))
                w_identity_format = ui.select(['openclaw', 'aieos'], label='identity.format',
                    value=cur_id if cur_id in ['openclaw','aieos'] else 'openclaw').classes('w-full')

            # ══ Providers ═══════════════════════════════════════════════════
            with ui.tab_panel(t_prov):
                ui.label('模型提供商 (model_providers.*)').classes('text-subtitle2 text-grey-7 q-mt-sm')
                ui.label('每个卡片 = config.toml 中的 [model_providers.<alias>]').classes('text-caption text-grey-5')
                provider_container = ui.column().classes('w-full')
                for alias, mp_data in conf.get('model_providers', {}).items():
                    build_provider_card(provider_container, alias, mp_data)
                ui.separator().classes('q-my-sm')
                with ui.row().classes('w-full gap-2 items-end'):
                    new_alias_input = ui.input('新 alias (如 openai, groq, local…)').classes('flex-1')
                    def _add_provider():
                        alias = new_alias_input.value.strip()
                        if not alias: ui.notify('请输入 alias', type='warning'); return
                        if alias in provider_panels: ui.notify(f'alias "{alias}" 已存在', type='warning'); return
                        build_provider_card(provider_container, alias, {}); new_alias_input.value = ''
                    ui.button('+ 添加 Provider', on_click=_add_provider).props('outline color=blue')

            # ══ 自主 ════════════════════════════════════════════════════════
            with ui.tab_panel(t_auto):
                ui.label('权限等级').classes('text-subtitle2 text-grey-7 q-mt-sm')
                cur_lvl = autonomy.get('level', 'supervised')
                w_auto_level = ui.select(['read_only', 'supervised', 'full'], label='autonomy.level',
                    value=cur_lvl if cur_lvl in ['read_only','supervised','full'] else 'supervised').classes('w-full')
                w_auto_workspace        = ui.checkbox('workspace_only',                   value=autonomy.get('workspace_only', True))
                w_auto_require_approval = ui.checkbox('require_approval_for_medium_risk', value=autonomy.get('require_approval_for_medium_risk', True))
                w_auto_block_high       = ui.checkbox('block_high_risk_commands',          value=autonomy.get('block_high_risk_commands', True))

                ui.separator().classes('q-my-sm')
                w_auto_max_actions = ui.number('max_actions_per_hour',   value=autonomy.get('max_actions_per_hour', 20),   min=1,  step=1).classes('w-full')
                w_auto_max_cost    = ui.number('max_cost_per_day_cents',  value=autonomy.get('max_cost_per_day_cents', 500), min=0,  step=10).classes('w-full')

                ui.separator().classes('q-my-sm')
                ui.label('allowed_commands（每行一个）').classes('text-caption text-grey-6')
                w_auto_cmds = ui.textarea(value='\n'.join(autonomy.get('allowed_commands', []))).classes('w-full').props('outlined rows=5')
                ui.label('auto_approve（每行一个）').classes('text-caption text-grey-6')
                w_auto_approve = ui.textarea(value='\n'.join(autonomy.get('auto_approve', []))).classes('w-full').props('outlined rows=3')
                ui.label('always_ask（每行一个）').classes('text-caption text-grey-6')
                w_auto_always_ask = ui.textarea(value='\n'.join(autonomy.get('always_ask', []))).classes('w-full').props('outlined rows=3')
                ui.label('forbidden_paths（每行一个）').classes('text-caption text-grey-6')
                w_auto_forbidden = ui.textarea(value='\n'.join(autonomy.get('forbidden_paths', []))).classes('w-full').props('outlined rows=5')
                ui.label('allowed_roots（每行一个）').classes('text-caption text-grey-6')
                w_auto_allowed_roots = ui.textarea(value='\n'.join(autonomy.get('allowed_roots', []))).classes('w-full').props('outlined rows=3')
                ui.label('shell_env_passthrough（每行一个）').classes('text-caption text-grey-6')
                w_auto_shell_env = ui.textarea(value='\n'.join(autonomy.get('shell_env_passthrough', []))).classes('w-full').props('outlined rows=3')

            # ══ Agent ════════════════════════════════════════════════════════
            with ui.tab_panel(t_agent):
                ui.label('Agent 行为').classes('text-subtitle2 text-grey-7 q-mt-sm')
                w_agent_compact  = ui.checkbox('compact_context', value=agent_c.get('compact_context', False))
                w_agent_parallel = ui.checkbox('parallel_tools',  value=agent_c.get('parallel_tools', False))
                w_agent_max_iter = ui.number('max_tool_iterations',  value=agent_c.get('max_tool_iterations', 10),  min=1, step=1).classes('w-full')
                w_agent_max_hist = ui.number('max_history_messages', value=agent_c.get('max_history_messages', 50), min=1, step=5).classes('w-full')
                cur_disp = agent_c.get('tool_dispatcher', 'auto')
                w_agent_tool_dispatcher = ui.select(['auto', 'sequential', 'parallel'], label='tool_dispatcher',
                    value=cur_disp if cur_disp in ['auto','sequential','parallel'] else 'auto').classes('w-full')

                ui.separator().classes('q-my-sm')
                ui.label('可观测性 (observability)').classes('text-subtitle2 text-grey-7')
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
                ui.label('技能 (skills)').classes('text-subtitle2 text-grey-7')
                w_skills_open = ui.checkbox('open_skills_enabled', value=skills.get('open_skills_enabled', False))
                cur_pm = skills.get('prompt_injection_mode', 'full')
                w_skills_mode = ui.select(['full', 'compact'], label='prompt_injection_mode',
                    value=cur_pm if cur_pm in ['full','compact'] else 'full').classes('w-full')

            # ══ 记忆 ════════════════════════════════════════════════════════
            with ui.tab_panel(t_mem):
                ui.label('存储').classes('text-subtitle2 text-grey-7 q-mt-sm')
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
                ui.label('向量嵌入').classes('text-subtitle2 text-grey-7')
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
                ui.label('响应缓存 & 快照').classes('text-subtitle2 text-grey-7')
                w_mem_resp_cache    = ui.checkbox('response_cache_enabled', value=memory.get('response_cache_enabled', False))
                w_mem_snapshot      = ui.checkbox('snapshot_enabled',       value=memory.get('snapshot_enabled', False))
                w_mem_snap_hygiene  = ui.checkbox('snapshot_on_hygiene',    value=memory.get('snapshot_on_hygiene', False))
                w_mem_resp_ttl      = ui.number('response_cache_ttl_minutes',  value=memory.get('response_cache_ttl_minutes', 60),   min=1, step=5).classes('w-full')
                w_mem_resp_max      = ui.number('response_cache_max_entries',  value=memory.get('response_cache_max_entries', 5000), min=0, step=500).classes('w-full')

            # ══ 通信 ════════════════════════════════════════════════════════
            with ui.tab_panel(t_comm):
                ui.label('Gateway').classes('text-subtitle2 text-grey-7 q-mt-sm')
                w_gw_port    = ui.number('port', value=gateway.get('port', 42617), min=1024, max=65535, step=1).classes('w-full')
                w_gw_host    = ui.input('host',  value=str(gateway.get('host', '127.0.0.1'))).classes('w-full')
                w_gw_pairing = ui.checkbox('require_pairing',   value=gateway.get('require_pairing', True))
                w_gw_public  = ui.checkbox('allow_public_bind', value=gateway.get('allow_public_bind', False))

                ui.separator().classes('q-my-sm')
                ui.label('Tunnel').classes('text-subtitle2 text-grey-7')
                cur_tn = tunnel.get('provider', 'none')
                w_tunnel = ui.select(['none', 'cloudflare', 'ngrok'], label='tunnel.provider',
                    value=cur_tn if cur_tn in ['none','cloudflare','ngrok'] else 'none').classes('w-full')

                ui.separator().classes('q-my-sm')
                ui.label('channels_config 全局').classes('text-subtitle2 text-grey-7')
                w_cli_enabled = ui.checkbox('cli (启用 CLI 频道)', value=ch_conf_top.get('cli', True))
                w_msg_timeout = ui.number('message_timeout_secs', value=ch_conf_top.get('message_timeout_secs', 300), min=30, step=30).classes('w-full')

            # ══ Channels ═════════════════════════════════════════════════════
            with ui.tab_panel(t_ch):
                ui.label('频道配置 (channels_config.*)').classes('text-subtitle2 text-grey-7 q-mt-sm')
                ui.label('每个卡片 = config.toml 中的 [channels_config.<channel>]').classes('text-caption text-grey-5')
                channel_container = ui.column().classes('w-full')
                for ch_key in CHANNEL_KEYS:
                    if ch_key in ch_conf_top and isinstance(ch_conf_top[ch_key], dict):
                        build_channel_card(channel_container, ch_key, ch_conf_top[ch_key])
                ui.separator().classes('q-my-sm')
                with ui.row().classes('w-full gap-2 items-end'):
                    new_ch_select = ui.select({k: v for k, v in CHANNEL_LABELS.items()}, label='选择频道类型').classes('flex-1')
                    def _add_channel():
                        ch_key = new_ch_select.value
                        if not ch_key: ui.notify('请选择频道类型', type='warning'); return
                        if ch_key in channel_panels: ui.notify(f'{CHANNEL_LABELS.get(ch_key, ch_key)} 已添加', type='warning'); return
                        build_channel_card(channel_container, ch_key, {})
                    ui.button('+ 添加频道', on_click=_add_channel).props('outline color=green')

            # ══ 安全 ════════════════════════════════════════════════════════
            with ui.tab_panel(t_sec):
                with ui.expansion('资源限制 (security.resources)', icon='memory').classes('w-full'):
                    w_sec_mem         = ui.number('max_memory_mb',        value=sec_res.get('max_memory_mb', 512),        min=64,  step=64).classes('w-full')
                    w_sec_cpu         = ui.number('max_cpu_time_seconds', value=sec_res.get('max_cpu_time_seconds', 60),  min=5,   step=5).classes('w-full')
                    w_sec_procs       = ui.number('max_subprocesses',     value=sec_res.get('max_subprocesses', 10),      min=1,   step=1).classes('w-full')
                    w_sec_mem_monitor = ui.checkbox('memory_monitoring',  value=bool(sec_res.get('memory_monitoring', True)))

                with ui.expansion('沙箱 (security.sandbox)', icon='shield').classes('w-full'):
                    cur_sb = sec_sandbox.get('backend', 'auto')
                    w_sec_sandbox = ui.select(['auto', 'firejail', 'none'], label='sandbox.backend',
                        value=cur_sb if cur_sb in ['auto','firejail','none'] else 'auto').classes('w-full')

                with ui.expansion('审计 (security.audit)', icon='fact_check').classes('w-full'):
                    w_sec_audit_enabled  = ui.checkbox('enabled',     value=bool(sec_audit.get('enabled', True)))
                    w_sec_audit_log_path = ui.input('log_path',       value=str(sec_audit.get('log_path', 'audit.log'))).classes('w-full')
                    w_sec_audit_max      = ui.number('max_size_mb',   value=sec_audit.get('max_size_mb', 100), min=1, step=10).classes('w-full')
                    w_sec_audit_sign     = ui.checkbox('sign_events', value=bool(sec_audit.get('sign_events', False)))

                with ui.expansion('OTP (security.otp)', icon='lock').classes('w-full'):
                    w_sec_otp_enabled = ui.checkbox('enabled', value=bool(sec_otp.get('enabled', False)))
                    cur_om = sec_otp.get('method', 'totp')
                    w_sec_otp_method  = ui.select(['totp', 'pairing', 'cli-prompt'], label='method',
                        value=cur_om if cur_om in ['totp','pairing','cli-prompt'] else 'totp').classes('w-full')
                    w_sec_otp_ttl     = ui.number('token_ttl_secs',   value=sec_otp.get('token_ttl_secs', 30),     min=10, step=5).classes('w-full')
                    w_sec_otp_cache   = ui.number('cache_valid_secs', value=sec_otp.get('cache_valid_secs', 300),  min=30, step=30).classes('w-full')
                    ui.label('gated_actions（每行一个）').classes('text-caption text-grey-6')
                    w_sec_otp_actions = ui.textarea(value='\n'.join(sec_otp.get('gated_actions',
                        ['shell', 'file_write', 'browser_open', 'browser', 'memory_forget']))).classes('w-full').props('outlined rows=4')
                    ui.label('gated_domains（每行一个，支持 *.example.com）').classes('text-caption text-grey-6')
                    w_sec_otp_domains = ui.textarea(value='\n'.join(sec_otp.get('gated_domains', []))).classes('w-full').props('outlined rows=3')

                with ui.expansion('紧急停止 (security.estop)', icon='emergency').classes('w-full'):
                    w_sec_estop_enabled = ui.checkbox('enabled',               value=bool(sec_estop.get('enabled', False)))
                    w_sec_estop_file    = ui.input('state_file',               value=str(sec_estop.get('state_file', '~/.zeroclaw/estop-state.json'))).classes('w-full')
                    w_sec_estop_otp     = ui.checkbox('require_otp_to_resume', value=bool(sec_estop.get('require_otp_to_resume', True)))

                with ui.expansion('可靠性 (reliability)', icon='sync').classes('w-full'):
                    w_rel_retries    = ui.number('provider_retries',             value=reliability.get('provider_retries', 2),             min=0, step=1).classes('w-full')
                    w_rel_backoff    = ui.number('provider_backoff_ms',          value=reliability.get('provider_backoff_ms', 500),         min=0, step=100).classes('w-full')
                    w_rel_ch_backoff = ui.number('channel_initial_backoff_secs', value=reliability.get('channel_initial_backoff_secs', 2),  min=1, step=1).classes('w-full')
                    w_rel_ch_max     = ui.number('channel_max_backoff_secs',     value=reliability.get('channel_max_backoff_secs', 60),     min=5, step=5).classes('w-full')

                with ui.expansion('调度器 (scheduler)', icon='schedule').classes('w-full'):
                    w_sched_enabled    = ui.checkbox('enabled', value=scheduler.get('enabled', True))
                    w_sched_tasks      = ui.number('max_tasks',      value=scheduler.get('max_tasks', 64),     min=1, step=8).classes('w-full')
                    w_sched_concurrent = ui.number('max_concurrent', value=scheduler.get('max_concurrent', 4), min=1, step=1).classes('w-full')

            # ══ 功能 ════════════════════════════════════════════════════════
            with ui.tab_panel(t_feat):
                with ui.expansion('Web Fetch', icon='download').classes('w-full'):
                    w_wf_enabled  = ui.checkbox('enabled', value=web_fetch.get('enabled', False))
                    ui.label('allowed_domains（每行一个，* = 全部）').classes('text-caption text-grey-6')
                    w_wf_domains  = ui.textarea(value='\n'.join(web_fetch.get('allowed_domains', ['*']))).classes('w-full').props('outlined rows=3')
                    ui.label('blocked_domains（每行一个）').classes('text-caption text-grey-6')
                    w_wf_blocked  = ui.textarea(value='\n'.join(web_fetch.get('blocked_domains', []))).classes('w-full').props('outlined rows=3')
                    w_wf_max_size = ui.number('max_response_size (bytes)', value=web_fetch.get('max_response_size', 500000), min=1000, step=100000).classes('w-full')
                    w_wf_timeout  = ui.number('timeout_secs',              value=web_fetch.get('timeout_secs', 30),         min=5,    step=5).classes('w-full')

                with ui.expansion('Web Search', icon='search').classes('w-full'):
                    w_ws_enabled  = ui.checkbox('enabled', value=web_search.get('enabled', False))
                    cur_wsp = web_search.get('provider', 'duckduckgo')
                    w_ws_provider = ui.select(['duckduckgo', 'google', 'bing'], label='provider',
                        value=cur_wsp if cur_wsp in ['duckduckgo','google','bing'] else 'duckduckgo').classes('w-full')
                    w_ws_max      = ui.number('max_results', value=web_search.get('max_results', 5),  min=1, step=1).classes('w-full')
                    w_ws_timeout  = ui.number('timeout_secs', value=web_search.get('timeout_secs', 15), min=5, step=5).classes('w-full')

                with ui.expansion('HTTP Request', icon='http').classes('w-full'):
                    w_http_enabled   = ui.checkbox('enabled', value=http_request.get('enabled', False))
                    ui.label('allowed_domains（每行一个，* = 全部公网）').classes('text-caption text-grey-6')
                    w_http_domains   = ui.textarea(value='\n'.join(http_request.get('allowed_domains', []))).classes('w-full').props('outlined rows=3')
                    w_http_max_size  = ui.number('max_response_size (bytes)', value=http_request.get('max_response_size', 1000000), min=1000, step=100000).classes('w-full')
                    w_http_timeout   = ui.number('timeout_secs',              value=http_request.get('timeout_secs', 30),           min=5,    step=5).classes('w-full')

                with ui.expansion('Browser', icon='open_in_browser').classes('w-full'):
                    w_br_enabled   = ui.checkbox('enabled', value=browser.get('enabled', False))
                    ui.label('allowed_domains（每行一个）').classes('text-caption text-grey-6')
                    w_br_domains   = ui.textarea(value='\n'.join(browser.get('allowed_domains', []))).classes('w-full').props('outlined rows=3')
                    cur_bb = browser.get('backend', 'agent_browser')
                    w_br_backend   = ui.select(['agent_browser', 'rust_native', 'computer_use', 'auto'], label='backend',
                        value=cur_bb if cur_bb in ['agent_browser','rust_native','computer_use','auto'] else 'agent_browser').classes('w-full')
                    w_br_headless  = ui.checkbox('native_headless',      value=bool(browser.get('native_headless', True)))
                    w_br_webdriver = ui.input('native_webdriver_url',    value=str(browser.get('native_webdriver_url', 'http://127.0.0.1:9515'))).classes('w-full')

                with ui.expansion('多模态 (multimodal)', icon='image').classes('w-full'):
                    w_mm_images     = ui.number('max_images',        value=multimodal.get('max_images', 4),        min=1, step=1).classes('w-full')
                    w_mm_image_size = ui.number('max_image_size_mb', value=multimodal.get('max_image_size_mb', 5), min=1, step=1).classes('w-full')
                    w_mm_remote     = ui.checkbox('allow_remote_fetch', value=bool(multimodal.get('allow_remote_fetch', False)))

                with ui.expansion('费用控制 (cost)', icon='attach_money').classes('w-full'):
                    w_cost_enabled  = ui.checkbox('enabled',          value=cost.get('enabled', False))
                    w_cost_override = ui.checkbox('allow_override',   value=bool(cost.get('allow_override', False)))
                    w_cost_daily    = ui.number('daily_limit_usd',    value=cost.get('daily_limit_usd', 10.0),    min=0, step=1.0).classes('w-full')
                    w_cost_monthly  = ui.number('monthly_limit_usd',  value=cost.get('monthly_limit_usd', 100.0), min=0, step=5.0).classes('w-full')
                    w_cost_warn     = ui.number('warn_at_percent',    value=cost.get('warn_at_percent', 80),      min=10, max=100, step=5).classes('w-full')

                with ui.expansion('Composio', icon='hub').classes('w-full'):
                    w_comp_enabled = ui.checkbox('enabled', value=bool(composio_c.get('enabled', False)))
                    w_comp_entity  = ui.input('entity_id', value=str(composio_c.get('entity_id', 'default'))).classes('w-full')

                with ui.expansion('Hooks', icon='webhook').classes('w-full'):
                    w_hooks_enabled = ui.checkbox('hooks.enabled', value=bool(hooks.get('enabled', True)))

                with ui.expansion('Hardware', icon='developer_board').classes('w-full'):
                    w_hw_enabled    = ui.checkbox('enabled', value=bool(hardware.get('enabled', False)))
                    cur_ht = hardware.get('transport', 'none')
                    w_hw_transport  = ui.select(['none', 'native', 'serial', 'probe'], label='transport',
                        value=cur_ht if cur_ht in ['none','native','serial','probe'] else 'none').classes('w-full')
                    w_hw_baud       = ui.number('baud_rate',           value=hardware.get('baud_rate', 115200), min=1200, step=9600).classes('w-full')
                    w_hw_datasheets = ui.checkbox('workspace_datasheets', value=bool(hardware.get('workspace_datasheets', False)))

            # ══ 系统 ════════════════════════════════════════════════════════
            with ui.tab_panel(t_sys):
                ui.label('转录 (transcription)').classes('text-subtitle2 text-grey-7 q-mt-sm')
                w_tr_enabled      = ui.checkbox('enabled', value=transcription.get('enabled', False))
                w_tr_url          = ui.input('api_url', value=str(transcription.get('api_url', 'https://api.groq.com/openai/v1/audio/transcriptions'))).classes('w-full')
                w_tr_model        = ui.input('model',   value=str(transcription.get('model', 'whisper-large-v3-turbo'))).classes('w-full')
                w_tr_max_duration = ui.number('max_duration_secs', value=transcription.get('max_duration_secs', 120), min=10, step=10).classes('w-full')

                ui.separator().classes('q-my-sm')
                ui.label('心跳 (heartbeat)').classes('text-subtitle2 text-grey-7')
                w_hb_enabled  = ui.checkbox('enabled', value=heartbeat.get('enabled', False))
                w_hb_interval = ui.number('interval_minutes', value=heartbeat.get('interval_minutes', 30), min=1, step=5).classes('w-full')

                ui.separator().classes('q-my-sm')
                ui.label('定时任务 (cron)').classes('text-subtitle2 text-grey-7')
                w_cron_enabled     = ui.checkbox('enabled', value=cron.get('enabled', True))
                w_cron_max_history = ui.number('max_run_history', value=cron.get('max_run_history', 50), min=1, step=10).classes('w-full')

                ui.separator().classes('q-my-sm')
                ui.label('服务日志').classes('text-subtitle2 text-grey-7')
                with ui.row().classes('w-full gap-2'):
                    ui.button('最近日志', icon='article', on_click=lambda: ui.notify(
                        subprocess.getoutput('journalctl -u zeroclaw.service -n 30 --no-pager'),
                        multi_line=True, timeout=15000)).props('outline').classes('flex-1')
                    ui.button('服务状态', icon='info', on_click=do_status).props('outline').classes('flex-1')

        ui.separator()
        with ui.row().classes('w-full gap-2 q-pa-sm'):
            ui.button('💾 保存',       on_click=do_save).props('elevated').classes('flex-1 bg-blue text-white')
            ui.button('🔄 保存并重启', on_click=do_save_restart).props('elevated').classes('flex-1 bg-green text-white')


ui.run(title='ClawBoard', port=8080, reload=False, host='0.0.0.0')
