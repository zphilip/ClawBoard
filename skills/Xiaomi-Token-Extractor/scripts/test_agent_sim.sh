#!/usr/bin/env bash
# test_agent_sim.sh — Simulates exactly how an AI agent (openclaw) calls
# extract_tokens.py.  Mirrors the two-phase tool-call pattern:
#
#   Phase 1:  agent runs the script and captures ALL output only after it exits
#             (no streaming — this is the critical constraint that causes "expired")
#   Phase 2:  agent shows QR to user, then runs --collect in a second tool call
#
# Usage:
#   bash test_agent_sim.sh [--server cn] [--filter TEXT] [--port 31415]
#   bash test_agent_sim.sh --help

set -euo pipefail

# ── Colour helpers ─────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[SIM]${RESET} $*"; }
ok()      { echo -e "${GREEN}[OK] ${RESET} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET} $*"; }
fail()    { echo -e "${RED}[FAIL]${RESET} $*" >&2; exit 1; }
section() { echo -e "\n${BOLD}${CYAN}━━━  $*  ━━━${RESET}"; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EXTRACT="$SCRIPT_DIR/extract_tokens.py"

[ -f "$EXTRACT" ] || fail "extract_tokens.py not found at $EXTRACT"

# ── Parse our own args (pass the rest through to extract_tokens.py) ────────────
PASSTHROUGH_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h)
            echo "Usage: $0 [--server cn|de|us|...] [--filter TEXT] [--port PORT]"
            echo "       Simulates an AI agent calling extract_tokens.py in two phases."
            exit 0 ;;
        *) PASSTHROUGH_ARGS+=("$1"); shift ;;
    esac
done

# ── Phase 1: exactly what an agent tool-call does ─────────────────────────────
section "PHASE 1 — Agent tool call (captures output only after script exits)"
info "Running: python3 extract_tokens.py ${PASSTHROUGH_ARGS[*]:-}"
info "Timing the call... (should complete in ≤5 s)"

T_START=$(date +%s%3N)

# KEY SIMULATION DETAIL: $() captures ALL output only after the subprocess exits.
# This is identical to how openclaw/Claude tool-use receives script output.
PHASE1_OUTPUT=$(python3 "$EXTRACT" "${PASSTHROUGH_ARGS[@]:-}" 2>/tmp/phase1_stderr.txt)

T_END=$(date +%s%3N)
ELAPSED=$(( T_END - T_START ))

echo ""
echo -e "${BOLD}--- Raw Phase 1 output (what the agent receives) ---${RESET}"
echo "$PHASE1_OUTPUT"
echo -e "${BOLD}----------------------------------------------------${RESET}"
echo ""
info "Phase 1 completed in ${BOLD}${ELAPSED} ms${RESET}"

if (( ELAPSED > 10000 )); then
    fail "Phase 1 took ${ELAPSED}ms — WAY too long. Two-phase split is not working! QR would be expired."
elif (( ELAPSED > 4000 )); then
    warn "Phase 1 took ${ELAPSED}ms — acceptable but slow. Xiaomi QR sessions last ~60-120s."
else
    ok "Phase 1 exited fast (${ELAPSED}ms) — QR session is still fresh ✓"
fi

if [ -s /tmp/phase1_stderr.txt ]; then
    echo -e "${YELLOW}--- stderr (QR art / warnings) ---${RESET}"
    cat /tmp/phase1_stderr.txt
    echo -e "${YELLOW}----------------------------------${RESET}"
fi

# ── Parse Phase 1 output keys ─────────────────────────────────────────────────
section "Parsing Phase 1 output keys"

get_key() { echo "$PHASE1_OUTPUT" | grep "^$1=" | head -1 | cut -d= -f2-; }

SESSION_FILE=$(get_key SESSION_FILE)
QR_COLLECT_CMD=$(get_key QR_COLLECT_CMD)
QR_IMAGE_URL=$(get_key QR_IMAGE_URL)
QR_IMAGE_B64=$(get_key QR_IMAGE_B64)
QR_SERVER_PID=$(get_key QR_SERVER_PID)
STATUS=$(get_key STATUS)

# Check for failures
if echo "$PHASE1_OUTPUT" | grep -q "^STATUS=login_failed"; then
    REASON=$(get_key STATUS)
    fail "Login failed in Phase 1: $REASON"
fi

[ -n "$SESSION_FILE" ] && ok "SESSION_FILE=$SESSION_FILE" || fail "SESSION_FILE not found in output — two-phase split is broken"
[ -n "$QR_COLLECT_CMD" ] && ok "QR_COLLECT_CMD found" || fail "QR_COLLECT_CMD not found in output"
[ -n "$QR_IMAGE_URL" ] && ok "QR_IMAGE_URL=$QR_IMAGE_URL" || warn "QR_IMAGE_URL not found"
[ -n "$QR_SERVER_PID" ] && ok "QR server PID=$QR_SERVER_PID" || warn "QR_SERVER_PID not found"
[ "$STATUS" = "waiting_for_scan" ] && ok "STATUS=waiting_for_scan" || warn "STATUS=$STATUS (expected waiting_for_scan)"

[ -f "$SESSION_FILE" ] && ok "Session file exists on disk" || fail "Session file $SESSION_FILE does not exist"

# ── QR image: save and verify ─────────────────────────────────────────────────
section "QR Image verification"

QR_PNG_PATH="/tmp/sim_qr_$(date +%s).png"

if [ -n "$QR_IMAGE_B64" ]; then
    echo "$QR_IMAGE_B64" | base64 -d > "$QR_PNG_PATH" 2>/dev/null
    SIZE=$(wc -c < "$QR_PNG_PATH")
    if (( SIZE > 100 )); then
        ok "QR_IMAGE_B64 decoded to ${SIZE} bytes PNG → $QR_PNG_PATH"
    else
        warn "QR_IMAGE_B64 decoded but file is tiny (${SIZE} bytes) — may be corrupt"
    fi
else
    warn "QR_IMAGE_B64 not present — falling back to URL"
fi

if [ -n "$QR_IMAGE_URL" ]; then
    info "Verifying QR image URL is reachable: $QR_IMAGE_URL"
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$QR_IMAGE_URL" 2>/dev/null || echo "000")
    if [ "$HTTP_CODE" = "200" ]; then
        ok "QR server is reachable (HTTP $HTTP_CODE) — detached server is alive ✓"
    else
        warn "QR server returned HTTP $HTTP_CODE — server may not be up yet or port is blocked"
    fi
fi

# ── Show QR to user (what the agent does after Phase 1) ───────────────────────
section "Showing QR to user (agent step)"

echo ""
echo -e "${BOLD}The agent would show this QR image to the user.${RESET}"
if [ -f "$QR_PNG_PATH" ] && (( $(wc -c < "$QR_PNG_PATH") > 100 )); then
    echo -e "  QR image saved to: ${CYAN}$QR_PNG_PATH${RESET}"
    echo -e "  QR image URL:      ${CYAN}$QR_IMAGE_URL${RESET}"
    # Try to open the image if a viewer is available
    for viewer in eog feh xdg-open display; do
        if command -v "$viewer" &>/dev/null; then
            info "Opening QR image with '$viewer'..."
            "$viewer" "$QR_PNG_PATH" &>/dev/null &
            break
        fi
    done
fi
echo ""
echo -e "${YELLOW}  ➤  Open ${BOLD}$QR_IMAGE_URL${RESET}${YELLOW} in your phone's browser (same WiFi)${RESET}"
echo -e "${YELLOW}  ➤  OR scan the saved PNG: ${BOLD}$QR_PNG_PATH${RESET}"
echo -e "${YELLOW}  ➤  Use Mi Home app: Profile → top-right ··· → Scan${RESET}"
echo ""

# ── Phase 2: agent runs collect after showing QR ──────────────────────────────
section "PHASE 2 — Agent runs --collect (long-polls for scan)"
echo -e "${BOLD}Press ENTER when ready to start Phase 2 (or scan first, then press ENTER):${RESET}"
read -r

info "Running Phase 2: $QR_COLLECT_CMD"
T2_START=$(date +%s%3N)

PHASE2_OUTPUT=$(eval "$QR_COLLECT_CMD" 2>/tmp/phase2_stderr.txt) || true

T2_END=$(date +%s%3N)
T2_ELAPSED=$(( T2_END - T2_START ))

echo ""
echo -e "${BOLD}--- Raw Phase 2 output ---${RESET}"
echo "$PHASE2_OUTPUT"
echo -e "${BOLD}--------------------------${RESET}"
echo ""

if [ -s /tmp/phase2_stderr.txt ]; then
    cat /tmp/phase2_stderr.txt
fi

# ── Phase 2 result ────────────────────────────────────────────────────────────
section "Phase 2 result"

P2_STATUS=$(echo "$PHASE2_OUTPUT" | grep "^STATUS=" | tail -1 | cut -d= -f2-)
DEVICE_COUNT=$(echo "$PHASE2_OUTPUT" | grep -c "^DEVICE=" || true)
DONE_LINE=$(echo "$PHASE2_OUTPUT" | grep "^DONE " | head -1)

case "$P2_STATUS" in
    login_success)
        ok "Login successful ✓  (Phase 2 completed in ${T2_ELAPSED}ms)"
        ok "Devices found: ${DEVICE_COUNT}"
        [ -n "$DONE_LINE" ] && ok "$DONE_LINE"
        ;;
    login_timeout)
        fail "Login timed out — QR was not scanned within the timeout window"
        ;;
    login_failed*)
        fail "Login failed: $P2_STATUS"
        ;;
    "")
        warn "No STATUS line found in Phase 2 output"
        ;;
    *)
        warn "Unexpected status: $P2_STATUS"
        ;;
esac

# ── Session file cleanup check ────────────────────────────────────────────────
if [ ! -f "$SESSION_FILE" ]; then
    ok "Session file cleaned up ✓"
else
    warn "Session file still exists: $SESSION_FILE (Phase 2 may have failed)"
fi

section "Simulation complete"
echo -e "  Phase 1 time: ${BOLD}${ELAPSED}ms${RESET}"
echo -e "  Phase 2 time: ${BOLD}${T2_ELAPSED}ms${RESET}"
echo -e "  Devices:      ${BOLD}${DEVICE_COUNT}${RESET}"
[ "$P2_STATUS" = "login_success" ] && echo -e "  Result: ${GREEN}${BOLD}PASS ✓${RESET}" || echo -e "  Result: ${RED}${BOLD}FAIL ✗${RESET}"
