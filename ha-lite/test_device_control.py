#!/usr/bin/env python3
"""
Targeted device control test for ha-lite.

Usage:
    python3 test_device_control.py                           # default: localhost:8090
    python3 test_device_control.py http://192.168.1.50:8090   # custom server
    python3 test_device_control.py http://192.168.1.50:8090 --dry-run
"""

import json
import sys
import time
import urllib.request
import urllib.error

SERVER = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "http://localhost:8090"
DRY_RUN = "--dry-run" in sys.argv

# ── Colors ─────────────────────────────────────────────────────────────────────
class C:
    R = "\033[0;31m"; G = "\033[0;32m"; Y = "\033[1;33m"
    B = "\033[0;34m"; C = "\033[0;36m"; W = "\033[1m"; N = "\033[0m"

def api(method: str, path: str, body: dict | None = None, timeout: int = 15) -> tuple[int, str]:
    """Call ha-lite API."""
    url = f"{SERVER}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:
        return 0, json.dumps({"error": str(e)})

# ── Step 1: List all devices ────────────────────────────────────────────────────
print(f"{C.B}═══ Step 1: List Devices ═══{C.N}")
http_code, body = api("GET", "/api/devices")
if http_code != 200:
    print(f"  {C.R}❌ Cannot reach ha-lite: HTTP {http_code}{C.N}")
    sys.exit(1)

data = json.loads(body)
devices = data.get("devices", [])
print(f"  Total devices: {len(devices)}")

if len(devices) == 0:
    print(f"  {C.Y}⚠️  No devices. Did you complete QR login?{C.N}")
    sys.exit(1)

# Find target devices.
target_names = ["筒灯电视上3", "风扇"]
targets = []
for d in devices:
    name = d.get("name", "")
    for tn in target_names:
        if tn in name:
            targets.append(d)
            break

if not targets:
    # Show all devices so user can find the right names.
    print(f"  {C.Y}⚠️  Target devices not found by name. Available devices:{C.N}")
    for d in sorted(devices, key=lambda x: x.get("name", "")):
        print(f"    {d.get('name','?'):<30} {d.get('model','?'):<30} {d.get('did','?'):<20} online={d.get('online')}  ip={d.get('ip','?')}")
    print(f"\n  {C.Y}Usage: edit TARGET_NAMES in this script to match your device names{C.N}")
    sys.exit(1)

print(f"  {C.G}Found targets:{C.N}")
for d in targets:
    print(f"    {d['name']:<30} {d['model']:<30} {d['did']:<20} online={d.get('online')}  ip={d.get('ip','?')}")

# ── Step 2: Quick UDP reachability check ────────────────────────────────────────
print(f"\n{C.B}═══ Step 2: UDP Reachability Check ═══{C.N}")
for d in targets:
    ip = d.get("ip", "")
    name = d["name"]
    if not ip:
        continue
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2)
    try:
        sock.sendto(b"", (ip, 54321))
        # This will NOT get a response but verifies the network path.
        print(f"  {name}: UDP packet sent to {ip}:54321 (no error = reachable)")
    except Exception as e:
        print(f"  {C.R}{name}: UDP send to {ip}:54321 failed — {e}{C.N}")
    finally:
        sock.close()

# ── Step 3: Test control on each target ─────────────────────────────────────────
print(f"\n{C.B}═══ Step 3: Test Control ═══{C.N}")

for d in targets:
    name = d["name"]
    model = d["model"].lower()
    did = d["did"]
    ip = d.get("ip", "")
    online = d.get("online", False)

    print(f"\n{C.W}── {name} ({model}) ──{C.N}")
    if not ip:
        print(f"  {C.Y}⚠️  No IP — device may be offline or BLE{C.N}")
        continue
    if not online:
        print(f"  {C.Y}⚠️  Device marked offline (ip={ip}) — attempting anyway{C.N}")

    actions = []
    if "light" in model or "lamp" in model or "bulb" in model or "downlight" in model:
        actions = ["on", "off", "on"]
        print(f"  Type: Light → testing on → off → on")
    elif "fan" in model:
        actions = ["on", "off", "fan_speed:2", "on"]
        print(f"  Type: Fan → testing on → off → fan_speed:2 → on")
    else:
        actions = ["on", "off"]
        print(f"  Type: Generic → testing on/off")

    for action in actions:
        if DRY_RUN:
            print(f'  {C.C}DRY: POST /api/control {{"did":"{did}","action":"{action}"}}  # {name}{C.N}')
            continue

        print(f'  → {action}...', end=" ", flush=True)
        start = time.time()
        http_code, body = api("POST", "/api/control", {"did": did, "action": action}, timeout=20)
        elapsed = time.time() - start

        if http_code == 200:
            try:
                result = json.loads(body)
                status = result.get("status", "?")
                via = result.get("via", "")
                if status == "success":
                    print(f"{C.G}✅ OK ({via}) [{elapsed:.1f}s]{C.N}")
                else:
                    err = result.get("error", "")
                    print(f"{C.R}❌ device error: {err} [{elapsed:.1f}s]{C.N}")
                    if "timeout" in str(err).lower() or "deadline" in str(err).lower():
                        print(f"    {C.Y}→ UDP control timed out. Possible causes:{C.N}")
                        print(f"    {C.Y}  • Device IP ({ip}) not reachable from ha-lite server{C.N}")
                        print(f"    {C.Y}  • Device token expired — cloud sync may be needed{C.N}")
                        print(f"    {C.Y}  • Device is on a different VLAN/subnet{C.N}")
                        print(f"    {C.Y}  • Firewall blocking UDP port 54321{C.N}")
            except json.JSONDecodeError:
                print(f"{C.R}❌ unparseable response: {body[:100]} [{elapsed:.1f}s]{C.N}")
        elif http_code == 0:
            print(f"{C.R}❌ server unreachable [{elapsed:.1f}s]{C.N}")
        else:
            print(f"{C.R}❌ HTTP {http_code}: {body[:150]} [{elapsed:.1f}s]{C.N}")

        if action != actions[-1]:
            time.sleep(0.5)

# ── Step 3: Summary ─────────────────────────────────────────────────────────────
print(f"\n{C.B}═══ Done ═══{C.N}")
if DRY_RUN:
    print(f"  {C.C}Dry run — no commands sent. Run without --dry-run to execute.{C.N}")
else:
    print(f"  Check device state at {SERVER}/api/devices")