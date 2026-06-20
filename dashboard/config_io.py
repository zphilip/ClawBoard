"""Config load, save, and deploy helpers for all services."""
import json
import os
import re as _re
import shutil
import subprocess

import tomlkit

from .paths import (
    SCRIPT_DIR, CONFIG_PATH, DEPLOY_CONFIG_PATH,
    PICOCLAW_CONFIG_PATH, PICOCLAW_PID_FILE,
    PICOCLAW_SECURITY_YML, PICOCLAW_SECURITY_YML_LOCAL,
    PICOCLAW_DEPLOY_CONFIG_PATH, PICOCLAW_DEPLOY_SECURITY_PATH,
    OPENCLAW_CONFIG_PATH, OPENCLAW_DEPLOY_CONFIG_PATH,
    CLAWPROXY_CONFIG_PATH, CLAWPROXY_CONFIG_EXAMPLE,
    CLAWPROXY_DEPLOY_CONFIG_PATH,
)

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

