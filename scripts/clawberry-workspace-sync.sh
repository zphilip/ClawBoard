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
# Strategy:
#   • Clone repo into a temp dir (sparse, no history, saves bandwidth)
#   • rsync each workspace subtree into the target, preserving any local
#     files the service may have added (--update: skip if dest is newer)
#   • Never delete files the service has created locally
#   • Set correct ownership after copy

set -euo pipefail

REPO_URL="https://github.com/zphilip/ClawBoard.git"

PICOCLAW_WORKSPACE_SRC="picoclaw/workspace"
ZEROCLAW_WORKSPACE_SRC="zeroclaw/workspace"

PICOCLAW_WORKSPACE_DST="/var/lib/picoclaw/.picoclaw/workspace"
ZEROCLAW_WORKSPACE_DST="/var/lib/zeroclaw/.zeroclaw/workspace"

PICOCLAW_USER="picoclaw"
ZEROCLAW_USER="zeroclaw"

log() { echo "[workspace-sync] $*"; }
die() { echo "[workspace-sync] ERROR: $*" >&2; exit 1; }

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
$PICOCLAW_WORKSPACE_SRC/
$ZEROCLAW_WORKSPACE_SRC/
EOF

git -C "$TMPDIR" fetch --depth=1 origin HEAD
git -C "$TMPDIR" checkout -q FETCH_HEAD
log "Checkout complete."

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

log "Workspace sync complete."
