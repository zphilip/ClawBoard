#!/usr/bin/env bash
# clawberry-workspace-sync.sh
# Syncs workspace content from the ClawBoard GitHub repo into the local
# runtime directories for picoclaw and zeroclaw.
#
# Source (GitHub):
#   https://github.com/zphilip/ClawBoard.git
#     picoclaw/workspace  →  /var/lib/picoclaw/.picoclaw/workspace
#     zeroclaw/workspace  →  /var/lib/zeroclaw/.zeroclaw/workspace
#
# Usage:
#   clawberry-workspace-sync.sh [OPTIONS]
#
#   -config <target>   Also sync the specified config file(s).
#                      Targets: all | config.json | config.toml | security.yml
#                      May be repeated: -config config.json -config config.toml
#
#   Without -config the three protected files (config.json, config.toml,
#   .security.yml) are NEVER touched; only workspace/dashboard/binary files
#   are updated.
#
# Examples:
#   clawberry-workspace-sync.sh
#   clawberry-workspace-sync.sh -config all
#   clawberry-workspace-sync.sh -config config.json -config security.yml
#
# Strategy:
#   • Clone repo into a temp dir (sparse, no history, saves bandwidth)
#   • rsync each workspace subtree into the target, preserving any local
#     files the service may have added (--update: skip if dest is newer)
#   • Never delete files the service has created locally
#   • Set correct ownership after copy

set -euo pipefail

REPO_URL="https://gh.fhjhy.top/https://github.com/zphilip/ClawBoard.git"
REPO_URL_FALLBACK="https://github.com/zphilip/ClawBoard.git"

# Path where the sync script should live on the device
SCRIPT_PATH="/usr/local/bin/clawberry-workspace-sync.sh"

PICOCLAW_SRC="picoclaw"
ZEROCLAW_SRC="zeroclaw"
PICOCLAW_WORKSPACE_SRC="picoclaw/workspace"
ZEROCLAW_WORKSPACE_SRC="zeroclaw/workspace"

CLAWBOARD_DST="/opt/clawboard"
CLAWBOARD_USER="zero"

PICOCLAW_WORKSPACE_DST="/var/lib/picoclaw/.picoclaw/workspace"
ZEROCLAW_WORKSPACE_DST="/var/lib/zeroclaw/.zeroclaw/workspace"
PICOCLAW_SKILLS_DST="/var/lib/picoclaw/.picoclaw/workspace/skills"
ZEROCLAW_SKILLS_DST="/var/lib/zeroclaw/.zeroclaw/workspace/skills"
OPENCLAW_SKILLS_DST="/var/lib/openclaw/.openclaw/workspace/skills"

PICOCLAW_USER="picoclaw"
ZEROCLAW_USER="zeroclaw"
OPENCLAW_USER="openclaw"

log() { echo "[workspace-sync] $*"; }
die() { echo "[workspace-sync] ERROR: $*" >&2; exit 1; }

# ── Argument parsing ───────────────────────────────────────────────────────────────
SYNC_CONFIG_JSON=no
SYNC_CONFIG_TOML=no
SYNC_SECURITY_YML=no

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -config)
                [[ $# -ge 2 ]] || die "-config requires an argument (all|config.json|config.toml|security.yml or comma-separated)"
                IFS=',' read -ra _targets <<< "$2"
                for _t in "${_targets[@]}"; do
                    case "$_t" in
                        all)           SYNC_CONFIG_JSON=yes; SYNC_CONFIG_TOML=yes; SYNC_SECURITY_YML=yes ;;
                        config.json)   SYNC_CONFIG_JSON=yes ;;
                        config.toml)   SYNC_CONFIG_TOML=yes ;;
                        security.yml)  SYNC_SECURITY_YML=yes ;;
                        *) die "Unknown -config target: '$_t' (use all|config.json|config.toml|security.yml)" ;;
                    esac
                done
                shift 2
                ;;
            *) die "Unknown argument: $1" ;;
        esac
    done
}

main() {
parse_args "$@"

# ── Require git ──────────────────────────────────────────────────────────────
command -v git  >/dev/null 2>&1 || die "git is not installed"
command -v rsync >/dev/null 2>&1 || die "rsync is not installed"

# ── Sparse-clone into a temp directory ──────────────────────────────────────
# Use /var/tmp (on the main filesystem) instead of /tmp (tmpfs, often small)
WORK_DIR="$(mktemp -d -p /var/tmp)"
trap 'rm -rf "$WORK_DIR"' EXIT

log "Cloning $REPO_URL (sparse, depth=1) into $WORK_DIR ..."
git -C "$WORK_DIR" init -q
git -C "$WORK_DIR" remote add origin "$REPO_URL"
git -C "$WORK_DIR" config core.sparseCheckout true

# Declare only the two workspace subtrees we care about
mkdir -p "$WORK_DIR/.git/info"
cat > "$WORK_DIR/.git/info/sparse-checkout" <<EOF
$PICOCLAW_SRC/
$ZEROCLAW_SRC/
daemon/
locales/
lib/
config/
wifi-connect/
skills/
clawproxy/
nginx/nginx.openclaw
dashboard.py
clawberry_bluetooth.py
clawberry_display.py
clawberry_display_radxa.py
clawberry_paircode.py
clawberry_radxa_patch.py
publish_services.sh
scripts/clawberry-workspace-sync.sh
EOF

# GIT_TERMINAL_PROMPT=0 prevents git from hanging asking for credentials.
# If the primary (gitee) fails, fall back to github automatically.
if ! GIT_TERMINAL_PROMPT=0 git -C "$WORK_DIR" fetch --depth=1 origin HEAD 2>/dev/null; then
    log "Primary mirror ($REPO_URL) failed, falling back to $REPO_URL_FALLBACK ..."
    git -C "$WORK_DIR" remote set-url origin "$REPO_URL_FALLBACK"
    GIT_TERMINAL_PROMPT=0 git -C "$WORK_DIR" fetch --depth=1 origin HEAD \
        || die "Both mirrors failed. Check network connectivity."
fi
git -C "$WORK_DIR" checkout -q FETCH_HEAD
log "Checkout complete."

# ── Self-update: copy latest script from repo to /usr/local/bin and ensure executable ──
REPO_SCRIPT_PATH="$WORK_DIR/scripts/clawberry-workspace-sync.sh"
SCRIPT_PATH="/usr/local/bin/clawberry-workspace-sync.sh"
if [[ -f "$REPO_SCRIPT_PATH" ]]; then
    _repo_hash=$(sha256sum "$REPO_SCRIPT_PATH" | cut -d' ' -f1)
    _cur_hash=$(sha256sum "$SCRIPT_PATH" 2>/dev/null | cut -d' ' -f1 || echo "none")
    if [[ "$_repo_hash" != "$_cur_hash" ]]; then
        log "Sync script has been updated in the repo — installing new version to $SCRIPT_PATH ..."
        if cp "$REPO_SCRIPT_PATH" "$SCRIPT_PATH" 2>/dev/null; then
            chmod +x "$SCRIPT_PATH" 2>/dev/null || true
            log "✅ Sync script updated at $SCRIPT_PATH."
            log ""
            log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            log "  The sync script was updated. Please run it again to apply"
            log "  all remaining changes with the new version:"
            log "    sudo bash $SCRIPT_PATH $*"
            log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            exit 0
        else
            log "WARNING: failed to copy $REPO_SCRIPT_PATH to $SCRIPT_PATH (permission?)"
        fi
    else
        log "Sync script is already up to date — continuing."
    fi
else
    log "WARNING: sync script not found in repo at $REPO_SCRIPT_PATH — skipping self-update"
fi

# ── Bootstrap clawboard group (idempotent) ──────────────────────────────────
groupadd --system clawboard 2>/dev/null || true
for _u in zero zeroclaw picoclaw openclaw; do
    id -u "$_u" >/dev/null 2>&1 && usermod -aG clawboard "$_u" 2>/dev/null || true
done
mkdir -p /opt/clawproxy /opt/picoclaw /opt/zeroclaw
chown zero:zero /opt/clawproxy 2>/dev/null || true
if [ -d /opt/clawboard/venv ]; then
    chgrp -R clawboard /opt/clawboard/venv 2>/dev/null || true
    find /opt/clawboard/venv -type d -exec chmod g+rwx {} + 2>/dev/null || true
    find /opt/clawboard/venv -type f ! -path "*/bin/*" -exec chmod g+rw {} + 2>/dev/null || true
    find /opt/clawboard/venv/bin -type f -exec chmod g+rwx {} + 2>/dev/null || true
fi
log "clawboard group bootstrapped."

# Record service states and stop services to allow binary replacement
PIC_ACTIVE=no
ZC_ACTIVE=no
PICWEB_ACTIVE=no
PROXY_ACTIVE=no
DASHBOARD_CHANGED=no
NGINX_CHANGED=no
if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet picoclaw;          then PIC_ACTIVE=yes;    fi
    if systemctl is-active --quiet zeroclaw;          then ZC_ACTIVE=yes;     fi
    if systemctl is-active --quiet picoclaw-web;      then PICWEB_ACTIVE=yes; fi
    if systemctl is-active --quiet clawberry-proxy;   then PROXY_ACTIVE=yes;  fi
    if [[ "$PIC_ACTIVE" == "yes" || "$ZC_ACTIVE" == "yes" || "$PICWEB_ACTIVE" == "yes" || "$PROXY_ACTIVE" == "yes" ]]; then
        log "Stopping services before file operations: picoclaw=$PIC_ACTIVE zeroclaw=$ZC_ACTIVE picoclaw-web=$PICWEB_ACTIVE clawberry-proxy=$PROXY_ACTIVE"
        systemctl stop clawberry-proxy 2>/dev/null || true
        systemctl stop picoclaw-web    2>/dev/null || true
        systemctl stop picoclaw        2>/dev/null || true
        systemctl stop zeroclaw        2>/dev/null || true
    fi
fi

# ── Deploy prebuilt binaries (if present in repo)
if [[ -f "$WORK_DIR/picoclaw/picoclaw-linux-arm64" ]]; then
    log "Installing picoclaw binary to /opt/picoclaw/picoclaw"
    mkdir -p /opt/picoclaw
    if cp "$WORK_DIR/picoclaw/picoclaw-linux-arm64" /opt/picoclaw/picoclaw 2>/dev/null; then
        chmod +x /opt/picoclaw/picoclaw || true
        log "picoclaw installed to /opt/picoclaw/picoclaw"
    else
        log "WARNING: failed to copy picoclaw binary (permission?)"
    fi
fi

if [[ -f "$WORK_DIR/picoclaw/picoclaw-launcher-linux-arm64" ]]; then
    log "Installing picoclaw-web binary to /opt/picoclaw/picoclaw-launcher"
    mkdir -p /opt/picoclaw
    if cp "$WORK_DIR/picoclaw/picoclaw-launcher-linux-arm64" /opt/picoclaw/picoclaw-launcher 2>/dev/null; then
        chmod +x /opt/picoclaw/picoclaw-launcher || true
        ln -sf /opt/picoclaw/picoclaw-launcher /usr/local/bin/picoclaw-launcher 2>/dev/null || true
        log "picoclaw-web installed to /opt/picoclaw/picoclaw-launcher"
    else
        log "WARNING: failed to copy picoclaw-web binary (permission?)"
    fi
fi

if [[ -f "$WORK_DIR/zeroclaw/zeroclaw" ]]; then
    log "Installing zeroclaw binary to /opt/zeroclaw/zeroclaw"
    mkdir -p /opt/zeroclaw
    if cp "$WORK_DIR/zeroclaw/zeroclaw" /opt/zeroclaw/zeroclaw 2>&1; then
        chmod +x /opt/zeroclaw/zeroclaw || true
        log "zeroclaw installed to /opt/zeroclaw/zeroclaw"
        
        # Run config migration as zeroclaw user
        log "Running zeroclaw config migrate as zeroclaw user..."
        if sudo -u zeroclaw /opt/zeroclaw/zeroclaw config migrate 2>&1; then
            log "zeroclaw config migration completed successfully"
        else
            log "WARNING: zeroclaw config migration failed — check logs"
        fi
    else
        log "WARNING: failed to copy zeroclaw binary — check /opt/zeroclaw permissions"
    fi
fi

if [[ -f "$WORK_DIR/clawproxy/clawproxy-arm64" ]]; then
    log "Installing clawproxy binary to /usr/local/bin/clawproxy"
    mkdir -p /opt/clawproxy
    chown zero:zero /opt/clawproxy 2>/dev/null || true
    if cp "$WORK_DIR/clawproxy/clawproxy-arm64" /usr/local/bin/clawproxy 2>/dev/null; then
        chmod +x /usr/local/bin/clawproxy || true
        log "clawproxy installed to /usr/local/bin/clawproxy"
    else
        log "WARNING: failed to copy clawproxy binary (permission?)"
    fi
fi

# ── Deploy daemon service files ─────────────────────────────────────────────
SVC_CHANGED=no
if [[ -d "$WORK_DIR/daemon" ]]; then
    log "Installing systemd service files from daemon/ ..."
    for svc_file in "$WORK_DIR/daemon/"*.service; do
        [[ -f "$svc_file" ]] || continue
        svc_name="$(basename "$svc_file")"
        dst_svc="/etc/systemd/system/$svc_name"
        if cp "$svc_file" "$dst_svc" 2>/dev/null; then
            chmod 644 "$dst_svc" || true
            log "Installed $svc_name → $dst_svc"
            SVC_CHANGED=yes
        else
            log "WARNING: failed to install $svc_name (permission?)"
        fi
    done
else
    log "WARNING: daemon/ not found in repo — service files not updated"
fi

# ── Deploy picoclaw-web environment file ─────────────────────────────────────
PICWEB_ENV_SRC="$WORK_DIR/daemon/picoclaw-web.env"
PICWEB_ENV_DST="/etc/clawberry/picoclaw-web.env"
if [[ -f "$PICWEB_ENV_SRC" ]]; then
    log "Installing picoclaw-web.env to $PICWEB_ENV_DST"
    mkdir -p "$(dirname "$PICWEB_ENV_DST")"
    if cp "$PICWEB_ENV_SRC" "$PICWEB_ENV_DST" 2>/dev/null; then
        chmod 640 "$PICWEB_ENV_DST" || true
        log "picoclaw-web.env installed to $PICWEB_ENV_DST"
    else
        log "WARNING: failed to install picoclaw-web.env (permission?)"
    fi
else
    log "WARNING: daemon/picoclaw-web.env not found in repo — env file not updated"
fi

# Reload systemd if any unit files changed
if [[ "$SVC_CHANGED" == "yes" ]] && command -v systemctl >/dev/null 2>&1; then
    log "Running systemctl daemon-reload ..."
    systemctl daemon-reload 2>/dev/null || true

    # Auto-enable services that should always be on but are not yet enabled.
    # Add to this list whenever a new always-on service is introduced.
    for _auto_enable in adb-server.service; do
        if [[ -f "/etc/systemd/system/$_auto_enable" ]]; then
            if ! systemctl is-enabled --quiet "$_auto_enable" 2>/dev/null; then
                log "Enabling $_auto_enable (first install) ..."
                systemctl enable "$_auto_enable" 2>/dev/null || \
                    log "WARNING: failed to enable $_auto_enable"
            fi
            if ! systemctl is-active --quiet "$_auto_enable" 2>/dev/null; then
                log "Starting $_auto_enable ..."
                systemctl start "$_auto_enable" 2>/dev/null || \
                    log "WARNING: failed to start $_auto_enable"
            fi
        fi
    done
fi

# ── Deploy ClawBoard dashboard to /opt/clawboard ─────────────────────────────
log "Deploying ClawBoard dashboard to $CLAWBOARD_DST ..."
mkdir -p "$CLAWBOARD_DST/config" "$CLAWBOARD_DST/locales" "$CLAWBOARD_DST/lib"

# dashboard.py and clawberry_*.py helper modules
for f in "$WORK_DIR/dashboard.py" "$WORK_DIR"/clawberry_*.py "$WORK_DIR/publish_services.sh"; do
    [[ -f "$f" ]] || continue
    fname="$(basename "$f")"
    _pre_hash="none"
    [[ "$fname" == "dashboard.py" ]] && _pre_hash=$(sha256sum "$CLAWBOARD_DST/$fname" 2>/dev/null | cut -d' ' -f1 || echo "none")
    if cp "$f" "$CLAWBOARD_DST/$fname" 2>/dev/null; then
        chmod 755 "$CLAWBOARD_DST/$fname" || true
        log "  installed $fname"
        if [[ "$fname" == "dashboard.py" ]]; then
            _post_hash=$(sha256sum "$CLAWBOARD_DST/$fname" | cut -d' ' -f1)
            [[ "$_pre_hash" != "$_post_hash" ]] && DASHBOARD_CHANGED=yes
        fi
    else
        log "WARNING: failed to install $fname to $CLAWBOARD_DST"
    fi
done

# locales/ directory (locale string modules)
if [[ -d "$WORK_DIR/locales" ]]; then
    rsync --archive --update "$WORK_DIR/locales/" "$CLAWBOARD_DST/locales/"
    log "  synced locales/"
fi

# lib/ directory
if [[ -d "$WORK_DIR/lib" ]]; then
    rsync --archive --update "$WORK_DIR/lib/" "$CLAWBOARD_DST/lib/"
    log "  synced lib/"
fi

# config/ — sync non-sensitive files always; the three protected config files
# are only copied when explicitly requested via -config.
if [[ -d "$WORK_DIR/config" ]]; then
    # Non-protected files (examples, templates, etc.) — always sync
    rsync --archive --update \
        --exclude='config.json' \
        --exclude='.security.yml' \
        --exclude='config.toml' \
        "$WORK_DIR/config/" "$CLAWBOARD_DST/config/"
    log "  synced config/ (non-protected files)"

    # Protected files — only when -config flag was given
    if [[ "$SYNC_CONFIG_JSON" == "yes" ]]; then
        if [[ -f "$WORK_DIR/config/config.json" ]]; then
            cp "$WORK_DIR/config/config.json" "$CLAWBOARD_DST/config/config.json" && \
                log "  config.json: synced from repo" || \
                log "WARNING: failed to copy config.json"
        else
            log "  config.json: not present in repo — skipping"
        fi
    else
        log "  config.json: skipped (use -config config.json or -config all to sync)"
    fi

    if [[ "$SYNC_CONFIG_TOML" == "yes" ]]; then
        if [[ -f "$WORK_DIR/config/config.toml" ]]; then
            cp "$WORK_DIR/config/config.toml" "$CLAWBOARD_DST/config/config.toml" && \
                log "  config.toml: synced from repo" || \
                log "WARNING: failed to copy config.toml"
        else
            log "  config.toml: not present in repo — skipping"
        fi
    else
        log "  config.toml: skipped (use -config config.toml or -config all to sync)"
    fi

    if [[ "$SYNC_SECURITY_YML" == "yes" ]]; then
        if [[ -f "$WORK_DIR/config/.security.yml" ]]; then
            cp "$WORK_DIR/config/.security.yml" "$CLAWBOARD_DST/config/.security.yml" && \
                log "  .security.yml: synced from repo" || \
                log "WARNING: failed to copy .security.yml"
        else
            log "  .security.yml: not present in repo — skipping"
        fi
    else
        log "  .security.yml: skipped (use -config security.yml or -config all to sync)"
    fi
fi


# ── Deploy wifi-connect helper script ────────────────────────────────────────
WIFI_LAUNCH_SRC="$WORK_DIR/wifi-connect/wifi-connect-gpio-launch.sh"
WIFI_LAUNCH_DST="/opt/wifi-connect/wifi-connect-gpio-launch.sh"
if [[ -f "$WIFI_LAUNCH_SRC" ]]; then
    mkdir -p /opt/wifi-connect
    if cp "$WIFI_LAUNCH_SRC" "$WIFI_LAUNCH_DST" 2>/dev/null; then
        chmod 755 "$WIFI_LAUNCH_DST" || true
        log "  installed wifi-connect-gpio-launch.sh → $WIFI_LAUNCH_DST"
    else
        log "WARNING: failed to install wifi-connect-gpio-launch.sh (permission?)"
    fi
else
    log "WARNING: wifi-connect/wifi-connect-gpio-launch.sh not found in repo — skipping"
fi

# sudoers drop-in
SUDOERS_SRC="$WORK_DIR/daemon/sudoers.d-clawboard"
if [[ -f "$SUDOERS_SRC" ]]; then
    if cp "$SUDOERS_SRC" /etc/sudoers.d/clawboard 2>/dev/null; then
        chmod 440 /etc/sudoers.d/clawboard || true
        log "  installed /etc/sudoers.d/clawboard"
    else
        log "WARNING: failed to install sudoers.d/clawboard (run as root?)"
    fi
fi

# ── Deploy nginx server config ───────────────────────────────────────────────
# Debian nginx pattern: install to sites-available, enable via symlink in
# sites-enabled (the main nginx.conf includes sites-enabled/*).
NGINX_SRC="$WORK_DIR/nginx/nginx.openclaw"
NGINX_AVAIL="/etc/nginx/sites-available/openclaw"
NGINX_ENABLED="/etc/nginx/sites-enabled/openclaw"
if [[ -f "$NGINX_SRC" ]]; then
    _nginx_pre=$(sha256sum "$NGINX_AVAIL" 2>/dev/null | cut -d' ' -f1 || echo "none")
    if cp "$NGINX_SRC" "$NGINX_AVAIL" 2>/dev/null; then
        chmod 644 "$NGINX_AVAIL" || true
        # Ensure the sites-enabled symlink exists
        if [[ ! -L "$NGINX_ENABLED" ]]; then
            ln -sf "$NGINX_AVAIL" "$NGINX_ENABLED" 2>/dev/null && \
                log "  created symlink $NGINX_ENABLED → $NGINX_AVAIL" || \
                log "WARNING: failed to create sites-enabled symlink"
        fi
        _nginx_post=$(sha256sum "$NGINX_AVAIL" | cut -d' ' -f1)
        if [[ "$_nginx_pre" != "$_nginx_post" ]]; then
            log "  nginx.openclaw updated → $NGINX_AVAIL"
            NGINX_CHANGED=yes
        else
            log "  nginx.openclaw already up to date"
        fi
    else
        log "WARNING: failed to install nginx.openclaw to $NGINX_AVAIL (run as root?)"
    fi
else
    log "WARNING: nginx/nginx.openclaw not found in repo — skipping"
fi

# Fix ownership
chown -R "$CLAWBOARD_USER:$CLAWBOARD_USER" "$CLAWBOARD_DST" 2>/dev/null || \
    log "WARNING: chown $CLAWBOARD_USER failed for $CLAWBOARD_DST"
log "ClawBoard dashboard deploy complete."

# ── Helper: sync one workspace ───────────────────────────────────────────────
sync_workspace() {
    local src="$WORK_DIR/$1"    # local path inside the clone
    local dst="$2"             # destination on disk
    local owner="$3"           # user:group for chown

    if [[ ! -d "$src" ]]; then
        log "WARNING: source directory $src not found in repo — skipping"
        return
    fi

    log "Syncing $1  →  $dst"
    mkdir -p "$dst"

    # --update     : skip files where destination is newer (preserve local edits)
    # --recursive  : recurse into subdirs
    # --times      : preserve timestamps (needed for --update comparisons)
    # No --delete  : never remove files the agent has created locally
    rsync --archive --update --times "$src/" "$dst/"

    # Fix ownership so the service user can read/write
    chown -R "$owner:$owner" "$dst" 2>/dev/null || \
        log "WARNING: chown $owner failed for $dst (running as non-root?)"

    log "Done: $dst"
}

# ── Sync both workspaces ─────────────────────────────────────────────────────
sync_workspace "$PICOCLAW_WORKSPACE_SRC" "$PICOCLAW_WORKSPACE_DST" "$PICOCLAW_USER"
sync_workspace "$ZEROCLAW_WORKSPACE_SRC" "$ZEROCLAW_WORKSPACE_DST" "$ZEROCLAW_USER"
# ── Sync skills into agent workspaces (picoclaw, zeroclaw, openclaw) ──────────
if [[ -d "$WORK_DIR/skills" ]]; then
    for _skills_entry in \
        "$PICOCLAW_SKILLS_DST:$PICOCLAW_USER:/var/lib/picoclaw/.picoclaw/workspace" \
        "$ZEROCLAW_SKILLS_DST:$ZEROCLAW_USER:/var/lib/zeroclaw/.zeroclaw/workspace" \
        "$OPENCLAW_SKILLS_DST:$OPENCLAW_USER:/var/lib/openclaw/.openclaw/workspace"
    do
        _dst="${_skills_entry%%:*}"
        _rest="${_skills_entry#*:}"
        _owner="${_rest%%:*}"
        _ws_dir="${_rest#*:}"
        # Only deploy if the parent workspace directory exists (agent is installed)
        if [[ -d "$_ws_dir" ]]; then
            log "Syncing skills/  →  $_dst"
            mkdir -p "$_dst"
            # No --update: skills are always overwritten from the repo.
            # --update would skip files edited locally on the device (newer mtime).
            rsync --archive --checksum "$WORK_DIR/skills/" "$_dst/"
            chown -R "$_owner:$_owner" "$_dst" 2>/dev/null || \
                log "WARNING: chown $_owner failed for $_dst (running as non-root?)"
            log "Done: $_dst"
        else
            log "Skipping skills for $_owner (workspace $_ws_dir not present)"
        fi
    done
else
    log "WARNING: skills/ not found in repo — skipping"
fi
# Ensure /var/lib/picoclaw/.picoclaw exists and is owned by picoclaw
if [[ ! -d "/var/lib/picoclaw/.picoclaw" ]]; then
    log "Creating /var/lib/picoclaw/.picoclaw"
    mkdir -p /var/lib/picoclaw/.picoclaw
    chown -R picoclaw:picoclaw /var/lib/picoclaw/.picoclaw
else
    chown -R picoclaw:picoclaw /var/lib/picoclaw/.picoclaw
fi
# Ensure /var/lib/zeroclaw/.zeroclaw exists and is owned by zeroclaw
if [[ ! -d "/var/lib/zeroclaw/.zeroclaw" ]]; then
    log "Creating /var/lib/zeroclaw/.zeroclaw"
    mkdir -p /var/lib/zeroclaw/.zeroclaw
    chown -R zeroclaw:zeroclaw /var/lib/zeroclaw/.zeroclaw
else
    chown -R zeroclaw:zeroclaw /var/lib/zeroclaw/.zeroclaw
fi

log "Workspace sync complete."

# Restart services that were running before the sync
if command -v systemctl >/dev/null 2>&1; then
    if [[ "$ZC_ACTIVE" == "yes" ]]; then
        log "Restarting zeroclaw"
        systemctl restart zeroclaw 2>/dev/null || true
    fi
    if [[ "$PIC_ACTIVE" == "yes" ]]; then
        log "Restarting picoclaw"
        systemctl restart picoclaw 2>/dev/null || true
    fi
    if [[ "$PICWEB_ACTIVE" == "yes" ]]; then
        log "Restarting picoclaw-web"
        systemctl restart picoclaw-web 2>/dev/null || true
    fi
    if [[ "$PROXY_ACTIVE" == "yes" ]]; then
        log "Restarting clawberry-proxy"
        systemctl restart clawberry-proxy 2>/dev/null || true
    fi
    if [[ "$DASHBOARD_CHANGED" == "yes" ]]; then
        log "dashboard.py was updated — restarting clawboard.service"
        systemctl restart clawboard 2>/dev/null || true
    fi
    if [[ "$NGINX_CHANGED" == "yes" ]]; then
        log "nginx.openclaw was updated — testing and reloading nginx"
        if nginx -t 2>/dev/null; then
            systemctl reload nginx 2>/dev/null || true
            log "  nginx reloaded"
        else
            log "WARNING: nginx config test failed — NOT reloading nginx"
        fi
    fi
fi

} # end main()

main "$@"
