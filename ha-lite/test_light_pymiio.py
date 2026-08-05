#!/usr/bin/env python3
"""Find the correct control method for philips.light.downlight."""
import json, socket, struct, hashlib
from Crypto.Cipher import AES

CACHE = "cache/mi_tokens.json"

with open(CACHE) as f:
    d = [v for v in json.load(f).values() if "筒灯沙发上1" in v.get("name","")][0]
ip, token, name = d["ip"], d["token"], d["name"]
print(f"Device: {name}  IP: {ip}  Token: {token[:8]}...")

from miio import Device

# Capture to decrypt
pkts = []
orig = socket.socket.sendto
def hook(self, data, addr):
    pkts.append(data)
    return orig(self, data, addr)
socket.socket.sendto = hook

# Test with base Device.send
dev = Device(ip=ip, token=token)
print(f"\n=== miIO.info ===")
info = dev.info()
print(f"  Model: {info.model}")

socket.socket.sendto = orig
orig_pkts = list(pkts)
pkts.clear()

# Test set_power OFF
socket.socket.sendto = hook
print(f"\n=== set_power off ===")
result = dev.send("set_power", ["off"])
print(f"  Result: {result}")
socket.socket.sendto = orig

# Decrypt the set_power command
for p in pkts:
    if len(p) > 32:
        tb = bytes.fromhex(token)
        key = hashlib.md5(tb).digest()
        iv = hashlib.md5(key + tb).digest()
        for desc, k, i in [
            ("V2", key, iv),
            ("V1", tb, hashlib.md5(tb)),
            ("V3", key, hashlib.md5(b'\xff'*16 + tb)),
        ]:
            try:
                plain = AES.new(k, AES.MODE_CBC, i).decrypt(p[32:])
                if plain[-1] <= 16:
                    plain = plain[:-plain[-1]]
                    print(f"  Cmd [{desc}]: {json.loads(plain)}")
                    break
            except:
                continue

# Now test if the light actually turned off
import time; time.sleep(1)
pkts.clear()
socket.socket.sendto = hook
print(f"\n=== Check status ===")
try:
    status_result = dev.send("get_prop", ["power"])
    print(f"  get_prop power: {status_result}")
except Exception as e:
    print(f"  get_prop error: {e}")
socket.socket.sendto = orig
