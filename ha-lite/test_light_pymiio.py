#!/usr/bin/env python3
"""Compare python-miio control vs our raw miIO — verify command format."""
import json, socket, struct, hashlib
from Crypto.Cipher import AES

CACHE = "cache/mi_tokens.json"
RAW_HELLO = bytes.fromhex("21310020" + "ff" * 28)

with open(CACHE) as f:
    d = [v for v in json.load(f).values() if "筒灯沙发上1" in v.get("name","")][0]
ip, token, name = d["ip"], d["token"], d["name"]
print(f"Device: {name}  IP: {ip}")

# ── Test 1: python-miio full test ────────────────────────────────────────
print(f"\n=== Test 1: python-miio on/off ===")
from miio import Device
dev = Device(ip=ip, token=token)

# Check current state
from miio.integrations.light.philips.philips_bulb import PhilipsBulb
bulb = PhilipsBulb(ip=ip, token=token)
try:
    status = bulb.status()
    print(f"  Current: is_on={status.is_on}, brightness={status.brightness}")
except Exception as e:
    print(f"  Status error: {e}")

# Capture python-miio's set_power packet
pkts = []
orig = socket.socket.sendto
def hook(self, data, addr):
    pkts.append(data)
    return orig(self, data, addr)
socket.socket.sendto = hook

print(f"  Sending OFF via python-miio...")
bulb.off()
socket.socket.sendto = orig

print(f"  Packets captured: {len(pkts)}")
for i, p in enumerate(pkts):
    if len(p) == 32:
        print(f"  PKT {i}: RAW hello (32B)")
    else:
        # Show the encrypted command details
        hdr = p[:32]
        print(f"  PKT {i}: Command ({len(p)}B)")
        print(f"    Header: {hdr[:16].hex()}")
        print(f"    device_id=0x{struct.unpack('>I', hdr[8:12])[0]:08x} ts={struct.unpack('>I', hdr[12:16])[0]}")

# Try to decrypt the captured command to see what python-miio sends
for p in pkts:
    if len(p) > 32:
        tb = bytes.fromhex(token)
        key = hashlib.md5(tb).digest()
        iv = hashlib.md5(key + tb).digest()
        try:
            plain = AES.new(key, AES.MODE_CBC, iv).decrypt(p[32:])
            pad = plain[-1]
            plain = plain[:-pad]
            print(f"\n  Decrypted command: {json.loads(plain)}")
        except:
            pass

import time; time.sleep(1)

# ── Test 2: Our raw miIO — same format ───────────────────────────────────
print(f"\n=== Test 2: Our raw miIO ON ===")
pkts2 = []
socket.socket.sendto = hook

# Use Device.send (which uses python-miio's internal protocol)
dev.send("set_power", ["on"])

socket.socket.sendto = orig

for p in pkts2:
    if len(p) > 32:
        tb = bytes.fromhex(token)
        key = hashlib.md5(tb).digest()
        iv = hashlib.md5(key + tb).digest()
        try:
            plain = AES.new(key, AES.MODE_CBC, iv).decrypt(p[32:])
            pad = plain[-1]
            plain = plain[:-pad]
            print(f"  python-miio set_power sends: {json.loads(plain)}")
        except:
            pass

time.sleep(1)

# ── Test 3: Check state after ────────────────────────────────────────────
print(f"\n=== Test 3: State after commands ===")
try:
    status = bulb.status()
    print(f"  is_on={status.is_on}, brightness={status.brightness}")
except Exception as e:
    print(f"  Status error: {e}")
