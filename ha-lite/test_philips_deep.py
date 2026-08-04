#!/usr/bin/env python3
"""
Deep-dive: why python-miio fails on philips.light.downlight despite having unique IPs.

Tests:
  1. All miIO encryption variants (V1/V2/V3)
  2. Different commands (miIO.info, get_prop, set_power, set_properties)
  3. Different ports (54321, 554, 80)
  4. Check if device responds to TCP on common ports
"""

import json, socket, struct, hashlib, time, sys, random
from Crypto.Cipher import AES

CACHE = "cache/mi_tokens.json"

def md5hash(d): return hashlib.md5(d).digest()

def pkcs7_pad(d, bs=16):
    n = bs - len(d) % bs
    return d + bytes([n] * n)

def pkcs7_unpad(d):
    return d[:-d[-1]]

def aes_encrypt(key, iv, plain):
    c = AES.new(key, AES.MODE_CBC, iv)
    return c.encrypt(pkcs7_pad(plain))

def aes_decrypt(key, iv, enc):
    c = AES.new(key, AES.MODE_CBC, iv)
    return pkcs7_unpad(c.decrypt(enc))

def build_packet(token_hex, payload_bytes, device_id, key, iv):
    token_bytes = bytes.fromhex(token_hex)
    encrypted = aes_encrypt(key, iv, payload_bytes)
    stamp = int(time.time())
    header = struct.pack('>HHIIII', 0x2131, 32 + len(encrypted), 0, 0, device_id, stamp)
    csum = md5hash(header[:16] + token_bytes + encrypted)
    return header[:16] + csum + encrypted

def send_and_receive(ip, port, token_hex, cmd_dict, device_id, key, iv, timeout=5):
    payload = json.dumps(cmd_dict, separators=(',', ':')).encode()
    pkt = build_packet(token_hex, payload, device_id, key, iv)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(pkt, (ip, port))
        data, _ = sock.recvfrom(4096)
        if len(data) >= 32 and data[0] == 0x21 and data[1] == 0x31:
            # It's a miIO packet! Try to decrypt.
            token_bytes = bytes.fromhex(token_hex)
            encrypted = data[32:]
            for desc, k, iv in get_all_variants(token_hex):
                try:
                    plain = aes_decrypt(k, iv, encrypted)
                    return desc, json.loads(plain)
                except:
                    continue
            return "raw", data.hex()
        return "non-miio", data.hex()[:60]
    except socket.timeout:
        return None, None
    except Exception as e:
        return "error", str(e)
    finally:
        sock.close()

def get_all_variants(token_hex):
    tb = bytes.fromhex(token_hex)
    # V2: key=MD5(token), iv=MD5(key+token)
    k2 = md5hash(tb)
    yield "V2", k2, md5hash(k2 + tb)
    # V3: key=MD5(token), iv=MD5(0xFF*16+token)
    yield "V3", k2, md5hash(b'\xff'*16 + tb)
    # V1: key=token, iv=MD5(token)
    yield "V1", tb, md5hash(tb)

def main():
    name_filter = sys.argv[1] if len(sys.argv) > 1 else "筒灯"

    with open(CACHE) as f:
        devs = json.load(f)
    targets = [v for v in devs.values()
               if name_filter in v.get("name", "") and "philips" in v.get("model", "").lower()]
    if not targets:
        print(f"No Philips lights matching '{name_filter}'")
        return

    d = targets[0]
    name, ip, token, did, model = d["name"], d["ip"], d["token"], d["did"], d["model"]
    print(f"Device: {name} ({model})")
    print(f"IP: {ip}  DID: {did}  Token: {token[:8]}...")

    # 1. Try python-miio first
    print(f"\n=== Test 1: python-miio ===")
    try:
        from miio import Device
        dev = Device(ip=ip, token=token)
        info = dev.info()
        print(f"  ✅ miIO.info works! model={info.model}")
    except Exception as e:
        print(f"  ❌ python-miio: {str(e)[:120]}")

    # 2. Try raw UDP with all variants
    print(f"\n=== Test 2: Raw UDP miIO (all variants, port 54321) ===")
    hello = {"id": 1, "method": "miIO.info", "params": []}
    for desc, key, iv in get_all_variants(token):
        result_desc, result = send_and_receive(ip, 54321, token, hello, 0xFFFFFFFF, key, iv, timeout=3)
        if result:
            print(f"  {desc}: {result_desc} → {json.dumps(result, indent=2)[:200] if isinstance(result, dict) else result}")
            if isinstance(result, dict) and "result" in result:
                break
        else:
            print(f"  {desc}: timeout")

    # 3. Try different set_properties command with found key
    print(f"\n=== Test 3: set_properties via miIO ===")
    tb = bytes.fromhex(token)
    k2 = md5hash(tb)
    iv2 = md5hash(k2 + tb)

    commands = [
        ("set_power", ["on"]),
        ("set_power", ["off"]),
        ("set_properties", [{"did": f"prop-2-1", "siid": 2, "piid": 1, "value": True}]),
    ]
    for method, params in commands:
        cmd = {"id": 2, "method": method, "params": params}
        result_desc, result = send_and_receive(ip, 54321, token, cmd, 0, k2, iv2, timeout=3)
        if result:
            print(f"  {method}: {result_desc} → {json.dumps(result, indent=2)[:200] if isinstance(result, dict) else result}")
            break
        else:
            print(f"  {method}: timeout")

    # 4. Try different ports
    print(f"\n=== Test 4: Different ports ===")
    for port in [54321, 554, 80, 8080, 8443]:
        result_desc, result = send_and_receive(ip, port, token, hello, 0xFFFFFFFF, k2, iv2, timeout=2)
        if result:
            print(f"  Port {port}: {result_desc}")
        else:
            print(f"  Port {port}: timeout")

    # 5. TCP probe
    print(f"\n=== Test 5: TCP ports ===")
    for port in [80, 443, 8080, 554, 54321]:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex((ip, port))
            if result == 0:
                print(f"  Port {port}: OPEN (TCP)")
            else:
                print(f"  Port {port}: closed")
            sock.close()
        except:
            print(f"  Port {port}: error")

if __name__ == "__main__":
    main()