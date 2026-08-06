#!/usr/bin/env python3
"""Minimal raw socket test — mirrors test_light_final.py approach exactly."""
import hashlib, json, socket, struct, sys
from Crypto.Cipher import AES

CACHE = "cache/mi_tokens.json"
RAW_HELLO = bytes.fromhex("21310020" + "ff" * 28)

name = sys.argv[1] if len(sys.argv) > 1 else "风扇"
action = sys.argv[2] if len(sys.argv) > 2 else "on"

with open(CACHE) as f:
    d = [v for v in json.load(f).values() if name in v.get("name", "")][0]
ip, token = d["ip"], d["token"]
print(f"Device: {d['name']} ({d['model']}) IP={ip}")

tb = bytes.fromhex(token)
key = hashlib.md5(tb).digest()
iv = hashlib.md5(key + tb).digest()

# ── Raw socket control (exact same logic as test_light_final.py) ─────
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
sock.bind(("0.0.0.0", 0))
sock.settimeout(5)

try:
    # Step 1: RAW hello ×3
    for _ in range(3):
        sock.sendto(RAW_HELLO, (ip, 54321))
    data, _ = sock.recvfrom(4096)
    did = struct.unpack('>I', data[8:12])[0]
    ts = struct.unpack('>I', data[12:16])[0] + 1
    print(f"  Handshake OK: did=0x{did:08x} ts={ts}")

    # Step 2: Encrypted command
    cmd = json.dumps({"id": 1, "method": "set_power", "params": [action]}, separators=(",", ":")).encode()
    pad = 16 - len(cmd) % 16
    enc = AES.new(key, AES.MODE_CBC, iv).encrypt(cmd + bytes([pad] * pad))
    hdr = struct.pack('>HHIII', 0x2131, 32 + len(enc), 0, did, ts)
    csum = hashlib.md5(hdr + tb + enc).digest()
    sock.sendto(hdr + csum + enc, (ip, 54321))
    data, _ = sock.recvfrom(4096)
    print(f"  ✅ {action.upper()} OK (response: {len(data)}B)")

except socket.timeout:
    print(f"  ❌ Timeout")
except Exception as e:
    print(f"  ❌ Error: {e}")
finally:
    sock.close()