#!/usr/bin/env python3
"""Debug: compare raw socket vs python-miio for the same device."""
import hashlib, json, socket, struct, sys
from Crypto.Cipher import AES

CACHE = "cache/mi_tokens.json"
RAW_HELLO = bytes.fromhex("21310020" + "ff" * 28)

name = sys.argv[1] if len(sys.argv) > 1 else "风扇"
action = sys.argv[2] if len(sys.argv) > 2 else "on"

with open(CACHE) as f:
    d = [v for v in json.load(f).values() if name in v.get("name", "")][0]
ip, token = d["ip"], d["token"]
print(f"Device: {d['name']} ({d['model']}) IP={ip} token={token[:8]}...")

# ── Method A: python-miio (known working) ──────────────────────────────
print("\n=== Method A: python-miio ===")
from miio import Device
dev = Device(ip=ip, token=token)
before = dev.send("get_prop", ["power"])
print(f"  Before: {before}")
result = dev.send("set_power", [action])
print(f"  Result: {result}")
after = dev.send("get_prop", ["power"])
print(f"  After: {after}")

# ── Method B: raw socket (same as test_light.py) ──────────────────────
print("\n=== Method B: raw socket ===")
tb = bytes.fromhex(token)
key = hashlib.md5(tb).digest()
iv = hashlib.md5(key + tb).digest()

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
sock.bind(("0.0.0.0", 0))
sock.settimeout(5)

try:
    # Step 1: RAW hello ×3
    for i in range(3):
        sock.sendto(RAW_HELLO, (ip, 54321))
        print(f"  Sent hello #{i+1}")
    data, addr = sock.recvfrom(4096)
    print(f"  Hello response: {len(data)} bytes from {addr}")
    print(f"  Hello hex: {data[:32].hex()}")

    if len(data) < 16:
        print(f"  ❌ Response too short: {len(data)}")
        sys.exit(1)

    did = struct.unpack('>I', data[8:12])[0]
    ts = struct.unpack('>I', data[12:16])[0] + 1
    print(f"  device_id=0x{did:08x} ts={ts}")

    # Step 2: Encrypted command
    cmd = json.dumps({"id": 1, "method": "set_power", "params": [action]}, separators=(",", ":")).encode()
    print(f"  Command: {cmd}")

    pad = 16 - len(cmd) % 16
    enc = AES.new(key, AES.MODE_CBC, iv).encrypt(cmd + bytes([pad] * pad))
    hdr = struct.pack('>HHIII', 0x2131, 32 + len(enc), 0, did, ts)
    csum = hashlib.md5(hdr + tb + enc).digest()

    print(f"  Header ({len(hdr)}B): {hdr.hex()}")
    print(f"  Checksum: {csum.hex()}")
    print(f"  Encrypted payload: {len(enc)}B")

    pkt = hdr + csum + enc
    print(f"  Full packet: {len(pkt)}B")
    sock.sendto(pkt, (ip, 54321))

    data, _ = sock.recvfrom(4096)
    print(f"  Response: {len(data)} bytes")
    print(f"  Response hex: {data[:64].hex()}")
    print(f"  ✅ Raw socket OK")

except socket.timeout:
    print(f"  ❌ Timeout at some step")
except Exception as e:
    print(f"  ❌ Error: {e}")
finally:
    sock.close()

# ── Method C: raw socket WITHOUT verify_state before ──────────────────
print("\n=== Method C: raw socket (no verify_state before) ===")
sock2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock2.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
sock2.bind(("0.0.0.0", 0))
sock2.settimeout(5)

try:
    for i in range(3):
        sock2.sendto(RAW_HELLO, (ip, 54321))
    data, _ = sock2.recvfrom(4096)
    did = struct.unpack('>I', data[8:12])[0]
    ts = struct.unpack('>I', data[12:16])[0] + 1
    print(f"  device_id=0x{did:08x} ts={ts}")

    cmd = json.dumps({"id": 1, "method": "set_power", "params": [action]}, separators=(",", ":")).encode()
    pad = 16 - len(cmd) % 16
    enc = AES.new(key, AES.MODE_CBC, iv).encrypt(cmd + bytes([pad] * pad))
    hdr = struct.pack('>HHIII', 0x2131, 32 + len(enc), 0, did, ts)
    csum = hashlib.md5(hdr + tb + enc).digest()
    sock2.sendto(hdr + csum + enc, (ip, 54321))
    data, _ = sock2.recvfrom(4096)
    print(f"  ✅ Raw socket (no verify_state) OK")
except socket.timeout:
    print(f"  ❌ Timeout")
except Exception as e:
    print(f"  ❌ Error: {e}")
finally:
    sock2.close()

print("\nDone.")