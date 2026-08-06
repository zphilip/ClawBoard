#!/usr/bin/env python3
"""
Control Xiaomi devices via local miIO.
Reads IP/token from cache/mi_tokens.json. Verified working for philips.light.downlight.

Usage:
  python3 test_light.py --name "筒灯沙发上1" --on
  python3 test_light.py --name "筒灯沙发上1" --off
  python3 test_light.py --name "筒灯沙发上1" --brightness 50
  python3 test_light.py --name "筒灯沙发上1" --cct 4000
  python3 test_light.py --name "筒灯沙发上1" --status
  python3 test_light.py --name "风扇" --on
  python3 test_light.py --list
"""

import argparse, hashlib, json, socket, struct, sys, time
from Crypto.Cipher import AES

CACHE = "cache/mi_tokens.json"
RAW_HELLO = bytes.fromhex("21310020" + "ff" * 28)

def load_devices():
    with open(CACHE) as f:
        devs = json.load(f)
    return list(devs.values()) if isinstance(devs, dict) else devs

def find_device(name):
    for d in load_devices():
        if name in d.get("name", ""):
            return d
    return None

def miio_control(ip, token_hex, method, params, timeout=5):
    """Send miIO command. Returns (ok, data_or_message)."""
    if not ip or ip == "0.0.0.0":
        return False, "No IP (Zigbee/BLE device)"

    tb = bytes.fromhex(token_hex)
    key = hashlib.md5(tb).digest()
    iv = hashlib.md5(key + tb).digest()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("0.0.0.0", 0))
    sock.settimeout(timeout)

    try:
        # Step 1: RAW hello ×3
        for _ in range(3):
            sock.sendto(RAW_HELLO, (ip, 54321))
        data, _ = sock.recvfrom(4096)
        did = struct.unpack('>I', data[8:12])[0]
        ts = struct.unpack('>I', data[12:16])[0] + 1

        # Step 2: Encrypted command (header: HHIII = 16 bytes)
        cmd = json.dumps({"id": 1, "method": method, "params": params}, separators=(",", ":")).encode()
        pad = 16 - len(cmd) % 16
        enc = AES.new(key, AES.MODE_CBC, iv).encrypt(cmd + bytes([pad] * pad))
        hdr = struct.pack('>HHIII', 0x2131, 32 + len(enc), 0, did, ts)
        csum = hashlib.md5(hdr + tb + enc).digest()

        sock.sendto(hdr + csum + enc, (ip, 54321))
        data, _ = sock.recvfrom(4096)

        # Decrypt response
        resp = None
        if len(data) > 32:
            try:
                p = AES.new(key, AES.MODE_CBC, iv).decrypt(data[32:])
                p = p[:-p[-1]]  # remove PKCS7 padding
                resp = json.loads(p)
            except:
                pass

        return True, resp
    except socket.timeout:
        return False, "Timeout"
    except Exception as e:
        return False, str(e)
    finally:
        sock.close()

def verify_state(ip, token):
    """Use python-miio to check device power state."""
    try:
        from miio import Device
        dev = Device(ip=ip, token=token)
        return str(dev.send("get_prop", ["power"]))
    except:
        return "?"

def get_props(ip, token):
    """Read device properties (power, brightness, color temp)."""
    ok, resp = miio_control(ip, token, "get_prop", ["power", "bright", "cct"])
    if ok and resp and "result" in resp:
        return resp["result"]
    return None

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--name", help="Device name substring")
    p.add_argument("--did", help="Device ID")
    p.add_argument("--on", action="store_true", help="Turn on")
    p.add_argument("--off", action="store_true", help="Turn off")
    p.add_argument("--toggle", action="store_true", help="Toggle power")
    p.add_argument("--brightness", "-b", type=int, metavar="1-100", help="Set brightness (1-100)")
    p.add_argument("--cct", type=int, metavar="2700-6500", help="Set color temperature (2700-6500K)")
    p.add_argument("--status", action="store_true", help="Read device properties (power, brightness, cct)")
    p.add_argument("--list", action="store_true", help="List all devices")
    p.add_argument("--raw", help="Raw method:params (e.g. 'set_bright:50')")
    args = p.parse_args()

    if args.list:
        print(f"{'Name':<30} {'IP':<16} {'Model':<30} {'DID':<15}")
        print("-" * 90)
        for d in sorted(load_devices(), key=lambda x: x.get("name", "")):
            print(f"{d.get('name','?'):<30} {d.get('ip','?'):<16} {d.get('model','?'):<30} {d.get('did','?'):<15}")
        return

    dev = find_device(args.name) if args.name else None
    if args.did:
        for d in load_devices():
            if d.get("did") == args.did:
                dev = d; break
    if not dev:
        print(f"Not found: {args.name or args.did}"); return

    ip, token, name, model = dev["ip"], dev["token"], dev["name"], dev["model"]

    print(f"{name} ({model})  IP={ip}")

    # ── Status / get_prop ──────────────────────────────────────────────────
    if args.status:
        props = get_props(ip, token)
        if props:
            labels = ["power", "brightness", "color_temp"]
            for i, val in enumerate(props):
                label = labels[i] if i < len(labels) else f"prop{i}"
                print(f"  {label}: {val}")
        else:
            print("  ⚠️  Could not read properties")
        return

    # ── Determine action ───────────────────────────────────────────────────
    if args.raw:
        parts = args.raw.split(":", 1)
        method = parts[0]
        params = [parts[1]] if len(parts) > 1 else []
    elif args.brightness is not None:
        b = max(1, min(100, args.brightness))
        method, params = "set_bright", [b]
        action_desc = f"brightness={b}"
    elif args.cct is not None:
        c = max(2700, min(6500, args.cct))
        method, params = "set_cct", [c]
        action_desc = f"cct={c}K"
    elif args.toggle:
        # Read current state, then toggle
        props = get_props(ip, token)
        current = "off"
        if props and len(props) > 0:
            current = str(props[0]).lower()
        action = "off" if current == "on" else "on"
        method, params = "set_power", [action]
        action_desc = f"toggle → {action}"
    elif args.on:
        method, params = "set_power", ["on"]
        action_desc = "on"
    elif args.off:
        method, params = "set_power", ["off"]
        action_desc = "off"
    else:
        # Default: show status
        props = get_props(ip, token)
        if props:
            labels = ["power", "brightness", "color_temp"]
            for i, val in enumerate(props):
                label = labels[i] if i < len(labels) else f"prop{i}"
                print(f"  {label}: {val}")
        else:
            print("  No action specified. Use --on, --off, --brightness, --cct, --status, or --toggle")
        return

    # ── Execute ────────────────────────────────────────────────────────────
    before = get_props(ip, token)
    if before:
        print(f"  Before: power={before[0] if len(before)>0 else '?'}, bright={before[1] if len(before)>1 else '?'}, cct={before[2] if len(before)>2 else '?'}")

    ok, resp = miio_control(ip, token, method, params)
    if ok:
        time.sleep(0.3)
        after = get_props(ip, token)
        if after:
            print(f"  After:  power={after[0] if len(after)>0 else '?'}, bright={after[1] if len(after)>1 else '?'}, cct={after[2] if len(after)>2 else '?'}")
        if resp:
            print(f"  Response: {json.dumps(resp)}")
        print(f"  ✅ {action_desc.upper()} OK")
    else:
        print(f"  ❌ {resp}")

if __name__ == "__main__":
    main()