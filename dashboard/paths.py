"""Path constants for ClawBoard dashboard."""
import os

SCRIPT_DIR        = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATHS             = [os.path.join(SCRIPT_DIR, 'config/config.toml'), 'config.toml']
CONFIG_PATH       = next((p for p in PATHS if os.path.exists(p)), PATHS[0])
DEPLOY_CONFIG_PATH = '/var/lib/zeroclaw/.zeroclaw/config.toml'  # real zeroclaw config
PICOCLAW_CONFIG_PATH         = os.path.join(SCRIPT_DIR, 'config', 'config.json')
PICOCLAW_PID_FILE            = '/var/lib/picoclaw/.picoclaw/.picoclaw.pid'
PICOCLAW_SECURITY_YML        = '/var/lib/picoclaw/.picoclaw/.security.yml'
PICOCLAW_SECURITY_YML_LOCAL  = os.path.join(SCRIPT_DIR, 'config', 'security.yml')
PICOCLAW_DEPLOY_CONFIG_PATH  = '/var/lib/picoclaw/.picoclaw/config.json'
PICOCLAW_DEPLOY_SECURITY_PATH= '/var/lib/picoclaw/.picoclaw/.security.yml'

OPENCLAW_CONFIG_PATH         = os.path.join(SCRIPT_DIR, 'config', 'openclaw.json')
OPENCLAW_DEPLOY_CONFIG_PATH  = '/var/lib/openclaw/.openclaw/openclaw.json'
CLAWPROXY_CONFIG_PATH        = os.path.join(SCRIPT_DIR, 'clawproxy', 'config.toml')
CLAWPROXY_CONFIG_EXAMPLE     = os.path.join(SCRIPT_DIR, 'clawproxy', 'config.toml.example')
CLAWPROXY_DEPLOY_CONFIG_PATH = '/opt/clawproxy/config.toml'
CHARACTERS_DIR               = os.path.join(SCRIPT_DIR, 'characters')
