#!/usr/bin/env python3
"""
Control Xiaomi devices locally via miIO (RAW hello + encrypted command).
Reads IP and token from cache/mi_tokens.json.

Usage:
  python3 test_light.py --name "筒灯沙发上1" --on
  python3 test_light.py --name "筒灯沙发上1" --off
  python3 test_light.py --name "风扇" --on
  python3 test_light.py --list                          # list all devices
"""

import argparse, hashlib, json, socket, struct, time
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

def miio_encrypt(token_hex, plain_bytes):
    tb = bytes.fromhex(token_hex)
    key = hashlib.md5(tb).digest()
    iv = hashlib.md5(key + tb).digest()
    pad = 16 - len(plain_bytes) % 16
    padded = plain_bytes + bytes([pad] * pad)
    return AES.new(key, AES.MODE_CBC, iv).encrypt(padded)

def miio_send(ip, token_hex, method, params, timeout=5):
    """Send miIO command with RAW hello handshake. Returns (ok, message)."""
    # Create socket (matching python-miio + Go v0.6.3)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("0.0.0.0", 0))
    sock.settimeout(timeout)

    try:
        # Step 1: RAW hello ×3
        for _ in range(3):
            sock.sendto(RAW_HELLO, (ip, 54321))
        data, _ = sock.recvfrom(4096)
        device_id = struct.unpack('>I', data[8:12])[0]
        dev_ts = struct.unpack('>I', data[12:16])[0] + 1

        # Step 2: Encrypted command
        cmd = json.dumps({"id": 1, "method": method, "params": params}, separators=(",", ":")).encode()
        enc = miio_encrypt(token_hex, cmd)
        tb = bytes.fromhex(token_hex)
        key = hashlib.md5(tb).digest()
        header = struct.pack('>HHIII', 0x2131, 32 + len(enc), 0, device_id, dev_ts)
        csum = hashlib.md5(header[:16] + tb + enc).digest()
        pkt = header[:16] + csum + enc
        sock.sendto(pkt, (ip, 54321))
        data, _ = sock.recvfrom(4096)

        return True, f"OK (device_id=0x{device_id:08x})"
    except socket.timeout:
        return False, "Timeout — device not responding"
    except Exception as e:
        return False, str(e)
    finally:
        sock.close()

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--name", help="Device name substring")
    p.add_argument("--did", help="Device ID (exact)")
    p.add_argument("--on", action="store_true")
    p.add_argument("--off", action="store_true")
    p.add_argument("--list", action="store_true")
    args = p.parse_args()

    if args.list:
        print(f"{'Name':<30} {'IP':<16} {'Model':<30} {'DID':<15}")
        print("-" * 90)
        for d in sorted(load_devices(), key=lambda x: x.get("name", "")):
            print(f"{d.get('name','?'):<30} {d.get('ip','?'):<16} {d.get('model','?'):<30} {d.get('did','?'):<15}")
        return

    if not args.name and not args.did:
        p.print_help()
        return

    dev = find_device(args.name) if args.name else None
    if args.did:
        for d in load_devices():
            if d.get("did") == args.did:
                dev = d
                break

    if not dev:
        print(f"Device not found: {args.name or args.did}")
        return

    ip = dev.get("ip", "")
    token = dev.get("token", "")

    if not ip or ip == "0.0.0.0":
        print(f"Device '{dev['name']}' has no IP — cannot control locally (try OAuth cloud control)")
        return

    action = "on" if args.on else "off"
    print(f"Device: {dev['name']} ({dev['model']})")
    print(f"IP: {ip}  DID: {dev['did']}")
    print(f"Sending set_power(['{action}'])...")

    ok, msg = miio_send(ip, token, "set_power", [action])
    if ok:
        print(f"✅ {action.upper()} success — {msg}")
    else:
        print(f"❌ Failed: {msg}")

if __name__ == "__main__":
    main()