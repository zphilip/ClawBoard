#!/usr/bin/env python3
"""Compare python-miio packets vs raw socket packets to find the exact difference."""
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

# ── Capture python-miio's packets ────────────────────────────────────
pkts = []
orig_sendto = socket.socket.sendto
def hook(self, data, addr):
    pkts.append(data)
    return orig_sendto(self, data, addr)
socket.socket.sendto = hook

from miio import Device
dev = Device(ip=ip, token=token)
dev.info()  # handshake
pkts.clear()
result = dev.send("set_power", [action])
socket.socket.sendto = orig_sendto

print(f"\npython-miio result: {result}")
for i, p in enumerate(pkts):
    print(f"\n  PKT {i}: {len(p)}B")
    if len(p) >= 32:
        print(f"    Header (32B): {p[:32].hex()}")
        hdr = p[:16]
        magic = struct.unpack('>H', hdr[0:2])[0]
        length = struct.unpack('>H', hdr[2:4])[0]
        unknown = struct.unpack('>I', hdr[4:8])[0]
        did = struct.unpack('>I', hdr[8:12])[0]
        ts = struct.unpack('>I', hdr[12:16])[0]
        print(f"    magic=0x{magic:04x} length={length} unknown=0x{unknown:08x} did=0x{did:08x} ts={ts}")
        # Decrypt
        try:
            ptext = AES.new(key, AES.MODE_CBC, iv).decrypt(p[32:])
            pad = ptext[-1]
            if 1 <= pad <= 16:
                ptext = ptext[:-pad]
            print(f"    Decrypted: {json.loads(ptext)}")
        except Exception as e:
            print(f"    Decrypt failed: {e}")

# ── Now our raw socket version with same params ──────────────────────
print(f"\n=== Our raw socket version ===")
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
sock.bind(("0.0.0.0", 0))
sock.settimeout(5)

try:
    for i in range(3):
        sock.sendto(RAW_HELLO, (ip, 54321))
    data, _ = sock.recvfrom(4096)
    print(f"  Hello response: {len(data)}B")
    our_did = struct.unpack('>I', data[8:12])[0]
    our_ts = struct.unpack('>I', data[12:16])[0] + 1
    print(f"  did=0x{our_did:08x} ts={our_ts}")

    cmd = json.dumps({"id": 1, "method": "set_power", "params": [action]}, separators=(",", ":")).encode()
    print(f"  cmd: {cmd}")

    pad = 16 - len(cmd) % 16
    enc = AES.new(key, AES.MODE_CBC, iv).encrypt(cmd + bytes([pad] * pad))
    hdr = struct.pack('>HHIII', 0x2131, 32 + len(enc), 0, our_did, our_ts)
    csum = hashlib.md5(hdr + tb + enc).digest()
    our_pkt = hdr + csum + enc

    print(f"  Our packet: {len(our_pkt)}B")
    print(f"  Header: {hdr.hex()}")
    print(f"  Checksum: {csum.hex()}")

    # Compare with python-miio packet
    if pkts:
        pmiio = pkts[0]
        print(f"\n  COMPARISON:")
        print(f"    python-miio packet: {len(pmiio)}B")
        print(f"    Our packet:         {len(our_pkt)}B")
        print(f"    python-miio header: {pmiio[:16].hex()}")
        print(f"    Our header:         {hdr.hex()}")
        print(f"    Headers match: {pmiio[:16] == hdr}")
        if len(pmiio) >= 32 and len(our_pkt) >= 32:
            print(f"    python-miio checksum: {pmiio[16:32].hex()}")
            print(f"    Our checksum:         {csum.hex()}")
            print(f"    Checksums match: {pmiio[16:32] == csum}")
            print(f"    python-miio payload (first 32): {pmiio[32:64].hex()}")
            print(f"    Our payload (first 32):         {our_pkt[32:64].hex()}")
            print(f"    Payloads match: {pmiio[32:] == our_pkt[32:]}")

    sock.sendto(our_pkt, (ip, 54321))
    data, _ = sock.recvfrom(4096)
    print(f"\n  Response: {len(data)}B")
    if len(data) > 32:
        try:
            ptext = AES.new(key, AES.MODE_CBC, iv).decrypt(data[32:])
            pad = ptext[-1]
            if 1 <= pad <= 16:
                ptext = ptext[:-pad]
            print(f"  Decrypted: {json.loads(ptext)}")
        except Exception as e:
            print(f"  Decrypt failed: {e}")
    print(f"  ✅ Raw socket OK")

except socket.timeout:
    print(f"  ❌ Timeout")
except Exception as e:
    print(f"  ❌ Error: {e}")
finally:
    sock.close()