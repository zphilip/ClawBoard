#!/usr/bin/env python3
"""Compare our miIO packet vs python-miio's to find the exact difference."""
import json, socket, struct, hashlib
from Crypto.Cipher import AES

CACHE = "cache/mi_tokens.json"
RAW = bytes.fromhex("21310020" + "ff" * 28)

with open(CACHE) as f:
    d = [v for v in json.load(f).values() if "筒灯沙发上1" in v.get("name","")][0]
ip, token, name = d["ip"], d["token"], d["name"]

tb = bytes.fromhex(token)
key = hashlib.md5(tb).digest()
iv = hashlib.md5(key + tb).digest()

def enc(plain):
    n = 16 - len(plain) % 16
    p = plain + bytes([n] * n)
    return AES.new(key, AES.MODE_CBC, iv).encrypt(p)

def dec(cypher):
    p = AES.new(key, AES.MODE_CBC, iv).decrypt(cypher)
    return p[:-p[-1]]

# ── Capture python-miio's set_power command ──────────────────────────────
from miio import Device

pkts = []
orig = socket.socket.sendto
def hook(self, data, addr):
    pkts.append(data)
    return orig(self, data, addr)
socket.socket.sendto = hook

dev = Device(ip=ip, token=token)
dev.info()  # handshake
pkts.clear()
dev.send("set_power", ["on"])
socket.socket.sendto = orig

print("python-miio packets:")
for i, p in enumerate(pkts):
    if len(p) > 32:
        hdr = p[:16]
        did = struct.unpack('>I', hdr[8:12])[0]
        ts = struct.unpack('>I', hdr[12:16])[0]
        print(f"  PKT {i}: {len(p)}B  device_id=0x{did:08x} ts={ts}")
        try:
            plain = dec(p[32:])
            print(f"    Decrypted: {json.loads(plain)}")
        except:
            print(f"    Decrypt failed")
            # Try all variants
            for desc, key_v, iv_v in [
                ("V2", key, iv),
                ("V1", tb, hashlib.md5(tb)),
                ("V3", key, hashlib.md5(b'\xff'*16 + tb)),
            ]:
                try:
                    p2 = AES.new(key_v, AES.MODE_CBC, iv_v).decrypt(p[32:])
                    print(f"    [{desc}]: {p2.hex()}")
                except:
                    pass

# ── Now our version ──────────────────────────────────────────────────────
print(f"\nOur version:")
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
sock.bind(("0.0.0.0", 0))
sock.settimeout(3)

for _ in range(3):
    sock.sendto(RAW, (ip, 54321))
data, _ = sock.recvfrom(4096)
our_did = struct.unpack('>I', data[8:12])[0]
our_ts = struct.unpack('>I', data[12:16])[0] + 1

our_cmd = json.dumps({"id": 1, "method": "set_power", "params": ["on"]}, separators=(",", ":")).encode()
our_enc = enc(our_cmd)
our_hdr = struct.pack('>HHIIII', 0x2131, 32 + len(our_enc), 0, 0, our_did, our_ts)
our_csum = hashlib.md5(our_hdr[:16] + tb + our_enc).digest()
our_pkt = our_hdr[:16] + our_csum + our_enc

sock.sendto(our_pkt, (ip, 54321))
data, _ = sock.recvfrom(4096)
try:
    resp = dec(data[32:])
    print(f"  Response: {json.loads(resp)}")
except:
    print(f"  Response hex: {data[32:].hex()[:60]}")

# ── Compare ──────────────────────────────────────────────────────────────
print(f"\nComparison:")
pmiio_pkt = [p for p in pkts if len(p) > 32][0]
print(f"  python-miio cmd:     {dec(pmiio_pkt[32:]).decode()}")
print(f"  Our cmd:             {our_cmd.decode()}")
print(f"  Headers match:       {pmiio_pkt[:32] == our_pkt[:32]}")
print(f"  python-miio header:  {pmiio_pkt[:32].hex()}")
print(f"  Our header:          {our_pkt[:32].hex()}")
sock.close()
