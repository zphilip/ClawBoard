#!/usr/bin/env python3
"""
halite_control.py — Unified CLI for ha-lite Xiaomi Home device control.

Wraps ha-lite's REST API to provide friendly device control without
needing to remember DIDs, IPs, or tokens. Resolves device names to DIDs
via fuzzy matching against the device list.

Usage:
  # Discovery
  python3 halite_control.py list                          # All devices
  python3 halite_control.py list --online                  # Only online
  python3 halite_control.py list --category lights         # Only lights

  # Power control (resolves name → DID automatically)
  python3 halite_control.py on "Living Room Light"
  python3 halite_control.py off "热水器"
  python3 halite_control.py toggle "Bedroom Fan"

  # Light control
  python3 halite_control.py brightness "Living Room Light" 75
  python3 halite_control.py color_temp "Desk Lamp" 4000

  # Status
  python3 halite_control.py status "热水器"
  python3 halite_control.py status --all

  # Server
  python3 halite_control.py health
  python3 halite_control.py sync

  # Login
  python3 halite_control.py login qr
  python3 halite_control.py login oauth

Environment:
  HALITE_URL  — ha-lite server URL (default: http://localhost:8090)
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Optional

# ── Configuration ──────────────────────────────────────────────────────────────

HALITE_URL = os.environ.get("HALITE_URL", "http://localhost:8090")


def _api(method: str, path: str, body: Optional[dict] = None, timeout: int = 15) -> dict:
    """Call ha-lite REST API and return parsed JSON."""
    url = f"{HALITE_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        return {"error": f"Cannot reach ha-lite at {HALITE_URL}: {e}"}
    except json.JSONDecodeError:
        return {"error": "Invalid JSON response from ha-lite"}


# ── Device matching ────────────────────────────────────────────────────────────

def _get_devices() -> list[dict]:
    """Fetch all devices from ha-lite."""
    data = _api("GET", "/api/devices")
    return data.get("devices", [])


def _find_device(name_or_did: str) -> Optional[dict]:
    """Find a device by DID or fuzzy name match. Returns the device dict or None."""
    devices = _get_devices()
    if not devices:
        return None

    # Exact DID match first.
    for d in devices:
        if d.get("did") == name_or_did:
            return d

    # Exact name match.
    name_lower = name_or_did.lower().strip()
    for d in devices:
        if d.get("name", "").lower().strip() == name_lower:
            return d

    # Substring match.
    candidates = []
    for d in devices:
        dname = d.get("name", "").lower()
        if name_lower in dname:
            candidates.append(d)

    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        print(f"⚠️  Multiple devices match '{name_or_did}':")
        for d in candidates:
            print(f"   • {d['name']} ({d['did']})")
        print("   Use the full name or DID to disambiguate.")
        return None

    return None


# ── Category helpers ───────────────────────────────────────────────────────────

_CATEGORY_META = {
    "lights":    {"icon": "💡", "label": "Lights"},
    "vacuum":    {"icon": "🧹", "label": "Vacuums"},
    "fan":       {"icon": "🌀", "label": "Fans"},
    "sensor":    {"icon": "📡", "label": "Sensors"},
    "air":       {"icon": "🌬️", "label": "Air Purifiers"},
    "switch":    {"icon": "🔌", "label": "Switches & Plugs"},
    "camera":    {"icon": "📷", "label": "Cameras"},
    "curtain":   {"icon": "🪟", "label": "Curtains"},
    "lock":      {"icon": "🔐", "label": "Locks"},
    "gateway":   {"icon": "🌐", "label": "Gateways"},
    "speaker":   {"icon": "🔊", "label": "Speakers"},
    "appliance": {"icon": "🏠", "label": "Appliances"},
    "other":     {"icon": "📦", "label": "Other"},
}


def _device_category(model: str) -> str:
    """Auto-categorize a Xiaomi device by model name."""
    m = model.lower()
    if any(kw in m for kw in ("light", "lamp", "bulb", "candle", "downlight", "ceiling", "led")):
        return "lights"
    if any(kw in m for kw in ("vacuum", "clean", "sweep", "dust")):
        return "vacuum"
    if any(kw in m for kw in ("fan", "airer", "dryer")):
        return "fan"
    if any(kw in m for kw in ("sensor", "motion", "contact", "flood", "temp", "humid", "weather", "smoke", "gas", "magnet")):
        return "sensor"
    if any(kw in m for kw in ("purifier", "filter")):
        return "air"
    if any(kw in m for kw in ("plug", "outlet", "switch", "relay", "strip", "power", "socket")):
        return "switch"
    if any(kw in m for kw in ("camera", "doorbell", "monitor", "cam", "isp")):
        return "camera"
    if any(kw in m for kw in ("curtain", "blind", "window", "shade", "roller")):
        return "curtain"
    if any(kw in m for kw in ("lock", "door", "deadbolt")):
        return "lock"
    if any(kw in m for kw in ("gateway", "hub", "bridge", "repeater")):
        return "gateway"
    if any(kw in m for kw in ("speaker", "box", "audio", "sound", "alarm", "story")):
        return "speaker"
    if any(kw in m for kw in ("kettle", "cooker", "rice", "oven", "microwave", "fridge", "washer", "heater", "water", "toothbrush", "scale", "watch", "band", "humidifier", "diffuser")):
        return "appliance"
    return "other"


# ── Commands ───────────────────────────────────────────────────────────────────

def cmd_list(args):
    """List devices with optional filtering."""
    devices = _get_devices()

    if not devices:
        print("No devices found. Use 'login qr' or 'login oauth' to sync from cloud.")
        return

    # Filters.
    if args.online:
        devices = [d for d in devices if d.get("online")]
    if args.offline:
        devices = [d for d in devices if not d.get("online")]
    if args.category:
        devices = [d for d in devices if _device_category(d.get("model", "")) == args.category]
    if args.name:
        name_filter = args.name.lower()
        devices = [d for d in devices if name_filter in d.get("name", "").lower()]

    if not devices:
        print("No devices match the filter.")
        return

    # Sort: online first, then by category, then by name.
    devices.sort(key=lambda d: (
        not d.get("online", False),
        _device_category(d.get("model", "")),
        d.get("name", ""),
    ))

    for d in devices:
        online = d.get("online", False)
        status_icon = "🟢" if online else "🔴"
        name = d.get("name", "?")
        model = d.get("model", "?")
        ip = d.get("ip", "?")
        did = d.get("did", "?")
        cat = _device_category(model)
        cat_meta = _CATEGORY_META.get(cat, _CATEGORY_META["other"])
        print(f"{status_icon} {cat_meta['icon']} {name}")
        print(f"   Model: {model}  IP: {ip}  DID: {did}")


def cmd_list_categories(args):
    """List available categories with device counts."""
    devices = _get_devices()
    counts = {}
    for d in devices:
        cat = _device_category(d.get("model", ""))
        counts[cat] = counts.get(cat, 0) + 1

    for cat_key in ["lights", "switch", "fan", "air", "vacuum", "curtain",
                    "camera", "sensor", "lock", "gateway", "speaker",
                    "appliance", "other"]:
        if cat_key in counts:
            meta = _CATEGORY_META.get(cat_key, _CATEGORY_META["other"])
            print(f"  {meta['icon']} {meta['label']}: {counts[cat_key]}")


def cmd_control(args):
    """Send a control command to a device."""
    dev = _find_device(args.device)
    if not dev:
        print(f"❌ Device not found: {args.device}")
        sys.exit(1)

    action = args.action
    if args.value is not None:
        action = f"{action}:{args.value}"

    print(f"🎮 {action} → {dev['name']} ({dev['did']})")
    result = _api("POST", "/api/control", {"did": dev["did"], "action": action})

    if result.get("status") == "success":
        via = result.get("via", "local")
        print(f"✅ OK (via {via})")
    else:
        error = result.get("error", "Unknown error")
        print(f"❌ Failed: {error}")
        sys.exit(1)


def cmd_status(args):
    """Query device status."""
    if args.all:
        devices = _get_devices()
        if not devices:
            print("No devices found.")
            return
        online = [d for d in devices if d.get("online")]
        offline = [d for d in devices if not d.get("online")]
        print(f"🟢 Online: {len(online)}  🔴 Offline: {len(offline)}  Total: {len(devices)}")
        return

    dev = _find_device(args.device)
    if not dev:
        print(f"❌ Device not found: {args.device}")
        sys.exit(1)

    online = dev.get("online", False)
    print(f"{'🟢' if online else '🔴'} {dev['name']}")
    print(f"   Model: {dev.get('model', '?')}")
    print(f"   IP: {dev.get('ip', '?')}")
    print(f"   DID: {dev.get('did', '?')}")
    print(f"   Online: {online}")

    # Query device state if online.
    if online:
        result = _api("POST", "/api/control", {"did": dev["did"], "action": "status"})
        if result.get("status") == "success":
            resp = result.get("response", [])
            if isinstance(resp, list):
                labels = ["Power", "Brightness", "Color Temp"]
                for i, val in enumerate(resp):
                    if i < len(labels):
                        print(f"   {labels[i]}: {val}")
                    else:
                        print(f"   Prop[{i}]: {val}")
        else:
            print(f"   ⚠️  Could not query state: {result.get('error', '?')}")


def cmd_health(args):
    """Check ha-lite server health."""
    data = _api("GET", "/api/health")
    if data.get("error"):
        print(f"❌ ha-lite unreachable: {data['error']}")
        sys.exit(1)

    status = data.get("status", "?")
    version = data.get("version", "?")
    cloud = data.get("cloud_authed", False)
    oauth = data.get("oauth_authed", False)
    dev_count = data.get("device_count", 0)

    print(f"🏠 ha-lite {version}")
    print(f"   Status: {'🟢' if status == 'ok' else '🔴'} {status}")
    print(f"   Cloud (password/QR): {'✅' if cloud else '❌'}")
    print(f"   OAuth: {'✅' if oauth else '❌'}")
    print(f"   Devices: {dev_count}")


def cmd_sync(args):
    """Force cloud sync to refresh device tokens and IPs."""
    print("🔄 Syncing with Xiaomi Cloud...")
    result = _api("POST", "/api/sync")
    if result.get("error"):
        print(f"❌ Sync failed: {result['error']}")
        sys.exit(1)
    print(f"✅ Sync complete. Status: {result.get('status', 'ok')}")


def cmd_login(args):
    """Start QR or OAuth login flow."""
    if args.method == "qr":
        print("📱 Starting QR code login...")
        result = _api("POST", "/api/login/qr/start")
        if result.get("error"):
            print(f"❌ QR login failed: {result['error']}")
            sys.exit(1)
        qr_url = result.get("qr_url", "")
        if qr_url:
            print(f"🔗 Open this URL to scan QR code: {qr_url}")
        print("📱 Scan the QR code with the Mi Home app.")
        print("⏳ Waiting for scan...")

        # Poll for completion.
        for _ in range(40):  # ~2 minutes.
            time.sleep(3)
            status = _api("GET", "/api/login/qr/status")
            qr_status = status.get("status", "")
            if qr_status == "authenticated":
                # Collect the service token.
                collect = _api("POST", "/api/login/qr/collect")
                if collect.get("status") == "ok":
                    print("✅ Login successful! Syncing devices...")
                    cmd_sync(args)
                    return
                else:
                    print(f"⚠️  Auth OK but collect failed: {collect.get('error', '?')}")
                    return
            elif qr_status == "scanned":
                print("   📱 QR code scanned, waiting for confirmation...")
            elif qr_status in ("timeout", "error", "cancelled"):
                print(f"❌ Login failed: {qr_status} - {status.get('message', '')}")
                sys.exit(1)
        print("❌ Login timed out (2 minutes).")
        sys.exit(1)

    elif args.method == "oauth":
        print("🌐 Starting OAuth login...")
        result = _api("POST", "/api/login/oauth/start")
        if result.get("error"):
            print(f"❌ OAuth login failed: {result['error']}")
            sys.exit(1)
        auth_url = result.get("auth_url", "")
        if auth_url:
            print(f"🔗 Open this URL in your browser: {auth_url}")
        print("⏳ Waiting for authorization...")

        # Poll for completion.
        for _ in range(40):  # ~2 minutes.
            time.sleep(3)
            status = _api("GET", "/api/login/oauth/status")
            oauth_status = status.get("status", "")
            if oauth_status == "authorized":
                print("   ✅ Authorization received, exchanging code for token...")
                collect = _api("POST", "/api/login/oauth/collect")
                if collect.get("status") == "ok":
                    print("✅ OAuth login successful! Syncing devices...")
                    cmd_sync(args)
                    return
                else:
                    print(f"⚠️  Exchange failed: {collect.get('error', '?')}")
                    return
            elif oauth_status == "authenticated":
                print("✅ Already authenticated!")
                return
            elif oauth_status in ("error", "expired"):
                print(f"❌ Login failed: {oauth_status} - {status.get('message', '')}")
                sys.exit(1)
        print("❌ Login timed out (2 minutes).")
        sys.exit(1)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="ha-lite Xiaomi Home device control CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s list                          List all devices
  %(prog)s list --online                 List online devices only
  %(prog)s list --category lights        List lights only
  %(prog)s on "Living Room Light"        Turn on a device
  %(prog)s off "热水器"                   Turn off a device
  %(prog)s toggle "Bedroom Fan"         Toggle device power
  %(prog)s brightness "Desk Lamp" 75    Set brightness to 75%%
  %(prog)s color_temp "Desk Lamp" 4000  Set color temperature to 4000K
  %(prog)s status "热水器"               Query device status
  %(prog)s status --all                 Show online/offline summary
  %(prog)s health                       Check server health
  %(prog)s sync                         Force cloud sync
  %(prog)s login qr                     Start QR code login
  %(prog)s login oauth                  Start OAuth login
        """,
    )
    sub = parser.add_subparsers(dest="command", help="Command")

    # ── list ──────────────────────────────────────────────────────────────────
    p_list = sub.add_parser("list", help="List devices")
    p_list.add_argument("--online", action="store_true", help="Online devices only")
    p_list.add_argument("--offline", action="store_true", help="Offline devices only")
    p_list.add_argument("--category", "-c", help="Filter by category (lights, switch, fan, etc.)")
    p_list.add_argument("--name", "-n", help="Filter by name substring")

    # ── categories ────────────────────────────────────────────────────────────
    sub.add_parser("categories", help="List categories with device counts")

    # ── on / off / toggle ─────────────────────────────────────────────────────
    p_on = sub.add_parser("on", help="Turn device on")
    p_on.add_argument("device", help="Device name or DID")

    p_off = sub.add_parser("off", help="Turn device off")
    p_off.add_argument("device", help="Device name or DID")

    p_toggle = sub.add_parser("toggle", help="Toggle device power")
    p_toggle.add_argument("device", help="Device name or DID")

    # ── brightness ────────────────────────────────────────────────────────────
    p_bright = sub.add_parser("brightness", help="Set device brightness (0-100)")
    p_bright.add_argument("device", help="Device name or DID")
    p_bright.add_argument("value", type=int, help="Brightness level (0-100)")

    # ── color_temp ────────────────────────────────────────────────────────────
    p_ct = sub.add_parser("color_temp", help="Set color temperature (2700-6500K)")
    p_ct.add_argument("device", help="Device name or DID")
    p_ct.add_argument("value", type=int, help="Color temperature in Kelvin (2700-6500)")

    # ── status ────────────────────────────────────────────────────────────────
    p_status = sub.add_parser("status", help="Query device status")
    p_status.add_argument("device", nargs="?", help="Device name or DID")
    p_status.add_argument("--all", action="store_true", help="Show online/offline summary")

    # ── health ────────────────────────────────────────────────────────────────
    sub.add_parser("health", help="Check ha-lite server health")

    # ── sync ──────────────────────────────────────────────────────────────────
    sub.add_parser("sync", help="Force cloud sync to refresh device tokens")

    # ── login ─────────────────────────────────────────────────────────────────
    p_login = sub.add_parser("login", help="Login to Xiaomi cloud")
    p_login.add_argument("method", choices=["qr", "oauth"], help="Login method")

    args = parser.parse_args()

    if args.command == "list":
        cmd_list(args)
    elif args.command == "categories":
        cmd_list_categories(args)
    elif args.command in ("on", "off", "toggle"):
        cmd_control(args)
    elif args.command in ("brightness", "color_temp"):
        cmd_control(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "health":
        cmd_health(args)
    elif args.command == "sync":
        cmd_sync(args)
    elif args.command == "login":
        cmd_login(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()