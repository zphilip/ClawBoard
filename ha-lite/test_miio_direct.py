#!/usr/bin/env python3
"""
Direct Miio device control test — bypasses ha-lite server.
Reads device tokens from cache/mi_tokens.json and sends UDP packets directly.

Usage:
    python3 test_miio_direct.py                          # test "风扇" and "筒灯"
    python3 test_miio_direct.py --name "风扇"             # test specific device
    python3 test_miio_direct.py --list                    # list all devices
    python3 test_miio_direct.py --name "风扇" --method miIO.info  # send hello

This helps isolate whether the UDP control issue is:
  a) Network/firewall (UDP blocked at OS level)
  b) Protocol (ha-lite's Go miio implementation is wrong)
  c) Token (token from cloud is invalid for local control)
"""

import argparse
import json
import os
import socket
import struct
import hashlib
import time
import random
from Crypto.Cipher import AES

CACHE_FILE = "cache/mi_tokens.json"

# ── Miio packet helpers (matching ha-lite Go implementation) ──────────────────

MAGIC = 0x2131
HELLO_DEVICE_ID = 0xFFFFFFFF
DEFAULT_PORT = 54321
READ_TIMEOUT = 5

def md5hash(data):
    return hashlib.md5(data).digest()

def pkcs7_pad(data, block_size=16):
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)

def pkcs7_unpad(data):
    pad_len = data[-1]
    return data[:-pad_len]

def aes_encrypt(key, iv, plain):
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return cipher.encrypt(pkcs7_pad(plain))

def aes_decrypt(key, iv, encrypted):
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return pkcs7_unpad(cipher.decrypt(encrypted))

def build_packet(token_hex, payload_bytes, device_id, alt=False, ff_iv=False):
    """Build a miio UDP packet. Matches ha-lite encodePacketVariant."""
    token_bytes = bytes.fromhex(token_hex)

    if alt and not ff_iv:
        # V1: key=token, iv=MD5(token)
        key = token_bytes
        iv = md5hash(token_bytes)
    elif ff_iv:
        # V3: key=MD5(token), iv=MD5(0xFF*16 + token)
        key = md5hash(token_bytes)
        iv = md5hash(b'\xff' * 16 + token_bytes)
    else:
        # V2: key=MD5(token), iv=MD5(key + token)
        key = md5hash(token_bytes)
        iv = md5hash(key + token_bytes)

    encrypted = aes_encrypt(key, iv, payload_bytes)
    stamp = int(time.time())
    header = struct.pack('>HHIIII', MAGIC, 32 + len(encrypted), 0, 0, device_id, stamp)
    # Pad header to 16 bytes (already 16)
    checksum = md5hash(header[:16] + token_bytes + encrypted)
    return header[:16] + checksum + encrypted

def decode_packet(token_hex, data, alt=False, ff_iv=False):
    """Decode a miio UDP response."""
    token_bytes = bytes.fromhex(token_hex)
    if len(data) < 32:
        return None

    header = data[:16]
    magic = struct.unpack('>H', header[0:2])[0]
    if magic != MAGIC:
        return None

    encrypted = data[32:]

    if alt and not ff_iv:
        key = token_bytes
        iv = md5hash(token_bytes)
    elif ff_iv:
        key = md5hash(token_bytes)
        iv = md5hash(b'\xff' * 16 + token_bytes)
    else:
        key = md5hash(token_bytes)
        iv = md5hash(key + token_bytes)

    try:
        plain = aes_decrypt(key, iv, encrypted)
        return json.loads(plain)
    except Exception:
        return None

def send_miio(ip, token_hex, cmd_dict, timeout=READ_TIMEOUT):
    """Send a miio command and return the response JSON. Tries all 3 variants."""
    payload = json.dumps(cmd_dict).encode()
    variants = [
        ("V3", False, True),   # key=MD5(token), iv=MD5(0xFF*16+token)
        ("V2", False, False),  # key=MD5(token), iv=MD5(key+token)
        ("V1", True, False),   # key=token, iv=MD5(token)
    ]

    for name, alt, ff_iv in variants:
        packet = build_packet(token_hex, payload, 0, alt=alt, ff_iv=ff_iv)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            sock.sendto(packet, (ip, DEFAULT_PORT))
            data, _ = sock.recvfrom(4096)
            # Try all decode variants
            for dn, da, df in variants:
                result = decode_packet(token_hex, data, alt=da, ff_iv=df)
                if result is not None:
                    return name, result
        except socket.timeout:
            continue
        except Exception as e:
            continue
        finally:
            sock.close()
    return None, None

# ── Main ──────────────────────────────────────────────────────────────────────

def load_devices(cache_file):
    if not os.path.exists(cache_file):
        print(f"ERROR: {cache_file} not found")
        return []
    with open(cache_file) as f:
        devices = json.load(f)
    return list(devices.values()) if isinstance(devices, dict) else devices

def main():
    parser = argparse.ArgumentParser(description="Direct Miio UDP test")
    parser.add_argument("--name", help="Device name substring to match")
    parser.add_argument("--list", action="store_true", help="List all devices")
    parser.add_argument("--method", default="set_properties", help="Miio method (default: set_properties)")
    parser.add_argument("--cache", default=CACHE_FILE, help="Path to mi_tokens.json")
    parser.add_argument("--timeout", type=int, default=5, help="UDP read timeout in seconds")
    args = parser.parse_args()

    devices = load_devices(args.cache)
    if not devices:
        print("No devices found in cache")
        return

    if args.list:
        print(f"{'Name':<30} {'IP':<16} {'Model':<30} {'DID':<20}")
        print("-" * 96)
        for d in sorted(devices, key=lambda x: x.get("name", "")):
            print(f"{d.get('name','?'):<30} {d.get('ip','?'):<16} {d.get('model','?'):<30} {d.get('did','?'):<20}")
        return

    # Find target device(s).
    targets = []
    for d in devices:
        if args.name:
            if args.name.lower() in d.get("name", "").lower():
                targets.append(d)
        else:
            # Default: find fan and downlight
            name = d.get("name", "")
            if "风扇" in name or "筒灯" in name:
                targets.append(d)

    if not targets:
        print(f"No devices matching '{args.name or '风扇/筒灯'}' found. Use --list to see all.")
        return

    for d in targets:
        name = d.get("name", "?")
        ip = d.get("ip", "")
        token = d.get("token", "")
        model = d.get("model", "")
        did = d.get("did", "")

        print(f"\n{'='*60}")
        print(f"Device: {name}")
        print(f"  Model: {model}  DID: {did}")
        print(f"  IP: {ip}  Token: {token[:8]}...")
        print(f"{'='*60}")

        if not ip or not token:
            print("  SKIP: missing IP or token")
            continue

        # Build command.
        if args.method == "miIO.info":
            cmd = {"id": random.randint(1, 9999), "method": "miIO.info", "params": []}
        else:
            cmd = {
                "id": random.randint(1, 9999),
                "method": "set_properties",
                "params": [{"did": f"property-2-1", "siid": 2, "piid": 1, "value": True}]
            }

        print(f"  Command: {args.method}")
        print(f"  Sending UDP to {ip}:{DEFAULT_PORT}...")

        t0 = time.time()
        variant, result = send_miio(ip, token, cmd, timeout=args.timeout)
        elapsed = time.time() - t0

        if result is not None:
            print(f"  ✅ SUCCESS [{variant}] ({elapsed:.1f}s)")
            print(f"  Response: {json.dumps(result, indent=2, ensure_ascii=False)[:500]}")
        else:
            print(f"  ❌ FAILED — all 3 encryption variants timed out ({elapsed:.1f}s)")
            print(f"  → Device at {ip}:{DEFAULT_PORT} did not respond to any variant.")
            print(f"  → Possible causes:")
            print(f"    1. UDP port 54321 blocked by firewall")
            print(f"    2. Device token is wrong for local control")
            print(f"    3. Device is behind a gateway (needs gateway IP, not device IP)")
            print(f"    4. Device uses a different miio protocol version")

if __name__ == "__main__":
    main()