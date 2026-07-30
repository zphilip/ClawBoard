#!/usr/bin/env python3
"""
End-to-end test of ha-lite QR login flow.

This script:
1. Starts a QR login via the ha-lite API
2. Polls the status endpoint (simulating the browser JS)
3. Reports each state transition
4. After scan is detected, collects tokens and lists devices
5. Saves the service token for future use

Usage:
    python3 test_qr_flow.py                          # default: http://localhost:8090
    python3 test_qr_flow.py http://192.168.1.50:8090  # custom server
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from typing import Optional

SERVER = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8090"
TIMEOUT = 30  # total seconds to wait for scan

# ── Colors ─────────────────────────────────────────────────────────────────────

class C:
    R = "\033[0;31m"; G = "\033[0;32m"; Y = "\033[1;33m"
    B = "\033[0;34m"; C = "\033[0;36m"; W = "\033[1m"; N = "\033[0m"

# ── Helpers ────────────────────────────────────────────────────────────────────

def api(method: str, path: str, body: Optional[dict] = None) -> tuple[int, str]:
    """Call the ha-lite API."""
    url = f"{SERVER}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:
        return 0, str(e)

def print_json(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))

# ──────────────────────────────────────────────────────────────────────────────
# Step 1: Health check
# ──────────────────────────────────────────────────────────────────────────────
print(f"{C.B}═══ Step 1: Health Check ═══{C.N}")
http_code, body = api("GET", "/api/health")
print(f"  HTTP {http_code}")
try:
    health = json.loads(body)
    print(f"  Status: {health.get('status')} | Devices: {health.get('device_count')} | Cloud: {health.get('cloud_authed')}")
except json.JSONDecodeError:
    print(f"  Raw: {body[:200]}")

# ──────────────────────────────────────────────────────────────────────────────
# Step 2: Start QR login
# ──────────────────────────────────────────────────────────────────────────────
print(f"\n{C.B}═══ Step 2: Start QR Login ═══{C.N}")
http_code, body = api("POST", "/api/login/qr/start")

if http_code != 200:
    print(f"  {C.R}❌ Failed to start QR login: HTTP {http_code}{C.N}")
    print(f"  Body: {body[:500]}")
    sys.exit(1)

qr_data = json.loads(body)
print(f"  Status: {qr_data.get('status')}")

# Print QR art if available (for terminal scanning).
qr_art = qr_data.get("qr_ascii_art", "")
if qr_art:
    print(f"\n{C.Y}  📱 Scan this QR code with Mi Home app:{C.N}")
    print(qr_art)

login_url = qr_data.get("login_url", "")
if login_url:
    print(f"  {C.C}🔗 Direct login URL: {login_url}{C.N}")

qr_image_url = qr_data.get("qr_image_url", "")
if qr_image_url:
    print(f"  🖼️  QR image URL: {qr_image_url}")

print(f"  ⏱️  Timeout: {qr_data.get('timeout_seconds', '?')}s")
print(f"  📝 Next: {qr_data.get('next_step', '')}")

# ──────────────────────────────────────────────────────────────────────────────
# Step 3: Wait for scan (poll ha-lite status)
# ──────────────────────────────────────────────────────────────────────────────
print(f"\n{C.B}═══ Step 3: Waiting for Scan ═══{C.N}")
print(f"  {C.Y}Open the QR image in browser: {SERVER}/api/login/qr/image{C.N}")
print(f"  Or visit the direct login URL above.")
print(f"  {C.W}Then scan QR with Mi Home app (Profile → top-right → Scan){C.N}")
print()

start_time = time.time()
last_status = ""
while True:
    elapsed = time.time() - start_time
    if elapsed > TIMEOUT:
        print(f"\n  {C.R}⏰ Timeout after {TIMEOUT}s — scan not detected.{C.N}")
        break

    http_code, body = api("GET", "/api/login/qr/status")
    if http_code != 200:
        print(f"  {C.R}Status error HTTP {http_code}{C.N}")
        time.sleep(2)
        continue

    try:
        status = json.loads(body)
    except json.JSONDecodeError:
        print(f"  {C.R}Invalid status response: {body[:100]}{C.N}")
        time.sleep(2)
        continue

    current_status = status.get("status", "?")
    has_token = status.get("has_service_token", False)
    msg = status.get("message", "")

    # Only print on status change.
    if current_status != last_status or has_token:
        elapsed_str = f"{elapsed:.0f}s"
        if has_token or current_status == "authenticated":
            print(f"  [{elapsed_str}] {C.G}✅ Authenticated! has_service_token={has_token}{C.N}")
            break
        elif current_status == "scanned":
            print(f"  [{elapsed_str}] {C.Y}📱 QR scanned! Waiting for token exchange...{C.N}")
        elif current_status == "timeout":
            print(f"  [{elapsed_str}] {C.R}⏰ QR code expired.{C.N}")
            break
        elif current_status == "error":
            print(f"  [{elapsed_str}] {C.R}❌ Error: {msg}{C.N}")
            break
        else:
            print(f"  [{elapsed_str}] {C.C}⏳ {current_status} — {msg[:60]}", end="")
            if elapsed > 5:
                print(f" (waited {elapsed:.0f}s)")
            else:
                print()
        last_status = current_status

    sys.stdout.flush()
    time.sleep(2)

# ──────────────────────────────────────────────────────────────────────────────
# Step 4: Collect tokens (if authenticated)
# ──────────────────────────────────────────────────────────────────────────────
print(f"\n{C.B}═══ Step 4: Collect Tokens ═══{C.N}")

# Check status one more time.
http_code, body = api("GET", "/api/login/qr/status")
final_status = json.loads(body)
print(f"  Status: {final_status.get('status')} | has_token: {final_status.get('has_service_token')}")

if final_status.get("has_service_token"):
    http_code, body = api("POST", "/api/login/qr/collect")
    print(f"  Collect: HTTP {http_code}")
    try:
        result = json.loads(body)
        print(f"  Status: {result.get('status')} | Devices: {result.get('count', 0)}")
        if result.get("devices"):
            print(f"\n  {C.G}📋 Devices:{C.N}")
            for d in result["devices"]:
                online = "🟢" if d.get("online") else "🔴"
                print(f"    {online} {d.get('name','?'):<20} {d.get('model','?'):<25} {d.get('ip','?'):<15} {d.get('did','?')}")
    except json.JSONDecodeError:
        print(f"  Raw: {body[:500]}")
else:
    print(f"  {C.Y}⚠️  Not authenticated yet. Try scanning the QR code.{C.N}")
    print(f"  If you already scanned, the LP polling may not be working.")
    print(f"  Check: curl {SERVER}/api/login/qr/status")

# ──────────────────────────────────────────────────────────────────────────────
# Step 5: Save session for reuse
# ──────────────────────────────────────────────────────────────────────────────
print(f"\n{C.B}═══ Step 5: Save Session ═══{C.N}")

http_code, body = api("GET", "/api/health")
health = json.loads(body)
if health.get("cloud_authed"):
    print(f"  {C.G}✅ Cloud authenticated — session is active.{C.N}")
    print(f"  {C.W}The ha-lite server now has a valid service token.{C.N}")
    print(f"  {C.W}Devices are accessible at: {SERVER}/api/devices{C.N}")
    print(f"  {C.W}Schema for AI agents:   {SERVER}/openclaw/schema{C.N}")
else:
    print(f"  {C.Y}⚠️  Cloud not yet authenticated.{C.N}")

# ──────────────────────────────────────────────────────────────────────────────
# Summary
# ──────────────────────────────────────────────────────────────────────────────
print(f"\n{C.B}═══ Test Complete ═══{C.N}")
print(f"  If the QR was scanned but status never updated:")
print(f"    → The long-poll (LP) connection to Xiaomi may have failed.")
print(f"    → Check network connectivity to Xiaomi servers.")
print(f"    → Try the direct login URL: {login_url}")
print(f"  If authenticated successfully:")
print(f"    → Visit {SERVER}/api/devices to see your devices.")
print(f"    → OpenClaw schema at {SERVER}/openclaw/schema")