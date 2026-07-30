#!/usr/bin/env python3
"""
ha-lite server test script (Python edition).

Tests: health, schema, device list, and per-type device control.

Usage:
    python3 test_ha_lite.py                           # default: http://localhost:8090
    python3 test_ha_lite.py http://192.168.1.50:8090   # custom server
    DRY_RUN=1 python3 test_ha_lite.py                  # dry-run: show commands only
    SKIP_CONTROL=1 python3 test_ha_lite.py             # skip device control
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from typing import Optional

# ── Configuration ──────────────────────────────────────────────────────────────

SERVER = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8090"
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
SKIP_CONTROL = os.environ.get("SKIP_CONTROL", "0") == "1"
TIMEOUT = 10

# ── Colors ─────────────────────────────────────────────────────────────────────

class Color:
    RED    = "\033[0;31m"
    GREEN  = "\033[0;32m"
    YELLOW = "\033[1;33m"
    BLUE   = "\033[0;34m"
    CYAN   = "\033[0;36m"
    BOLD   = "\033[1m"
    NC     = "\033[0m"

PASS  = f"{Color.GREEN}✔{Color.NC}"
FAIL  = f"{Color.RED}✘{Color.NC}"
WARN  = f"{Color.YELLOW}⚠{Color.NC}"
INFO  = f"{Color.BLUE}ℹ{Color.NC}"
ARROW = f"{Color.CYAN}→{Color.NC}"

passed = 0
failed = 0
skipped = 0

# ── Helpers ────────────────────────────────────────────────────────────────────

def banner(text: str):
    print(f"\n{Color.BOLD}{Color.BLUE}═══ {text} ═══{Color.NC}")

def section(text: str):
    print(f"\n{Color.BOLD}{Color.CYAN}── {text} ──{Color.NC}")

def info(text: str):
    print(f"  {INFO} {text}")

def warn(text: str):
    print(f"  {WARN} {text}")

def ok(text: str):
    global passed
    print(f"  {PASS} {text}")
    passed += 1

def fail(text: str):
    global failed
    print(f"  {FAIL} {text}")
    failed += 1

def skip(text: str):
    global skipped
    print(f"  {WARN} {text} (skipped)")
    skipped += 1


def api(method: str, path: str, body: Optional[dict] = None) -> tuple[int, str]:
    """Call the ha-lite API. Returns (http_code, response_text)."""
    url = f"{SERVER}{path}"

    if DRY_RUN:
        if body is not None:
            body_str = json.dumps(body)
            print(f"  {ARROW} DRY: curl -X {method} '{url}' -H 'Content-Type: application/json' -d '{body_str}'")
        else:
            print(f"  {ARROW} DRY: curl -X {method} '{url}'")
        return 200, '{"status":"ok","devices":[],"count":0}'

    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return 0, str(e)


def check_http(http_code: int, expect: int, desc: str) -> bool:
    if http_code == expect:
        ok(f"{desc} (HTTP {http_code})")
        return True
    else:
        fail(f"{desc} (expected HTTP {expect}, got {http_code})")
        return False


def infer_device_type(model: str) -> str:
    """Classify a device by its model string."""
    m = model.lower()

    if any(k in m for k in ("light", "lamp", "bulb", "yeelight", "philips")):
        return "light"
    if any(k in m for k in ("plug", "outlet", "socket", "switch", "relay", "cuco")):
        return "switch"
    if any(k in m for k in ("robot", "vacuum", "sweep", "clean", "roborock", "dreame", "viomi", "mijia*vacuum")):
        return "robot"
    if any(k in m for k in ("air", "purifier", "filter")):
        return "purifier"
    if any(k in m for k in ("humidifier", "dehumidifier")):
        return "humidifier"
    if any(k in m for k in ("ac", "aircondition", "aircon", "climate")):
        return "ac"
    if any(k in m for k in ("curtain", "blind", "shade", "window")):
        return "curtain"
    if any(k in m for k in ("fan", "ventilator", "ventilation")):
        return "fan"
    if any(k in m for k in ("heater", "radiator", "warming")):
        return "heater"
    if any(k in m for k in ("camera", "cam", "ipc")):
        return "camera"
    if any(k in m for k in ("gateway", "hub")):
        return "gateway"
    if any(k in m for k in ("sensor", "motion", "contact", "temperature", "humidity")):
        return "sensor"

    return "generic"


def test_control(did: str, action: str, desc: str):
    """Execute a single device control action and report the result."""
    if DRY_RUN:
        print(f'  {ARROW} DRY: POST /api/control {{"did":"{did}","action":"{action}"}}  # {desc}')
        global skipped
        skipped += 1
        return

    http_code, body = api("POST", "/api/control", {"did": did, "action": action})

    if http_code == 200:
        try:
            result = json.loads(body)
            status = result.get("status", "?")
            if status == "success":
                ok(desc)
            else:
                err = result.get("error", "")
                warn(f"{desc} — device returned: {status} {err}")
        except json.JSONDecodeError:
            warn(f"{desc} — unparseable response: {body[:100]}")
    elif http_code == 0:
        fail(f"{desc} — server unreachable")
    else:
        fail(f"{desc} — HTTP {http_code}: {body[:200]}")


# ──────────────────────────────────────────────────────────────────────────────
# Test 1: Health Check
# ──────────────────────────────────────────────────────────────────────────────

banner("1. Health Check")

http_code, body = api("GET", "/api/health")
check_http(http_code, 200, "GET /api/health")

try:
    health = json.loads(body)
    if health.get("status") == "ok":
        ok("Server status: ok")
    else:
        fail(f"Server status not ok: {health.get('status')}")

    ver = health.get("version", "?")
    devs = health.get("device_count", 0)
    cloud = health.get("cloud_authed", False)
    info(f"Version: {ver}  |  Devices: {devs}  |  Cloud authed: {cloud}")
except json.JSONDecodeError:
    fail(f"Cannot parse health response: {body[:200]}")

# ──────────────────────────────────────────────────────────────────────────────
# Test 2: OpenClaw Schema
# ──────────────────────────────────────────────────────────────────────────────

banner("2. OpenClaw Schema")

http_code, body = api("GET", "/openclaw/schema")
check_http(http_code, 200, "GET /openclaw/schema")

try:
    schema = json.loads(body)
    name = schema.get("name", "?")
    ok(f"Schema name: {name}")

    endpoints = schema.get("endpoints", {})
    info(f"Endpoints: {len(endpoints)} ({', '.join(endpoints.keys())})")

    devices_in_schema = schema.get("devices", [])
    info(f"Devices in schema: {len(devices_in_schema)}")
except json.JSONDecodeError:
    fail(f"Cannot parse schema response: {body[:200]}")

# ──────────────────────────────────────────────────────────────────────────────
# Test 3: Device List
# ──────────────────────────────────────────────────────────────────────────────

banner("3. Device List")

http_code, body = api("GET", "/api/devices")
check_http(http_code, 200, "GET /api/devices")

devices = []
try:
    data = json.loads(body)
    devices = data.get("devices", [])
    count = data.get("count", len(devices))
    info(f"Total devices: {count}")

    if count == 0:
        warn("No devices found. Cloud sync may be needed (POST /api/sync).")
        warn("If no credentials configured, start QR login: POST /api/login/qr/start")
    else:
        for i, d in enumerate(devices):
            name = d.get("name", "?")
            model = d.get("model", "?")
            ip = d.get("ip", "?")
            did = d.get("did", "?")
            online = "🟢" if d.get("online") else "🔴"
            print(f"  {Color.GREEN}[{i+1}]{Color.NC} {online} {name:<20}  {Color.CYAN}{model:<25}{Color.NC}  {ip:<15}  {Color.YELLOW}{did}{Color.NC}")
except json.JSONDecodeError:
    fail(f"Cannot parse device list: {body[:200]}")

# ──────────────────────────────────────────────────────────────────────────────
# Test 4: Device Control (per type)
# ──────────────────────────────────────────────────────────────────────────────

banner("4. Device Control")

if SKIP_CONTROL:
    skip("Device control tests (SKIP_CONTROL=1)")
    devices = []

if not devices:
    warn("No devices to test control on.")
else:
    for d in devices:
        did = d.get("did", "")
        name = d.get("name", "?")
        model = d.get("model", "")
        model_lower = model.lower()

        section(f"Device: {name} ({model})")
        dev_type = infer_device_type(model)

        # ── Light ────────────────────────────────────────────────────────────
        if dev_type == "light":
            info("Type: Light — testing on/off, brightness, color_temp")
            test_control(did, "on", f"Turn on {name}")
            test_control(did, "off", f"Turn off {name}")
            test_control(did, "brightness:50", f"Set brightness 50%")
            test_control(did, "brightness:100", f"Set brightness 100%")
            test_control(did, "color_temp:4000", f"Set color temp 4000K")

        # ── Switch / Plug ────────────────────────────────────────────────────
        elif dev_type == "switch":
            info("Type: Switch/Plug — testing on/off/toggle")
            test_control(did, "on", f"Turn on {name}")
            test_control(did, "off", f"Turn off {name}")
            test_control(did, "on", f"Turn on {name} (restore)")

        # ── Robot Vacuum ─────────────────────────────────────────────────────
        elif dev_type == "robot":
            info("Type: Robot Vacuum — testing start/stop cleaning")
            test_control(did, "on", f"Start cleaning ({name})")
            info("Waiting 3s before stop...")
            time.sleep(3)
            test_control(did, "off", f"Stop cleaning ({name})")

        # ── Air Purifier ─────────────────────────────────────────────────────
        elif dev_type == "purifier":
            info("Type: Air Purifier — testing on/off, fan_speed, mode")
            test_control(did, "on", f"Turn on {name}")
            test_control(did, "fan_speed:1", f"Set fan speed 1")
            test_control(did, "fan_speed:2", f"Set fan speed 2")
            test_control(did, "mode:auto", f"Set mode auto")
            test_control(did, "off", f"Turn off {name}")

        # ── Humidifier ───────────────────────────────────────────────────────
        elif dev_type == "humidifier":
            info("Type: Humidifier — testing on/off, humidity")
            test_control(did, "on", f"Turn on {name}")
            test_control(did, "humidity:60", f"Set target humidity 60%")
            test_control(did, "off", f"Turn off {name}")

        # ── Air Conditioner ──────────────────────────────────────────────────
        elif dev_type == "ac":
            info("Type: AC — testing on/off, temperature, mode")
            test_control(did, "on", f"Turn on {name}")
            test_control(did, "mode:cool", f"Set mode cool")
            test_control(did, "temperature:24", f"Set temperature 24°C")
            test_control(did, "fan_speed:2", f"Set fan speed 2")
            test_control(did, "off", f"Turn off {name}")

        # ── Curtain / Blind ──────────────────────────────────────────────────
        elif dev_type == "curtain":
            info("Type: Curtain/Blind — testing open/close, position")
            test_control(did, "on", f"Open {name}")
            test_control(did, "position:50", f"Set position 50%")
            test_control(did, "off", f"Close {name}")

        # ── Fan ──────────────────────────────────────────────────────────────
        elif dev_type == "fan":
            info("Type: Fan — testing on/off, fan_speed, oscillate")
            test_control(did, "on", f"Turn on {name}")
            test_control(did, "fan_speed:2", f"Set fan speed 2")
            test_control(did, "oscillate:on", f"Enable oscillation")
            test_control(did, "oscillate:off", f"Disable oscillation")
            test_control(did, "off", f"Turn off {name}")

        # ── Heater ───────────────────────────────────────────────────────────
        elif dev_type == "heater":
            info("Type: Heater — testing on/off, temperature")
            test_control(did, "on", f"Turn on {name}")
            test_control(did, "temperature:22", f"Set temperature 22°C")
            test_control(did, "off", f"Turn off {name}")

        # ── Read-only devices ────────────────────────────────────────────────
        elif dev_type in ("camera", "gateway", "sensor"):
            skip(f"{name} ({dev_type} — read-only device, no control actions)")

        # ── Generic / Unknown ────────────────────────────────────────────────
        else:
            info("Type: Generic — testing basic on/off only")
            test_control(did, "on", f"Turn on {name}")
            test_control(did, "off", f"Turn off {name}")

# ──────────────────────────────────────────────────────────────────────────────
# Test 5: QR Login endpoints
# ──────────────────────────────────────────────────────────────────────────────

banner("5. QR Login Endpoints")

section("Check QR status (idle)")
http_code, body = api("GET", "/api/login/qr/status")
check_http(http_code, 200, "GET /api/login/qr/status")
try:
    qr = json.loads(body)
    info(f"QR status: {qr.get('status', '?')}")
except json.JSONDecodeError:
    warn(f"Unparseable QR status: {body[:100]}")

section("Start QR login")
http_code, body = api("POST", "/api/login/qr/start")
# 200 = QR started OK, 500 = network error reaching Xiaomi (expected without internet)
if http_code == 200:
    try:
        qr_data = json.loads(body)
        info(f"QR login started: status={qr_data.get('status')}")
        img_url = qr_data.get("qr_image_url", "")
        if img_url:
            info(f"QR image URL: {img_url}")
        # Print QR ASCII art if available (scannable directly from terminal).
        qr_art = qr_data.get("qr_ascii_art", "")
        if qr_art:
            print(f"\n  {Color.BOLD}📱 Scan the QR below with Mi Home app:{Color.NC}")
            print(qr_art)
    except json.JSONDecodeError:
        warn(f"Unparseable QR start response: {body[:100]}")
elif http_code == 500:
    warn(f"QR login start returned 500 — Xiaomi cloud may be unreachable (network issue or region block)")
else:
    warn(f"QR login start: HTTP {http_code}")

section("Cancel QR login")
http_code, body = api("POST", "/api/login/qr/cancel")
check_http(http_code, 200, "POST /api/login/qr/cancel")

# ──────────────────────────────────────────────────────────────────────────────
# Test 6: Force Cloud Sync
# ──────────────────────────────────────────────────────────────────────────────

banner("6. Force Cloud Sync")

http_code, body = api("POST", "/api/sync")
# 200 = synced, 500 = not authenticated (expected without credentials)
if http_code == 200:
    try:
        sync_data = json.loads(body)
        status = sync_data.get("status", "?")
        if status == "synced":
            ok(f"Cloud sync: {status}")
        else:
            warn(f"Cloud sync status: {status}")
    except json.JSONDecodeError:
        warn(f"Unparseable sync response: {body[:100]}")
elif http_code == 500:
    warn(f"Cloud sync returned 500 — not authenticated (expected without login)")
else:
    warn(f"Cloud sync: HTTP {http_code}")

# ──────────────────────────────────────────────────────────────────────────────
# Summary
# ──────────────────────────────────────────────────────────────────────────────

print("")
print("══════════════════════════════════════════════════════════════════")
print("  Test Summary")
print("══════════════════════════════════════════════════════════════════")
print(f"  {Color.GREEN}Passed:{Color.NC}  {passed}")
print(f"  {Color.RED}Failed:{Color.NC}  {failed}")
print(f"  {Color.YELLOW}Skipped:{Color.NC} {skipped}")
print("══════════════════════════════════════════════════════════════════")

sys.exit(1 if failed > 0 else 0)