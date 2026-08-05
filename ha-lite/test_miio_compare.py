#!/usr/bin/env python3
"""
Compare python-miio's socket behavior with ha-lite's Go approach.
Identifies the exact difference preventing local UDP control.

Tests:
  1. python-miio (working) — capture exact packets
  2. Raw socket (like Go ListenUDP + WriteTo) — same bytes, does it work?
  3. Raw socket with DialUDP (connected, like old Go) — same bytes?
"""

import json, socket, struct, hashlib, time, sys
from Crypto.Cipher import AES

CACHE = "cache/mi_tokens.json"
RAW_HELLO = bytes.fromhex("21310020" + "ff" * 28)  # 32 bytes, matching python-miio exactly

def find_device(name):
    with open(CACHE) as f:
        devs = json.load(f)
    return [v for v in devs.values() if name in v.get("name", "")][0]

def md5hash(d): return hashlib.md5(d).digest()

def aes_encrypt(key, iv, plain, bs=16):
    n = bs - len(plain) % bs
    p = plain + bytes([n] * n)
    return AES.new(key, AES.MODE_CBC, iv).encrypt(p)

def build_cmd_pkt(token_hex, device_id, dev_ts, method, params):
    tb = bytes.fromhex(token_hex)
    key = md5hash(tb)
    iv = md5hash(key + tb)
    cmd = json.dumps({"id": 1, "method": method, "params": params}, separators=(',', ':')).encode()
    enc = aes_encrypt(key, iv, cmd)
    header = struct.pack('>HHIIII', 0x2131, 32 + len(enc), 0, 0, device_id, dev_ts)
    csum = md5hash(header[:16] + tb + enc)
    return header[:16] + csum + enc


def test_python_miio(ip, token):
    """Test 1: python-miio (known working)."""
    print(f"\n=== Test 1: python-miio (working) ===")
    from miio import Device
    dev = Device(ip=ip, token=token)
    try:
        dev.info()
        print("  ✅ miIO.info works")
        return True
    except Exception as e:
        print(f"  ❌ {e}")
        return False


def test_raw_socket_listen(ip, token):
    """Test 2: Raw socket with ListenUDP + WriteTo (like Go v0.6.2)."""
    print(f"\n=== Test 2: Raw socket (ListenUDP → WriteTo, like Go) ===")

    # Create socket like Go's ListenUDP
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("0.0.0.0", 0))
    sock.settimeout(5)

    # Send RAW hello 3 times
    for i in range(3):
        sock.sendto(RAW_HELLO, (ip, 54321))

    try:
        data, addr = sock.recvfrom(4096)
        print(f"  ✅ RAW hello response: {len(data)} bytes from {addr}")
        print(f"     hex: {data.hex()[:80]}")

        device_id = struct.unpack('>I', data[8:12])[0]
        dev_ts = struct.unpack('>I', data[12:16])[0] + 1
        print(f"     device_id=0x{device_id:08x} ts={dev_ts}")

        # Send encrypted command
        pkt = build_cmd_pkt(token, device_id, dev_ts, "set_power", ["on"])
        sock.sendto(pkt, (ip, 54321))
        data2, _ = sock.recvfrom(4096)
        print(f"  ✅ Command response: {len(data2)} bytes")
        print(f"     hex: {data2.hex()[:80]}")
        sock.close()
        return True
    except socket.timeout:
        print(f"  ❌ Timeout — no response to RAW hello")
        sock.close()
        return False


def test_raw_socket_dial(ip, token):
    """Test 3: Raw socket with connect (DialUDP, like old Go)."""
    print(f"\n=== Test 3: Raw socket (connect → send, like old Go) ===")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.connect((ip, 54321))
    sock.settimeout(5)

    for i in range(3):
        sock.send(RAW_HELLO)

    try:
        data = sock.recv(4096)
        print(f"  ✅ RAW hello response: {len(data)} bytes")
        print(f"     hex: {data.hex()[:80]}")
        sock.close()
        return True
    except socket.timeout:
        print(f"  ❌ Timeout")
        sock.close()
        return False


def test_go_approach(ip, token):
    """Test 5: Exact Go approach — udp4, SO_BROADCAST, 3 sends, ReadFrom."""
    print(f"\n=== Test 5: Exact Go approach (udp4 + SO_BROADCAST) ===")

    # Create socket like Go's ListenUDP("udp4")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("0.0.0.0", 0))
    sock.settimeout(5)

    print(f"  Local addr: {sock.getsockname()}")

    # Send 3 times (matching Go)
    for i in range(3):
        sock.sendto(RAW_HELLO, (ip, 54321))

    try:
        data, addr = sock.recvfrom(4096)
        print(f"  ✅ Hello response: {len(data)}B from {addr}")
        device_id = struct.unpack('>I', data[8:12])[0]
        dev_ts = struct.unpack('>I', data[12:16])[0] + 1

        # Send set_power (matching Go)
        pkt = build_cmd_pkt(token, device_id, dev_ts, "set_power", ["on"])
        sock.sendto(pkt, (ip, 54321))
        data2, _ = sock.recvfrom(4096)
        print(f"  ✅ set_power response: {len(data2)}B")
        print(f"  ✅ Go approach WORKS!")
        sock.close()
        return True
    except socket.timeout:
        print(f"  ❌ Timeout")
        sock.close()
        return False


def test_local_control_full(ip, token, name):
    """Test 6: Full local control cycle — info → on → off → on."""
    print(f"\n=== Test 6: Full local control cycle ===")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("0.0.0.0", 0))
    sock.settimeout(5)

    # Get device info
    for _ in range(3):
        sock.sendto(RAW_HELLO, (ip, 54321))
    data, _ = sock.recvfrom(4096)
    device_id = struct.unpack('>I', data[8:12])[0]
    dev_ts = struct.unpack('>I', data[12:16])[0] + 1

    # miIO.info
    pkt = build_cmd_pkt(token, device_id, dev_ts, "miIO.info", [])
    sock.sendto(pkt, (ip, 54321))
    data, _ = sock.recvfrom(4096)
    print(f"  miIO.info: {len(data)}B response")

    dev_ts += 1

    # ON
    pkt = build_cmd_pkt(token, device_id, dev_ts, "set_power", ["on"])
    sock.sendto(pkt, (ip, 54321))
    data, _ = sock.recvfrom(4096)
    print(f"  set_power ON: {len(data)}B response")
    print(f"  ✅ Light should be ON now — check {name}")

    import time as _t
    _t.sleep(2)

    dev_ts += 1

    # OFF
    pkt = build_cmd_pkt(token, device_id, dev_ts, "set_power", ["off"])
    sock.sendto(pkt, (ip, 54321))
    data, _ = sock.recvfrom(4096)
    print(f"  set_power OFF: {len(data)}B response")
    print(f"  ✅ Light should be OFF now — check {name}")

    sock.close()
    return True


def test_python_miio_capture(ip, token):
    """Test 4: python-miio with packet capture — see exact bytes sent."""
    print(f"\n=== Test 4: python-miio packet capture ===")

    from miio import Device
    from miio.miioprotocol import MiIOProtocol

    orig_sendto = socket.socket.sendto
    pkts = []
    def hook(self, data, addr):
        pkts.append(data)
        return orig_sendto(self, data, addr)

    socket.socket.sendto = hook
    dev = Device(ip=ip, token=token)
    dev.info()
    socket.socket.sendto = orig_sendto

    for i, p in enumerate(pkts):
        print(f"  PKT {i}: {len(p)} bytes → {p.hex()[:80]}")
        if len(p) > 32:
            # Encrypted packet — show header
            print(f"    device_id=0x{struct.unpack('>I', p[8:12])[0]:08x} ts={struct.unpack('>I', p[12:16])[0]}")

    # Also compare our hello with python-miio's
    our_hello = RAW_HELLO
    their_hello = pkts[0] if pkts else b""
    print(f"\n  Compare RAW hello:")
    print(f"    Ours:   {our_hello.hex()}")
    print(f"    Theirs: {their_hello.hex()}")
    print(f"    Match: {our_hello == their_hello}")


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "筒灯沙发上1"
    d = find_device(name)
    ip, token = d["ip"], d["token"]
    print(f"Device: {d['name']} ({d['model']})")
    print(f"IP: {ip}  Token: {token[:8]}...")

    test_python_miio(ip, token)
    test_raw_socket_listen(ip, token)
    test_raw_socket_dial(ip, token)
    test_go_approach(ip, token)
    test_local_control_full(ip, token, name)

if __name__ == "__main__":
    main()