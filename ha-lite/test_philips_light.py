#!/usr/bin/env python3
"""
Test Philips light control — confirms Zigbee devices don't support local miIO,
then tests cloud OAuth control as the solution.

Usage:
  python3 test_philips_light.py                           # test all Philips lights
  python3 test_philips_light.py --name "筒灯沙发上1"       # test specific one
  python3 test_philips_light.py --token "ACCESS_TOKEN" --on # cloud control
"""

import json, sys, socket, time, hashlib, struct, os
from Crypto.Cipher import AES

CACHE = "cache/mi_tokens.json"

def test_local_miio(name, ip, token):
    """Try miIO.info hello — Philips lights should fail."""
    print(f"\n  Testing local miIO to {ip}:54321...")
    try:
        from miio import Device
        d = Device(ip=ip, token=token)
        info = d.info()
        print(f"  ✅ SURPRISE! miIO.info works! model={info.model}")
        return True
    except Exception as e:
        err = str(e)
        if "Unable to discover" in err:
            print(f"  ❌ Cannot discover (expected — Zigbee device, no miIO server)")
        elif "timeout" in err.lower():
            print(f"  ❌ Timeout (expected — no UDP response)")
        else:
            print(f"  ❌ {err[:80]}")
        return False

def test_cloud_control(token, did, on=True):
    """Test cloud control via ha.api.io.mi.com OAuth Bearer."""
    import urllib.request, urllib.error
    APP_ID = "2882303761520251711"
    url = "https://ha.api.io.mi.com/app/v2/miotspec/prop/set"
    value = True if on else False
    body = json.dumps({"params": [{"did": did, "siid": 2, "piid": 1, "value": value}]})
    req = urllib.request.Request(url, data=body.encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer{token}")
    req.add_header("X-Client-BizId", "haapi")
    req.add_header("X-Client-AppId", APP_ID)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read().decode())
        if result.get("code") == 0:
            action = "ON" if on else "OFF"
            print(f"  ✅ Cloud control SUCCESS: {action}")
            return True
        else:
            print(f"  ❌ Cloud API error: {result}")
    except Exception as e:
        print(f"  ❌ Cloud control failed: {e}")
    return False

def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--name", help="Device name filter")
    p.add_argument("--token", help="OAuth access_token for cloud control")
    p.add_argument("--on", action="store_true")
    p.add_argument("--off", action="store_true")
    args = p.parse_args()

    with open(CACHE) as f:
        devs = json.load(f)
    if isinstance(devs, dict):
        devs = list(devs.values())

    # Find Philips lights.
    targets = [d for d in devs if "philips" in d.get("model", "").lower() or "筒灯" in d.get("name", "")]
    if args.name:
        targets = [d for d in targets if args.name in d.get("name", "")]

    if not targets:
        print("No Philips/筒灯 devices found")
        return

    print(f"Found {len(targets)} Philips light(s):")
    for d in targets:
        print(f"  {d['name']:<25} DID={d['did']:<15} IP={d.get('ip','?'):<16}")

    for d in targets[:3]:  # Test first 3
        name, ip, token, did = d["name"], d.get("ip",""), d.get("token",""), d["did"]
        print(f"\n{'='*60}")
        print(f"Device: {name} (philips.light.downlight)")
        print(f"  DID={did}  IP={ip}")

        if ip and ip != "0.0.0.0":
            ok = test_local_miio(name, ip, token)
            if not ok and args.token:
                print(f"\n  Local miIO failed — trying cloud control instead:")
                test_cloud_control(args.token, did, on=args.on or not args.off)
        else:
            print(f"  No IP — Zigbee device, needs gateway or cloud control")
            if args.token:
                test_cloud_control(args.token, did, on=args.on or not args.off)

    if not args.token:
        print(f"\n{'='*60}")
        print(f"To test cloud control, get access_token from test_oauth_control.py")
        print(f"Then: python3 test_philips_light.py --token 'YOUR_TOKEN' --name '筒灯' --on")

if __name__ == "__main__":
    main()