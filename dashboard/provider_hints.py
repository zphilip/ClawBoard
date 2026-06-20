"""Provider hints, canonical slots, and channel schemas."""
import json
import os
from typing import Any

from .paths import SCRIPT_DIR
from .config_io import load_picoclaw_config

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

