#!/usr/bin/env python3
"""
Control Xiaomi devices via local miIO.
Reads IP/token from cache/mi_tokens.json. Verified working for philips.light.downlight.

Usage:
  python3 test_light.py --name "筒灯沙发上1" --on
  python3 test_light.py --name "筒灯沙发上1" --off
  python3 test_light.py --name "筒灯沙发上1" --brightness 50
  python3 test_light.py --name "筒灯沙发上1" --cct 4000
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
    """Send miIO command. Returns (ok, message)."""
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

        # Device ACK = success (32-byte response = no error)
        return True, f"OK (did=0x{did:08x})"
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

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--name", help="Device name substring")
    p.add_argument("--did", help="Device ID")
    p.add_argument("--on", action="store_true", help="Turn on")
    p.add_argument("--off", action="store_true", help="Turn off")
    p.add_argument("--brightness", "-b", type=int, metavar="1-100", help="Set brightness (1-100)")
    p.add_argument("--cct", type=int, metavar="2700-6500", help="Set color temperature (2700-6500K)")
    p.add_argument("--list", action="store_true")
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

    # ── Determine action ───────────────────────────────────────────────────
    if args.brightness is not None:
        b = max(1, min(100, args.brightness))
        method, params = "set_bright", [b]
        action = f"brightness={b}"
    elif args.cct is not None:
        c = max(2700, min(6500, args.cct))
        method, params = "set_cct", [c]
        action = f"cct={c}K"
    elif args.on:
        method, params = "set_power", ["on"]
        action = "on"
    elif args.off:
        method, params = "set_power", ["off"]
        action = "off"
    else:
        print(f"Usage: python3 test_light.py --name <name> [--on|--off|--brightness <1-100>|--cct <2700-6500>]")
        return

    print(f"{name} ({model})  IP={ip}")
    before = verify_state(ip, token)
    print(f"  Before: {before}")

    ok, msg = miio_control(ip, token, method, params)
    if ok:
        time.sleep(0.3)
        after = verify_state(ip, token)
        status = "✅" if action in after.lower() or (action == "on" and "on" in after.lower()) or (action == "off" and "off" in after.lower()) else "⚠️"
        print(f"  After:  {after}  {status} {action.upper()}")
    else:
        print(f"  ❌ {msg}")

if __name__ == "__main__":
    main()