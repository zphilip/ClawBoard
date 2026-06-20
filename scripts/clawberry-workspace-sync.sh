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

REPO_URL="https://gh-proxy.org/https://github.com/zphilip/ClawBoard.git"
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
command -v git    >/dev/null 2>&1 || die "git is not installed"
command -v rsync  >/dev/null 2>&1 || die "rsync is not installed"
command -v unzip  >/dev/null 2>&1 || die "unzip is not installed — run: apt install unzip"
command -v timeout >/dev/null 2>&1 || die "timeout is not installed (coreutils missing)"

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
scripts/upgrade_picoclaw_config.py
characters/
EOF

# GIT_TERMINAL_PROMPT=0 prevents git from hanging asking for credentials.
# Fallback chain: primary mirror → github → zip download (with retries)
GIT_OK=no
# Slow connections (30 KB/s on 20 MB = ~11 min) need generous timeouts.
# 1200 s = 20 min covers even very slow links.
GIT_TIMEOUT=1200
if GIT_TERMINAL_PROMPT=0 timeout "$GIT_TIMEOUT" git -C "$WORK_DIR" fetch --depth=1 origin HEAD 2>&1; then
    GIT_OK=yes
else
    log "Primary mirror ($REPO_URL) failed, falling back to $REPO_URL_FALLBACK ..."
    git -C "$WORK_DIR" remote set-url origin "$REPO_URL_FALLBACK"
    if GIT_TERMINAL_PROMPT=0 timeout "$GIT_TIMEOUT" git -C "$WORK_DIR" fetch --depth=1 origin HEAD 2>&1; then
        GIT_OK=yes
    else
        # ── Zip fallback: download archive from GitHub with retries ──────────
        ZIP_URL="https://github.com/zphilip/ClawBoard/archive/refs/heads/main.zip"
        ZIP_FILE="${WORK_DIR}.zip"
        ZIP_STAGING="${WORK_DIR}.staging"
        ZIP_OK=no

        for _attempt in 1 2 3 4 5; do
            log "Zip download attempt $_attempt/5 ..."
            rm -f "$ZIP_FILE"

            if command -v wget >/dev/null 2>&1; then
                # No read-timeout — on slow connections the server may pause
                # >30 s between chunks.  Outer loop + unzip -t handles retries.
                wget --tries=1 --timeout=0 \
                     -O "$ZIP_FILE" "$ZIP_URL" 2>&1 || true
            elif command -v curl >/dev/null 2>&1; then
                curl -fSL --connect-timeout 30 --max-time 1800 \
                     --retry 0 \
                     -o "$ZIP_FILE" "$ZIP_URL" 2>&1 || true
            else
                die "No downloader available (wget or curl required) and both git mirrors failed."
            fi

            if [[ -f "$ZIP_FILE" ]] && unzip -tq "$ZIP_FILE" 2>/dev/null; then
                ZIP_OK=yes
                break
            fi

            # Corrupt or missing — report size and retry
            _size=0
            [[ -f "$ZIP_FILE" ]] && _size=$(stat -c%s "$ZIP_FILE" 2>/dev/null || echo 0)
            log "  Zip invalid (${_size} bytes) — retrying in 5s ..."
            rm -f "$ZIP_FILE"
            sleep 5
        done

        if [[ "$ZIP_OK" != "yes" ]]; then
            die "All methods (primary git, fallback git, zip download ×3) failed. Check network connectivity."
        fi

        # ZIP_OK already validated the archive — just extract it
        mkdir -p "$ZIP_STAGING"
        if unzip -qo "$ZIP_FILE" -d "$ZIP_STAGING" 2>/dev/null; then
            # GitHub wraps everything in a single dir: ClawBoard-main/
            ZIP_WRAPPER=$(ls -1 "$ZIP_STAGING" | head -1)
            ZIP_SRC="$ZIP_STAGING"
            if [[ -n "$ZIP_WRAPPER" && -d "$ZIP_STAGING/$ZIP_WRAPPER" ]]; then
                ZIP_SRC="$ZIP_STAGING/$ZIP_WRAPPER"
            fi
            # Move repo contents into WORK_DIR (skip .git so sparse-checkout
            # doesn't apply — the zip gives us everything anyway)
            shopt -s dotglob
            for _item in "$ZIP_SRC"/*; do
                [[ -e "$_item" ]] || continue
                [[ "$(basename "$_item")" == ".git" ]] && continue
                mv "$_item" "$WORK_DIR/" 2>/dev/null || true
            done
            shopt -u dotglob
            log "Zip extracted successfully — proceeding without git."
        else
            die "Zip downloaded and validated but extraction failed."
        fi
        rm -rf "$ZIP_FILE" "$ZIP_STAGING" 2>/dev/null || true
    fi
fi

if [[ "$GIT_OK" == "yes" ]]; then
    git -C "$WORK_DIR" checkout -q FETCH_HEAD
    log "Checkout complete."
fi

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
    _zc_dst="/opt/zeroclaw/zeroclaw"
    _zc_tmp="/opt/zeroclaw/zeroclaw.new"

    # Ensure zeroclaw is fully stopped before replacing binary.
    if command -v systemctl >/dev/null 2>&1; then
        systemctl stop zeroclaw 2>/dev/null || true
    fi
    if pgrep -x zeroclaw >/dev/null 2>&1 || pgrep -f "/opt/zeroclaw/zeroclaw" >/dev/null 2>&1; then
        log "zeroclaw process still running, killing it..."
        pkill -9 -x zeroclaw 2>/dev/null || true
        pkill -9 -f "/opt/zeroclaw/zeroclaw" 2>/dev/null || true
    fi

    # Write to a temp file first, then atomically swap into place.
    if cp "$WORK_DIR/zeroclaw/zeroclaw" "$_zc_tmp" 2>&1 && mv -f "$_zc_tmp" "$_zc_dst" 2>&1; then
        chmod +x "$_zc_dst" || true
        log "zeroclaw installed to /opt/zeroclaw/zeroclaw"
        
        # Run config migration as zeroclaw user
        log "Running zeroclaw config migrate as zeroclaw user..."
        if id -u zeroclaw >/dev/null 2>&1 && sudo -u zeroclaw /opt/zeroclaw/zeroclaw config migrate 2>&1; then
            log "zeroclaw config migration completed successfully"
        else
            log "WARNING: zeroclaw config migration failed or zeroclaw user missing — check logs"
        fi
    else
        rm -f "$_zc_tmp" 2>/dev/null || true
        log "WARNING: failed to install zeroclaw binary — check process locks and /opt/zeroclaw permissions"
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

# ── Deploy clawproxy config (first-time seed from example) ───────────────────
if [[ -f "$WORK_DIR/clawproxy/config.toml.example" ]]; then
    # Always keep the example up-to-date in /opt/clawproxy
    cp "$WORK_DIR/clawproxy/config.toml.example" "/opt/clawproxy/config.toml.example"
    chown zero:zero "/opt/clawproxy/config.toml.example" 2>/dev/null || true
    # On first install (no config.toml exists), seed from example
    if [[ ! -f "/opt/clawproxy/config.toml" ]]; then
        cp "$WORK_DIR/clawproxy/config.toml.example" "/opt/clawproxy/config.toml"
        chown zero:zero "/opt/clawproxy/config.toml" 2>/dev/null || true
        log "clawproxy config.toml seeded from config.toml.example"
    fi
    log "clawproxy config.toml.example updated"
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

# characters/ directory (agent persona files)
if [[ -d "$WORK_DIR/characters" ]]; then
    rsync --archive --update "$WORK_DIR/characters/" "$CLAWBOARD_DST/characters/"
    log "  synced characters/"
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

# ── Deploy upgrade_picoclaw_config.py helper ─────────────────────────────────
UPGRADE_SCRIPT_SRC="$WORK_DIR/scripts/upgrade_picoclaw_config.py"
UPGRADE_SCRIPT_DST="$CLAWBOARD_DST/scripts/upgrade_picoclaw_config.py"
if [[ -f "$UPGRADE_SCRIPT_SRC" ]]; then
    mkdir -p "$CLAWBOARD_DST/scripts"
    if cp "$UPGRADE_SCRIPT_SRC" "$UPGRADE_SCRIPT_DST" 2>/dev/null; then
        chmod 755 "$UPGRADE_SCRIPT_DST" || true
        log "  installed upgrade_picoclaw_config.py"
    else
        log "WARNING: failed to install upgrade_picoclaw_config.py"
    fi
fi

# ── Auto-upgrade local picoclaw config.json if repo schema version is newer ──
# The repo config/config.json carries the canonical schema version for this
# ClawBoard release.  When the local working copy is older, run the upgrade
# script to bring it up to date.  picoclaw auto-migrates its own live copy
# (/var/lib/picoclaw/.picoclaw/config.json) on startup; we only need to upgrade
# the dashboard's local copy here.
REPO_CFG="$WORK_DIR/config/config.json"
LOCAL_CFG="$CLAWBOARD_DST/config/config.json"
if [[ -f "$REPO_CFG" && -f "$LOCAL_CFG" ]] && command -v python3 >/dev/null 2>&1; then
    _repo_ver=$(python3 -c "import json; print(json.load(open('$REPO_CFG')).get('version',0))" 2>/dev/null || echo 0)
    _local_ver=$(python3 -c "import json; print(json.load(open('$LOCAL_CFG')).get('version',0))" 2>/dev/null || echo 0)
    if [[ "$_repo_ver" -gt "$_local_ver" ]]; then
        log "Config schema upgrade needed: local v$_local_ver → repo v$_repo_ver"
        if [[ -f "$UPGRADE_SCRIPT_DST" ]]; then
            log "  Running upgrade_picoclaw_config.py on $LOCAL_CFG ..."
            if python3 "$UPGRADE_SCRIPT_DST" "$LOCAL_CFG"; then
                log "✅ Config upgraded to v$_repo_ver"
            else
                log "WARNING: config upgrade failed — $LOCAL_CFG may need manual review"
            fi
        else
            log "WARNING: upgrade script not found at $UPGRADE_SCRIPT_DST — cannot auto-upgrade config"
        fi
    else
        log "  Config schema is current (local v$_local_ver, repo v$_repo_ver) — no upgrade needed"
    fi
fi


# ── Deploy wifi-connect scripts ──────────────────────────────────────────────
WIFI_CONNECT_SRC="$WORK_DIR/wifi-connect"
WIFI_CONNECT_DST="/opt/wifi-connect"
if [[ -d "$WIFI_CONNECT_SRC" ]]; then
    mkdir -p "$WIFI_CONNECT_DST"
    # Copy all scripts (shell + Python) and the board_config module.
    # The wifi-connect binary itself is large and rarely changes — skip it here
    # (deploy separately when needed).
    for _f in "$WIFI_CONNECT_SRC"/*.{sh,py}; do
        [[ -f "$_f" ]] || continue
        _fname="$(basename "$_f")"
        if cp "$_f" "$WIFI_CONNECT_DST/$_fname" 2>/dev/null; then
            chmod 755 "$WIFI_CONNECT_DST/$_fname" || true
            log "  installed $_fname → $WIFI_CONNECT_DST/$_fname"
        else
            log "WARNING: failed to install $_fname (permission?)"
        fi
    done
else
    log "WARNING: wifi-connect/ not found in repo — skipping"
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
            # --update: skip files newer on the device so user-edited API keys are preserved.
            rsync --archive --update "$WORK_DIR/skills/" "$_dst/"
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
# ── Seed mobile-control config.json from example (first-time only) ───────────
for _mc_user_dir in \
    "/var/lib/picoclaw/.picoclaw/workspace" \
    "/var/lib/zeroclaw/.zeroclaw/workspace" \
    "/var/lib/openclaw/.openclaw/workspace"
do
    _mc_example="$_mc_user_dir/skills/mobile-control/config.json.example"
    _mc_config="$_mc_user_dir/skills/mobile-control/config.json"
    if [[ -f "$_mc_example" && ! -f "$_mc_config" ]]; then
        cp "$_mc_example" "$_mc_config"
        # chown to the owner of the workspace dir
        _mc_owner=$(stat -c '%U' "$_mc_user_dir" 2>/dev/null || echo "")
        [[ -n "$_mc_owner" ]] && chown "${_mc_owner}:${_mc_owner}" "$_mc_config" 2>/dev/null || true
        log "mobile-control config.json seeded from example → $_mc_config"
    fi
done
# ── Seed MEMORY.md into picoclaw workspace/memory/ ───────────────────────────
# MEMORY.md lives at the workspace root in the repo but picoclaw expects it
# (and writes runtime updates to) ~/.picoclaw/workspace/memory/MEMORY.md.
# --update: skip if the device copy is newer so the agent's own edits survive.
PICOCLAW_MEMORY_SRC="$WORK_DIR/$PICOCLAW_WORKSPACE_SRC/MEMORY.md"
PICOCLAW_MEMORY_DST_DIR="$PICOCLAW_WORKSPACE_DST/memory"
if [[ -f "$PICOCLAW_MEMORY_SRC" ]]; then
    mkdir -p "$PICOCLAW_MEMORY_DST_DIR"
    rsync --archive --update "$PICOCLAW_MEMORY_SRC" "$PICOCLAW_MEMORY_DST_DIR/MEMORY.md"
    chown -R "$PICOCLAW_USER:$PICOCLAW_USER" "$PICOCLAW_MEMORY_DST_DIR" 2>/dev/null || \
        log "WARNING: chown $PICOCLAW_USER failed for $PICOCLAW_MEMORY_DST_DIR"
    log "Seeded MEMORY.md → $PICOCLAW_MEMORY_DST_DIR/MEMORY.md"
else
    log "WARNING: $PICOCLAW_MEMORY_SRC not found in repo — skipping memory seed"
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
