#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# ha-lite server test script
# Tests: health, schema, device list, and device control for all device types.
#
# Usage:
#   ./test_ha_lite.sh                          # default: http://localhost:8090
#   ./test_ha_lite.sh http://192.168.1.50:8090  # custom server
#   DRY_RUN=1 ./test_ha_lite.sh                 # dry-run: show commands only
#   SKIP_CONTROL=1 ./test_ha_lite.sh            # skip actual device control
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────
SERVER="${1:-http://localhost:8090}"
DRY_RUN="${DRY_RUN:-0}"
SKIP_CONTROL="${SKIP_CONTROL:-0}"
CURL_OPTS="-s -w '\n%{http_code}' --connect-timeout 5 --max-time 10"

# ── Colors ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

PASS="${GREEN}✔${NC}"; FAIL="${RED}✘${NC}"; WARN="${YELLOW}⚠${NC}"
INFO="${BLUE}ℹ${NC}"; ARROW="${CYAN}→${NC}"

passed=0; failed=0; skipped=0

# ── Helpers ──────────────────────────────────────────────────────────────────
banner() { printf "\n${BOLD}${BLUE}═══ %s ═══${NC}\n" "$*"; }
section() { printf "\n${BOLD}${CYAN}── %s ──${NC}\n" "$*"; }
info() { printf "  ${INFO} %s\n" "$*"; }
warn() { printf "  ${WARN} %s\n" "$*"; }

ok() { printf "  ${PASS} %s\n" "$*"; ((passed++)) || true; }
fail() { printf "  ${FAIL} %s\n" "$*"; ((failed++)) || true; }
skip() { printf "  ${WARN} %s (skipped)\n" "$*"; ((skipped++)) || true; }

api() {
    local method="$1" path="$2" body="${3:-}"
    local url="${SERVER}${path}"

    if [[ "$DRY_RUN" == "1" ]]; then
        if [[ -n "$body" ]]; then
            printf "  ${ARROW} DRY: curl -X ${method} '%s' -H 'Content-Type: application/json' -d '%s'\n" "$url" "$body" >&2
        else
            printf "  ${ARROW} DRY: curl -X ${method} '%s'\n" "$url" >&2
        fi
        # Return dummy HTTP 200 + empty JSON for the caller to parse.
        printf '200\n'
        printf '{"status":"ok","devices":[],"count":0}\n'
        return 0
    fi

    local http_code tmpfile
    tmpfile=$(mktemp)
    if [[ -n "$body" ]]; then
        http_code=$(curl -s -o "$tmpfile" -w '%{http_code}' \
            --connect-timeout 5 --max-time 15 \
            -X "${method}" "${url}" \
            -H 'Content-Type: application/json' \
            -d "${body}" 2>/dev/null || echo "000")
    else
        http_code=$(curl -s -o "$tmpfile" -w '%{http_code}' \
            --connect-timeout 5 --max-time 10 \
            -X "${method}" "${url}" 2>/dev/null || echo "000")
    fi

    local response
    response=$(cat "$tmpfile"); rm -f "$tmpfile"

    # Return: http_code then response (separated by newline)
    printf '%s\n' "$http_code"
    printf '%s\n' "$response"
}

check_http() {
    local code="$1" expect="$2" desc="$3"
    if [[ "$code" == "$expect" ]]; then
        ok "$desc (HTTP $code)"
        return 0
    else
        fail "$desc (expected HTTP $expect, got $code)"
        return 1
    fi
}

# ──────────────────────────────────────────────────────────────────────────────
# Test 1: Health Check
# ──────────────────────────────────────────────────────────────────────────────
banner "1. Health Check"

{ read -r http_code; read -r body; } < <(api GET /api/health)
check_http "$http_code" "200" "GET /api/health" || true

if [[ "$DRY_RUN" != "1" ]]; then
    # Pretty-print if python3 available, otherwise show raw.
    if command -v python3 &>/dev/null; then
        echo "$body" | python3 -m json.tool 2>/dev/null || echo "$body"
    else
        echo "$body"
    fi

    if echo "$body" | grep -q '"status":"ok"'; then
        ok "Server status: ok"
    else
        fail "Server status not ok"
        echo "    raw: $body"
    fi

    # Extract fields with grep/sed (no python3 dependency).
    ver=$(echo "$body" | grep -o '"version":"[^"]*"' | head -1 | sed 's/.*"version":"\([^"]*\)".*/\1/')
    devs=$(echo "$body" | grep -o '"device_count":[0-9]*' | head -1 | sed 's/.*"device_count":\([0-9]*\).*/\1/')
    cloud=$(echo "$body" | grep -o '"cloud_authed":\(true\|false\)' | head -1 | sed 's/.*"cloud_authed":\(true\|false\).*/\1/')
    [[ -z "$ver" ]] && ver="?"
    [[ -z "$devs" ]] && devs="0"
    [[ -z "$cloud" ]] && cloud="?"
    info "Version: ${ver}  |  Devices: ${devs}  |  Cloud authed: ${cloud}"
fi

# ──────────────────────────────────────────────────────────────────────────────
# Test 2: OpenClaw Schema
# ──────────────────────────────────────────────────────────────────────────────
banner "2. OpenClaw Schema"

{ read -r http_code; read -r body; } < <(api GET /openclaw/schema)
check_http "$http_code" "200" "GET /openclaw/schema" || true

if [[ "$DRY_RUN" != "1" ]]; then
    schema_name=$(echo "$body" | grep -o '"name":"[^"]*"' | head -1 | sed 's/.*"name":"\([^"]*\)".*/\1/')
    [[ -z "$schema_name" ]] && schema_name="?"
    ok "Schema name: ${schema_name}"
fi

# ──────────────────────────────────────────────────────────────────────────────
# Test 3: Device List
# ──────────────────────────────────────────────────────────────────────────────
banner "3. Device List"

{ read -r http_code; read -r body; } < <(api GET /api/devices)
check_http "$http_code" "200" "GET /api/devices" || true

declare -a DEVICE_DIDS=()
declare -a DEVICE_NAMES=()
declare -a DEVICE_MODELS=()
declare -a DEVICE_IPS=()

if [[ "$DRY_RUN" != "1" ]]; then
    count=$(echo "$body" | grep -o '"count":[0-9]*' | head -1 | sed 's/.*"count":\([0-9]*\).*/\1/')
    [[ -z "$count" ]] && count=0
    info "Total devices: ${count}"

    if [[ "$count" -eq 0 ]]; then
        warn "No devices found. Cloud sync may be needed (POST /api/sync)."
        warn "If no credentials configured, start QR login: POST /api/login/qr/start"
    else
        # Parse each device using grep/sed.
        # Extract JSON array of devices and parse each device object.
        while IFS= read -r dev_json; do
            [[ -z "$dev_json" ]] && continue
            did=$(echo "$dev_json" | grep -o '"did":"[^"]*"' | head -1 | sed 's/.*"did":"\([^"]*\)".*/\1/')
            name=$(echo "$dev_json" | grep -o '"name":"[^"]*"' | head -1 | sed 's/.*"name":"\([^"]*\)".*/\1/')
            model=$(echo "$dev_json" | grep -o '"model":"[^"]*"' | head -1 | sed 's/.*"model":"\([^"]*\)".*/\1/')
            ip=$(echo "$dev_json" | grep -o '"ip":"[^"]*"' | head -1 | sed 's/.*"ip":"\([^"]*\)".*/\1/')
            [[ -z "$did" ]] && continue
            DEVICE_DIDS+=("$did")
            DEVICE_NAMES+=("$name")
            DEVICE_MODELS+=("$model")
            DEVICE_IPS+=("$ip")
        done < <(echo "$body" | grep -oP '\{[^}]*"did"[^}]*\}')

        for i in "${!DEVICE_DIDS[@]}"; do
            printf "  ${GREEN}[%d]${NC} %-20s  ${CYAN}%-25s${NC}  %-15s  ${YELLOW}%s${NC}\n" \
                "$((i+1))" "${DEVICE_NAMES[$i]}" "${DEVICE_MODELS[$i]}" "${DEVICE_IPS[$i]}" "${DEVICE_DIDS[$i]}"
        done
    fi
fi

# ──────────────────────────────────────────────────────────────────────────────
# Test 4: Device Control (per-type)
# ──────────────────────────────────────────────────────────────────────────────
banner "4. Device Control"

if [[ "$SKIP_CONTROL" == "1" ]]; then
    skip "Device control tests (SKIP_CONTROL=1)"
    echo ""
    echo "══════════════════════════════════════════════════════════════════"
    echo "  Test Summary"
    echo "══════════════════════════════════════════════════════════════════"
    printf "  ${GREEN}Passed:${NC}  %d\n" "$passed"
    printf "  ${RED}Failed:${NC}  %d\n" "$failed"
    printf "  ${YELLOW}Skipped:${NC} %d\n" "$skipped"
    echo "══════════════════════════════════════════════════════════════════"
    if [[ "$failed" -gt 0 ]]; then
        exit 1
    fi
    exit 0
fi

if [[ "${#DEVICE_DIDS[@]}" -eq 0 ]]; then
    warn "No devices to test control on."
    echo ""
    echo "══════════════════════════════════════════════════════════════════"
    echo "  Test Summary"
    echo "══════════════════════════════════════════════════════════════════"
    printf "  ${GREEN}Passed:${NC}  %d\n" "$passed"
    printf "  ${RED}Failed:${NC}  %d\n" "$failed"
    printf "  ${YELLOW}Skipped:${NC} %d\n" "$skipped"
    echo "══════════════════════════════════════════════════════════════════"
    exit 0
fi

# Classify each device by model and test appropriate commands.
for i in "${!DEVICE_DIDS[@]}"; do
    did="${DEVICE_DIDS[$i]}"
    name="${DEVICE_NAMES[$i]}"
    model="${DEVICE_MODELS[$i]}"
    model_lower=$(echo "$model" | tr '[:upper:]' '[:lower:]')

    section "Device: ${name} (${model})"

    # ── Determine device type ────────────────────────────────────────────────
    case "$model_lower" in
        *light*|*lamp*|*bulb*|*yeelight*|*philips*)
            dev_type="light"
            ;;
        *plug*|*outlet*|*socket*|*switch*|*relay*|*cuco*)
            dev_type="switch"
            ;;
        *robot*|*vacuum*|*sweep*|*clean*|*roborock*|*dreame*|*viomi*|*mijia*vacuum*)
            dev_type="robot"
            ;;
        *air*|*purifier*|*filter*)
            dev_type="purifier"
            ;;
        *humidifier*|*dehumidifier*)
            dev_type="humidifier"
            ;;
        *ac*|*aircondition*|*aircon*|*climate*)
            dev_type="ac"
            ;;
        *curtain*|*blind*|*shade*|*window*)
            dev_type="curtain"
            ;;
        *fan*|*ventilator*|*ventilation*)
            dev_type="fan"
            ;;
        *heater*|*radiator*|*warming*)
            dev_type="heater"
            ;;
        *camera*|*cam*|*ipc*|*mijia*camera*|*xiaomi*camera*)
            dev_type="camera"
            ;;
        *gateway*|*hub*)
            dev_type="gateway"
            ;;
        *sensor*|*motion*|*contact*|*temperature*|*humidity*)
            dev_type="sensor"
            ;;
        *)
            dev_type="generic"
            ;;
    esac

    # ── Test based on device type ────────────────────────────────────────────
    case "$dev_type" in
        light)
            info "Type: Light — testing on/off, brightness, color_temp"
            test_control "$did" "on"  "Turn on ${name}"
            test_control "$did" "off" "Turn off ${name}"
            test_control "$did" "brightness:50"  "Set brightness 50%"
            test_control "$did" "brightness:100" "Set brightness 100%"
            test_control "$did" "color_temp:4000" "Set color temp 4000K"
            ;;
        switch)
            info "Type: Switch/Plug — testing on/off/toggle"
            test_control "$did" "on"  "Turn on ${name}"
            test_control "$did" "off" "Turn off ${name}"
            test_control "$did" "on"  "Turn on ${name} (restore)"
            ;;
        robot)
            info "Type: Robot Vacuum — testing start/stop cleaning"
            test_control "$did" "on"  "Start cleaning (${name})"
            info "Waiting 3s before stop..."
            sleep 3
            test_control "$did" "off" "Stop cleaning (${name})"
            ;;
        purifier)
            info "Type: Air Purifier — testing on/off, fan_speed, mode"
            test_control "$did" "on"  "Turn on ${name}"
            test_control "$did" "fan_speed:1" "Set fan speed 1"
            test_control "$did" "fan_speed:2" "Set fan speed 2"
            test_control "$did" "mode:auto" "Set mode auto"
            test_control "$did" "off" "Turn off ${name}"
            ;;
        humidifier)
            info "Type: Humidifier — testing on/off, humidity"
            test_control "$did" "on"  "Turn on ${name}"
            test_control "$did" "humidity:60" "Set target humidity 60%"
            test_control "$did" "off" "Turn off ${name}"
            ;;
        ac)
            info "Type: AC — testing on/off, temperature, mode"
            test_control "$did" "on"  "Turn on ${name}"
            test_control "$did" "mode:cool" "Set mode cool"
            test_control "$did" "temperature:24" "Set temperature 24°C"
            test_control "$did" "fan_speed:2" "Set fan speed 2"
            test_control "$did" "off" "Turn off ${name}"
            ;;
        curtain)
            info "Type: Curtain/Blind — testing open/close, position"
            test_control "$did" "on"  "Open ${name}"
            test_control "$did" "position:50" "Set position 50%"
            test_control "$did" "off" "Close ${name}"
            ;;
        fan)
            info "Type: Fan — testing on/off, fan_speed, oscillate"
            test_control "$did" "on"  "Turn on ${name}"
            test_control "$did" "fan_speed:2" "Set fan speed 2"
            test_control "$did" "oscillate:on" "Enable oscillation"
            test_control "$did" "oscillate:off" "Disable oscillation"
            test_control "$did" "off" "Turn off ${name}"
            ;;
        heater)
            info "Type: Heater — testing on/off, temperature"
            test_control "$did" "on"  "Turn on ${name}"
            test_control "$did" "temperature:22" "Set temperature 22°C"
            test_control "$did" "off" "Turn off ${name}"
            ;;
        camera|gateway)
            skip "${name} (${dev_type} — read-only device, no control actions)"
            ;;
        sensor)
            skip "${name} (${dev_type} — read-only device, no control actions)"
            ;;
        generic)
            info "Type: Generic — testing basic on/off only"
            test_control "$did" "on"  "Turn on ${name}"
            test_control "$did" "off" "Turn off ${name}"
            ;;
    esac
done

# ──────────────────────────────────────────────────────────────────────────────
# Test 5: Force Cloud Sync
# ──────────────────────────────────────────────────────────────────────────────
banner "5. Force Cloud Sync"

{ read -r http_code; read -r body; } < <(api POST /api/sync)
check_http "$http_code" "200" "POST /api/sync" || true

if [[ "$DRY_RUN" != "1" ]]; then
    sync_status=$(echo "$body" | grep -o '"status":"[^"]*"' | head -1 | sed 's/.*"status":"\([^"]*\)".*/\1/')
    if [[ "$sync_status" == "synced" ]]; then
        ok "Cloud sync: ${sync_status}"
    elif [[ "$sync_status" == "failed" ]]; then
        fail "Cloud sync failed: $body"
    else
        warn "Cloud sync: ${sync_status:-unknown}"
    fi
fi

# ──────────────────────────────────────────────────────────────────────────────
# Summary
# ──────────────────────────────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════════════════════════"
echo "  Test Summary"
echo "══════════════════════════════════════════════════════════════════"
printf "  ${GREEN}Passed:${NC}  %d\n" "$passed"
printf "  ${RED}Failed:${NC}  %d\n" "$failed"
printf "  ${YELLOW}Skipped:${NC} %d\n" "$skipped"
echo "══════════════════════════════════════════════════════════════════"

if [[ "$failed" -gt 0 ]]; then
    exit 1
fi
exit 0

# ──────────────────────────────────────────────────────────────────────────────
# Helper: test a single control action
# ──────────────────────────────────────────────────────────────────────────────
test_control() {
    local did="$1" action="$2" desc="$3"

    if [[ "$DRY_RUN" == "1" ]]; then
        printf "  ${ARROW} DRY: POST /api/control {\"did\":\"%s\",\"action\":\"%s\"}  # %s\n" "$did" "$action" "$desc"
        ((skipped++)) || true
        return
    fi

    local http_code tmpfile body
    tmpfile=$(mktemp)
    http_code=$(curl -s -o "$tmpfile" -w '%{http_code}' \
        --connect-timeout 5 --max-time 15 \
        -X POST "${SERVER}/api/control" \
        -H 'Content-Type: application/json' \
        -d "{\"did\":\"${did}\",\"action\":\"${action}\"}" 2>/dev/null || echo "000")
    body=$(cat "$tmpfile"); rm -f "$tmpfile"

    if [[ "$http_code" == "200" ]]; then
        ctrl_status=$(echo "$body" | grep -o '"status":"[^"]*"' | head -1 | sed 's/.*"status":"\([^"]*\)".*/\1/')
        if [[ "$ctrl_status" == "success" ]]; then
            ok "$desc"
        else
            ctrl_err=$(echo "$body" | grep -o '"error":"[^"]*"' | head -1 | sed 's/.*"error":"\([^"]*\)".*/\1/')
            warn "$desc — device returned: ${ctrl_status:-?} ${ctrl_err}"
        fi
    elif [[ "$http_code" == "000" ]]; then
        fail "$desc — server unreachable"
    else
        fail "$desc — HTTP ${http_code}: $(echo "$body" | head -c 200)"
    fi
}