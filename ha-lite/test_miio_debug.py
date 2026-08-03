#!/usr/bin/env python3
"""Debug ha-lite miIO — compare encryption with python-miio."""
import argparse, json, sys, hashlib, struct
from Crypto.Cipher import AES
from miio import Device

CACHE = "cache/mi_tokens.json"

def md5hash(d): return hashlib.md5(d).digest()

def pkcs7_pad(d, bs=16):
    n = bs - len(d) % bs
    return d + bytes([n] * n)

def miio_encrypt(token_hex, payload_json):
    """Match python-miio's encryption: key=MD5(token), iv=MD5(key+token)."""
    tb = bytes.fromhex(token_hex)
    key = md5hash(tb)
    iv = md5hash(key + tb)
    plain = json.dumps(payload_json, separators=(',', ':')).encode()
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return key, iv, cipher.encrypt(pkcs7_pad(plain))

def build_packet(token_hex, payload_json, device_id=0):
    tb = bytes.fromhex(token_hex)
    key, iv, encrypted = miio_encrypt(token_hex, payload_json)
    stamp = 0  # python-miio uses increasing stamp but 0 works for first msg
    header = struct.pack('>HHIIII', 0x2131, 32 + len(encrypted), 0, 0, device_id, stamp)
    csum = md5hash(header[:16] + tb + encrypted)
    return header[:16] + csum + encrypted

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--name", default="风扇")
    p.add_argument("--on", action="store_true")
    p.add_argument("--off", action="store_true")
    args = p.parse_args()

    with open(CACHE) as f:
        devs = json.load(f)
    d = [v for v in devs.values() if args.name in v.get("name", "")][0]
    token, ip, name = d["token"], d["ip"], d["name"]

    print(f"=== python-miio can control? ===")
    dev = Device(ip=ip, token=token)
    info = dev.info()
    print(f"  ✅ miIO.info works (model={info.model})")

    # Show encryption params
    tb = bytes.fromhex(token)
    key = md5hash(tb)
    iv = md5hash(key + tb)
    print(f"  token bytes ({len(tb)}): {tb.hex()}")
    print(f"  key = MD5(token):       {key.hex()}")
    print(f"  iv  = MD5(key+token):   {iv.hex()}")

    # Test set_power via raw send (exactly what python-miio does)
    if args.on or args.off:
        action = "on" if args.on else "off"
        print(f"\n=== Testing set_power('{action}') ===")
        try:
            resp = dev.send("set_power", [action])
            print(f"  ✅ set_power('{action}') → {resp}")
        except Exception as e:
            print(f"  ❌ set_power failed: {e}")

            # Try MIoT set_properties
            print(f"\n=== Testing MIoT set_properties ===")
            try:
                from miio.integrations.genericmiot.genericmiot import GenericMiot
                miot = GenericMiot(ip=ip, token=token)
                val = True if args.on else False
                result = miot.set_property(2, 1, val)
                print(f"  ✅ MIoT set_property(2, 1, {val}) → {result}")
            except Exception as e2:
                print(f"  ❌ MIoT also failed: {e2}")

    # Show our packet construction vs python-miio's
    print(f"\n=== Our packet (for verification) ===")
    cmd = {"id": 1, "method": "set_power", "params": ["on"]}
    pkt = build_packet(token, cmd)
    print(f"  Packet length: {len(pkt)}")
    print(f"  Header hex: {pkt[:32].hex()}")
    print(f"  Payload len: {len(pkt)-32}")

if __name__ == "__main__":
    main()