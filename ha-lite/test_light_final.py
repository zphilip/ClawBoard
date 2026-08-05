#!/usr/bin/env python3
"""
Final working test — uses python-miio's EXACT packet construction.
Proves local miIO control for philips.light.downlight works.
"""
import json, socket, struct, hashlib, sys
from Crypto.Cipher import AES

CACHE = "cache/mi_tokens.json"
RAW_HELLO = bytes.fromhex("21310020" + "ff" * 28)

def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "筒灯沙发上1"
    action = sys.argv[2] if len(sys.argv) > 2 else "on"

    with open(CACHE) as f:
        d = [v for v in json.load(f).values() if name in v.get("name","")][0]
    ip, token = d["ip"], d["token"]

    tb = bytes.fromhex(token)
    key = hashlib.md5(tb).digest()
    iv = hashlib.md5(key + tb).digest()

    # Helper
    def enc(plain):
        n = 16 - len(plain) % 16
        return AES.new(key, AES.MODE_CBC, iv).encrypt(plain + bytes([n]*n))

    # Step 1: python-miio handshake + command (KNOWN WORKING)
    print(f"=== python-miio control ===")
    from miio import Device
    dev = Device(ip=ip, token=token)
    before = dev.send("get_prop", ["power"])
    print(f"  Before: {before}")

    # Capture exact packet python-miio sends
    import miio.miioprotocol as mp
    proto = dev._protocol
    # Force handshake to get fresh device_id + ts
    proto.send_handshake()
    did = proto._device_id
    ts = proto._device_ts

    print(f"  Python-miio internal state:")
    print(f"    _device_id: {did}")
    print(f"    _device_ts: {ts}")
    print(f"    _device_id bytes: {did.hex() if isinstance(did, bytes) else hex(did)}")
    print(f"    _device_ts type: {type(ts).__name__}")

    # See what _create_request produces
    request = proto._create_request("set_power", [action])
    print(f"  _create_request output: {json.dumps(request)}")

    # Send via python-miio
    result = dev.send("set_power", [action])
    print(f"  After: {dev.send('get_prop', ['power'])}")
    print(f"  Result: {result}")

if __name__ == "__main__":
    main()
