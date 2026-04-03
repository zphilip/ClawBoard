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

REPO_URL="https://gitee.com/tiandazhu/ClawBoard.git"

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

PICOCLAW_USER="picoclaw"
ZEROCLAW_USER="zeroclaw"

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
                [[ $# -ge 2 ]] || die "-config requires an argument (all|config.json|config.toml|security.yml)"
                case "$2" in
                    all)           SYNC_CONFIG_JSON=yes; SYNC_CONFIG_TOML=yes; SYNC_SECURITY_YML=yes ;;
                    config.json)   SYNC_CONFIG_JSON=yes ;;
                    config.toml)   SYNC_CONFIG_TOML=yes ;;
                    security.yml)  SYNC_SECURITY_YML=yes ;;
                    *) die "Unknown -config target: $2 (use all|config.json|config.toml|security.yml)" ;;
                esac
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
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

log "Cloning $REPO_URL (sparse, depth=1) into $TMPDIR ..."
git -C "$TMPDIR" init -q
git -C "$TMPDIR" remote add origin "$REPO_URL"
git -C "$TMPDIR" config core.sparseCheckout true

# Declare only the two workspace subtrees we care about
mkdir -p "$TMPDIR/.git/info"
cat > "$TMPDIR/.git/info/sparse-checkout" <<EOF
$PICOCLAW_SRC/
$ZEROCLAW_SRC/
daemon/
locales/
lib/
config/
dashboard.py
clawberry_bluetooth.py
clawberry_display.py
clawberry_paircode.py
publish_services.sh
scripts/clawberry-workspace-sync.sh
EOF

git -C "$TMPDIR" fetch --depth=1 origin HEAD
git -C "$TMPDIR" checkout -q FETCH_HEAD
log "Checkout complete."

# ── Self-update: copy latest script from repo to /usr/local/bin and ensure executable ──
REPO_SCRIPT_PATH="$TMPDIR/scripts/clawberry-workspace-sync.sh"
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

# Record service states and stop services to allow binary replacement
PIC_ACTIVE=no
ZC_ACTIVE=no
PICWEB_ACTIVE=no
if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet picoclaw;     then PIC_ACTIVE=yes;    fi
    if systemctl is-active --quiet zeroclaw;     then ZC_ACTIVE=yes;     fi
    if systemctl is-active --quiet picoclaw-web; then PICWEB_ACTIVE=yes; fi
    if [[ "$PIC_ACTIVE" == "yes" || "$ZC_ACTIVE" == "yes" || "$PICWEB_ACTIVE" == "yes" ]]; then
        log "Stopping services before file operations: picoclaw=$PIC_ACTIVE zeroclaw=$ZC_ACTIVE picoclaw-web=$PICWEB_ACTIVE"
        systemctl stop picoclaw-web 2>/dev/null || true
        systemctl stop picoclaw     2>/dev/null || true
        systemctl stop zeroclaw     2>/dev/null || true
    fi
fi

# ── Deploy prebuilt binaries (if present in repo)
if [[ -f "$TMPDIR/picoclaw/picoclaw-linux-arm64" ]]; then
    log "Installing picoclaw binary to /opt/picoclaw/picoclaw"
    mkdir -p /opt/picoclaw
    if cp "$TMPDIR/picoclaw/picoclaw-linux-arm64" /opt/picoclaw/picoclaw 2>/dev/null; then
        chmod +x /opt/picoclaw/picoclaw || true
        log "picoclaw installed to /opt/picoclaw/picoclaw"
    else
        log "WARNING: failed to copy picoclaw binary (permission?)"
    fi
fi

if [[ -f "$TMPDIR/picoclaw/picoclaw-launcher-linux-arm64" ]]; then
    log "Installing picoclaw-web binary to /opt/picoclaw/picoclaw-launcher"
    mkdir -p /opt/picoclaw
    if cp "$TMPDIR/picoclaw/picoclaw-launcher-linux-arm64" /opt/picoclaw/picoclaw-launcher 2>/dev/null; then
        chmod +x /opt/picoclaw/picoclaw-launcher || true
        ln -sf /opt/picoclaw/picoclaw-launcher /usr/local/bin/picoclaw-launcher 2>/dev/null || true
        log "picoclaw-web installed to /opt/picoclaw/picoclaw-launcher"
    else
        log "WARNING: failed to copy picoclaw-web binary (permission?)"
    fi
fi

if [[ -f "$TMPDIR/zeroclaw/zeroclaw" ]]; then
    log "Installing zeroclaw binary to /opt/zeroclaw/zeroclaw"
    mkdir -p /opt/zeroclaw
    if cp "$TMPDIR/zeroclaw/zeroclaw" /opt/zeroclaw/zeroclaw 2>/dev/null; then
        chmod +x /opt/zeroclaw/zeroclaw || true
        log "zeroclaw installed to /opt/zeroclaw/zeroclaw"
    else
        log "WARNING: failed to copy zeroclaw binary (permission?)"
    fi
fi

# ── Deploy daemon service files ─────────────────────────────────────────────
SVC_CHANGED=no
if [[ -d "$TMPDIR/daemon" ]]; then
    log "Installing systemd service files from daemon/ ..."
    for svc_file in "$TMPDIR/daemon/"*.service; do
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
PICWEB_ENV_SRC="$TMPDIR/daemon/picoclaw-web.env"
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
fi

# ── Deploy ClawBoard dashboard to /opt/clawboard ─────────────────────────────
log "Deploying ClawBoard dashboard to $CLAWBOARD_DST ..."
mkdir -p "$CLAWBOARD_DST/config" "$CLAWBOARD_DST/locales" "$CLAWBOARD_DST/lib"

# dashboard.py and clawberry_*.py helper modules
for f in "$TMPDIR/dashboard.py" "$TMPDIR"/clawberry_*.py "$TMPDIR/publish_services.sh"; do
    [[ -f "$f" ]] || continue
    fname="$(basename "$f")"
    if cp "$f" "$CLAWBOARD_DST/$fname" 2>/dev/null; then
        chmod 755 "$CLAWBOARD_DST/$fname" || true
        log "  installed $fname"
    else
        log "WARNING: failed to install $fname to $CLAWBOARD_DST"
    fi
done

# locales/ directory (locale string modules)
if [[ -d "$TMPDIR/locales" ]]; then
    rsync --archive --update "$TMPDIR/locales/" "$CLAWBOARD_DST/locales/"
    log "  synced locales/"
fi

# lib/ directory
if [[ -d "$TMPDIR/lib" ]]; then
    rsync --archive --update "$TMPDIR/lib/" "$CLAWBOARD_DST/lib/"
    log "  synced lib/"
fi

# config/ — sync non-sensitive files always; the three protected config files
# are only copied when explicitly requested via -config.
if [[ -d "$TMPDIR/config" ]]; then
    # Non-protected files (examples, templates, etc.) — always sync
    rsync --archive --update \
        --exclude='config.json' \
        --exclude='.security.yml' \
        --exclude='config.toml' \
        "$TMPDIR/config/" "$CLAWBOARD_DST/config/"
    log "  synced config/ (non-protected files)"

    # Protected files — only when -config flag was given
    if [[ "$SYNC_CONFIG_JSON" == "yes" ]]; then
        if [[ -f "$TMPDIR/config/config.json" ]]; then
            cp "$TMPDIR/config/config.json" "$CLAWBOARD_DST/config/config.json" && \
                log "  config.json: synced from repo" || \
                log "WARNING: failed to copy config.json"
        else
            log "  config.json: not present in repo — skipping"
        fi
    else
        log "  config.json: skipped (use -config config.json or -config all to sync)"
    fi

    if [[ "$SYNC_CONFIG_TOML" == "yes" ]]; then
        if [[ -f "$TMPDIR/config/config.toml" ]]; then
            cp "$TMPDIR/config/config.toml" "$CLAWBOARD_DST/config/config.toml" && \
                log "  config.toml: synced from repo" || \
                log "WARNING: failed to copy config.toml"
        else
            log "  config.toml: not present in repo — skipping"
        fi
    else
        log "  config.toml: skipped (use -config config.toml or -config all to sync)"
    fi

    if [[ "$SYNC_SECURITY_YML" == "yes" ]]; then
        if [[ -f "$TMPDIR/config/.security.yml" ]]; then
            cp "$TMPDIR/config/.security.yml" "$CLAWBOARD_DST/config/.security.yml" && \
                log "  .security.yml: synced from repo" || \
                log "WARNING: failed to copy .security.yml"
        else
            log "  .security.yml: not present in repo — skipping"
        fi
    else
        log "  .security.yml: skipped (use -config security.yml or -config all to sync)"
    fi
fi


# sudoers drop-in
SUDOERS_SRC="$TMPDIR/daemon/sudoers.d-clawboard"
if [[ -f "$SUDOERS_SRC" ]]; then
    if cp "$SUDOERS_SRC" /etc/sudoers.d/clawboard 2>/dev/null; then
        chmod 440 /etc/sudoers.d/clawboard || true
        log "  installed /etc/sudoers.d/clawboard"
    else
        log "WARNING: failed to install sudoers.d/clawboard (run as root?)"
    fi
fi

# Fix ownership
chown -R "$CLAWBOARD_USER:$CLAWBOARD_USER" "$CLAWBOARD_DST" 2>/dev/null || \
    log "WARNING: chown $CLAWBOARD_USER failed for $CLAWBOARD_DST"
log "ClawBoard dashboard deploy complete."

# ── Helper: sync one workspace ───────────────────────────────────────────────
sync_workspace() {
    local src="$TMPDIR/$1"    # local path inside the clone
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
fi

} # end main()

main "$@"
