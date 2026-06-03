#!/usr/bin/env bash
set -euo pipefail

# Clean runtime artifacts for skills/mobile-control without touching source code.
# Default: remove memory and screenshot/runtime data.
# --all: also remove trace log and __pycache__.
# --dry-run: print what would be removed.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN=0
ALL_MODE=0
QUIET=0

usage() {
  cat <<'USAGE'
Usage:
  ./clean_mobile_control_data.sh [--dry-run] [--all] [--quiet]

Options:
  --dry-run   Show targets without deleting.
  --all       Also remove skill_trace.log and __pycache__.
  --quiet     Reduce non-essential output.
  -h, --help  Show this help.

What gets removed by default:
  - memory_data/ (events.jsonl, events.db, records.jsonl, etc.)
  - screenshots/ (task images, annotations, leftovers)
  - Temporary screenshot/task folders in skill dir:
    screenshot_*, task_*, *_anno

Safety:
  - The script only deletes paths inside skills/mobile-control.
  - Source code and configs are not removed.
USAGE
}

log() {
  if [[ "$QUIET" -eq 0 ]]; then
    echo "$*"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      ;;
    --all)
      ALL_MODE=1
      ;;
    --quiet)
      QUIET=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 2
      ;;
  esac
  shift
done

safe_remove() {
  local target="$1"
  if [[ -z "$target" ]]; then
    return 0
  fi

  # Resolve path; if missing, skip.
  if [[ ! -e "$target" ]]; then
    return 0
  fi

  local resolved
  resolved="$(realpath "$target")"

  # Safety guard: only allow deletion under SCRIPT_DIR.
  if [[ "$resolved" != "$SCRIPT_DIR"/* ]]; then
    echo "[SKIP][unsafe] $target -> $resolved" >&2
    return 0
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[DRY-RUN] rm -rf $resolved"
  else
    rm -rf -- "$resolved"
    echo "[REMOVED] $resolved"
  fi
}

log "[INFO] skill dir: $SCRIPT_DIR"
log "[INFO] mode: dry_run=$DRY_RUN all=$ALL_MODE"

# Core runtime data
safe_remove "$SCRIPT_DIR/memory_data"
safe_remove "$SCRIPT_DIR/screenshots"

# Legacy or stray top-level runtime folders
while IFS= read -r -d '' p; do
  safe_remove "$p"
done < <(find "$SCRIPT_DIR" -mindepth 1 -maxdepth 1 -type d \( -name 'screenshot_*' -o -name 'task_*' -o -name '*_anno' \) -print0)

# Optional extended cleanup
if [[ "$ALL_MODE" -eq 1 ]]; then
  safe_remove "$SCRIPT_DIR/skill_trace.log"
  safe_remove "$SCRIPT_DIR/__pycache__"

  while IFS= read -r -d '' pyc; do
    safe_remove "$pyc"
  done < <(find "$SCRIPT_DIR" -type d -name '__pycache__' -print0)
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[DONE] Dry run complete. No files were deleted."
else
  echo "[DONE] Cleanup complete."
fi
