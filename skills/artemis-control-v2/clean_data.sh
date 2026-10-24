#!/bin/bash
# clean_artemis_control_data.sh — Clean up screenshots and temporary data
#
# Usage:
#   ./clean_artemis_control_data.sh          # Clean screenshots older than 1 day
#   ./clean_artemis_control_data.sh --all    # Clean ALL data including logs

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCREENSHOTS_DIR="${SCRIPT_DIR}/screenshots"
TRACE_LOG="${SCRIPT_DIR}/skill_trace.log"
MAX_AGE_HOURS=24

if [ "${1:-}" = "--all" ]; then
    echo "[artemis-control] Cleaning ALL data..."
    if [ -d "$SCREENSHOTS_DIR" ]; then
        rm -rf "$SCREENSHOTS_DIR"
        echo "  Removed: $SCREENSHOTS_DIR"
    fi
    if [ -f "$TRACE_LOG" ]; then
        rm -f "$TRACE_LOG"
        echo "  Removed: $TRACE_LOG"
    fi
    # Also clean any __pycache__
    find "$SCRIPT_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    echo "[artemis-control] Clean complete."
else
    echo "[artemis-control] Cleaning screenshots older than ${MAX_AGE_HOURS}h..."
    if [ -d "$SCREENSHOTS_DIR" ]; then
        find "$SCREENSHOTS_DIR" -type f -mmin +$((MAX_AGE_HOURS * 60)) -delete 2>/dev/null || true
        find "$SCREENSHOTS_DIR" -type d -empty -delete 2>/dev/null || true
        echo "  Cleaned old screenshots from: $SCREENSHOTS_DIR"
    fi
    echo "[artemis-control] Clean complete."
fi