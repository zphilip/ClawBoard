#!/usr/bin/env python3
"""
Debug ha-lite miIO implementation by comparing with python-miio.
Shows the exact encryption parameters and packet format that WORK.

Usage:
    python3 test_miio_debug.py                  # test "风扇"
    python3 test_miio_debug.py --name "插座"    # test specific device
    python3 test_miio_debug.py --name "风扇" --on   # turn on
    python3 test_miio_debug.py --name "风扇" --off  # turn off
"""

import argparse
import json
import sys
from miio import Device
from miio.miioprotocol import MiIOProtocol
from miio.device import DeviceInfo

CACHE = "cache/mi_tokens.json"

def find_device(name_substr):
    with open(CACHE) as f:
        devs = json.load(f)
    matches = [v for v in devs.values() if name_substr in v.get("name", "")]
    if not matches:
        print(f"No device matching '{name_substr}'")
        sys.exit(1)
    return matches[0]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="风扇")
    parser.add_argument("--on", action="store_true")
    parser.add_argument("--off", action="store_true")
    parser.add_argument("--debug-packet", action="store_true", help="Show raw packet hex")
    args = parser.parse_args()

    dev = find_device(args.name)
    ip = dev["ip"]
    token = dev["token"]
    name = dev["name"]
    model = dev["model"]
    print(f"Device: {name} ({model})")
    print(f"IP: {ip}  Token: {token}")

    d = Device(ip=ip, token=token)

    # Step 1: Show what python-miio uses for encryption
    print(f"\n=== python-miio internal state ===")
    info = d.info()
    print(f"  model: {info.model}")
    print(f"  fw_ver: {info.firmware_version}")
    print(f"  hw_ver: {info.hardware_version}")

    # Access the internal protocol to see encryption params
    proto = d._protocol
    print(f"  token (hex): {proto.token}")
    print(f"  device_id: {proto.device_id}")
    print(f"  _server_ts: {proto._server_ts}")

    # Step 2: Test control via raw send() - this shows the EXACT packet format
    print(f"\n=== Test control ===")

    # Get the specific device class for this model
    from miio import FanV2, Fan, ChuangmiPlug
    from miio.integrations.fan.zhimi.zhimi_fan import ZhimiFanV2
    from miio.integrations.genericmiot.genericmiot import GenericMiot

    # Try MIoT approach first
    try:
        miot_dev = GenericMiot(ip=ip, token=token)
        print(f"  Using GenericMiot for {model}")

        if args.on:
            # Try setting power via MIoT
            result = miot_dev.set_property(2, 1, True)
            print(f"  set_property(2, 1, True) = {result}")
        elif args.off:
            result = miot_dev.set_property(2, 1, False)
            print(f"  set_property(2, 1, False) = {result}")
        else:
            # Just get status
            status = miot_dev.get_properties([{"siid": 2, "piid": 1}])
            print(f"  get_properties = {status}")

    except Exception as e:
        print(f"  GenericMiot failed: {e}")
        # Fallback: try raw send
        print(f"  Trying raw send via MiIOProtocol...")
        cmd = {"id": 1, "method": "set_power", "params": ["on"]}
        resp = d.send("set_power", ["on"])
        print(f"  set_power(on) = {resp}")

    # Step 3: Show raw packet exchange if requested
    if args.debug_packet:
        print(f"\n=== Raw packet capture ===")
        import struct, hashlib
        from Crypto.Cipher import AES

        token_bytes = bytes.fromhex(token)
        key = hashlib.md5(token_bytes).digest()
        iv = hashlib.md5(key + token_bytes).digest()
        print(f"  key (hex): {key.hex()}")
        print(f"  iv  (hex): {iv.hex()}")
        print(f"  key = MD5(token): {hashlib.md5(token_bytes).hexdigest()}")
        print(f"  iv  = MD5(key + token): {hashlib.md5(key + token_bytes).hexdigest()}")

if __name__ == "__main__":
    main()