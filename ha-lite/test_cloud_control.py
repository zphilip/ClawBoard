#!/usr/bin/env python3
"""
Control Xiaomi devices via Cloud API (MIoT spec).
Works for ALL devices — WiFi, Zigbee, BLE.

Usage:
    python3 test_cloud_control.py --name "风扇" --off
    python3 test_cloud_control.py --name "筒灯电视上3" --on
    python3 test_cloud_control.py --name "风扇" --on
    python3 test_cloud_control.py --list  # list all devices
"""

import argparse, base64, hashlib, json, os, sys, time, urllib.request, urllib.error
from urllib.parse import urlencode

# Try to import pycryptodome for RC4.
try:
    from Crypto.Cipher import ARC4
except ImportError:
    from Cryptodome.Cipher import ARC4

CACHE = "cache/mi_tokens.json"
API_BASE = "https://api.io.mi.com/app"

# ── RC4 encryption helpers (matching token_extractor.py) ──────────────────────

def gen_nonce(millis=None):
    if millis is None:
        millis = round(time.time() * 1000)
    return base64.b64encode(os.urandom(8) + (int(millis / 60000)).to_bytes(4, "big")).decode()

def signed_nonce(nonce, ssecurity):
    h = hashlib.sha256(base64.b64decode(ssecurity) + base64.b64decode(nonce))
    return base64.b64encode(h.digest()).decode()

def encrypt_rc4(password, payload):
    r = ARC4.new(base64.b64decode(password))
    r.encrypt(bytes(1024))
    return base64.b64encode(r.encrypt(payload.encode())).decode()

def decrypt_rc4(password, payload):
    r = ARC4.new(base64.b64decode(password))
    r.encrypt(bytes(1024))
    return r.encrypt(base64.b64decode(payload)).decode()

def enc_signature(url, method, signed_nonce, params):
    path = url.split("com")[1].replace("/app/", "/") if "com" in url else url
    parts = [method.upper(), path]
    for k, v in params.items():
        parts.append(f"{k}={v}")
    parts.append(signed_nonce)
    return base64.b64encode(hashlib.sha1("&".join(parts).encode()).digest()).decode()

def enc_params(url, method, signed_nonce, nonce, params, ssecurity):
    params["rc4_hash__"] = enc_signature(url, method, signed_nonce, params)
    for k in list(params.keys()):
        params[k] = encrypt_rc4(signed_nonce, params[k])
    params.update({
        "signature": enc_signature(url, method, signed_nonce, params),
        "ssecurity": ssecurity,
        "_nonce": nonce,
    })
    return params

def api_call(url, data, ssecurity, service_token, user_id):
    """Make encrypted MIoT API call. Returns parsed JSON."""
    millis = round(time.time() * 1000)
    nonce = gen_nonce(millis)
    sn = signed_nonce(nonce, ssecurity)
    fields = enc_params(url, "POST", sn, nonce, {"data": data}, ssecurity)

    full_url = url + "?" + urlencode(fields)
    print(f"    DEBUG URL: {full_url[:120]}...", file=sys.stderr)
    req = urllib.request.Request(full_url, data=b"", method="POST")
    req.add_header("Accept-Encoding", "identity")
    req.add_header("User-Agent", "MIoT/Android APP/com.xiaomi.mihome APPV/10.5.201")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("x-xiaomi-protocal-flag-cli", "PROTOCAL-HTTP2")
    req.add_header("MIOT-ENCRYPT-ALGORITHM", "ENCRYPT-RC4")
    cookies = f"userId={user_id}; serviceToken={service_token}; yetAnotherServiceToken={service_token}; cUserId={user_id}; locale=en_GB; timezone=GMT+02:00; is_daylight=1; dst_offset=3600000; channel=MI_APP_STORE; countryCode=CN"
    req.add_header("Cookie", cookies)

    try:
        resp = urllib.request.urlopen(req, timeout=15)
        body = resp.read().decode()
        if resp.status == 200:
            decrypted = decrypt_rc4(signed_nonce(fields["_nonce"], ssecurity), body)
            return json.loads(decrypted)
        print(f"    HTTP {resp.status}: {body[:200]}", file=sys.stderr)
    except urllib.error.HTTPError as e:
        print(f"    HTTP {e.code}: {e.read().decode(errors='replace')[:200]}", file=sys.stderr)
    return None

# ── Main ──────────────────────────────────────────────────────────────────────

def load_creds():
    """Try to get credentials from ha-lite health endpoint or cache."""
    # Try ha-lite server first.
    try:
        req = urllib.request.Request("http://127.0.0.1:8090/api/health")
        resp = urllib.request.urlopen(req, timeout=2)
        health = json.loads(resp.read())
        if health.get("cloud_authed"):
            # Read credentials from ha-lite's cache.
            # The ssecurity/serviceToken/userId are in the QR login manager.
            # We need to read them from ha-lite's internal state.
            # For now, try reading from ha-lite's status endpoint.
            req2 = urllib.request.Request("http://127.0.0.1:8090/api/login/qr/status")
            resp2 = urllib.request.urlopen(req2, timeout=2)
            status = json.loads(resp2.read())
            if status.get("has_service_token"):
                print("  ✅ ha-lite is authenticated. Credentials are internal to ha-lite process.")
                print("  → Use these from ha-lite logs or pass manually:")
                print("     python3 test_cloud_control.py --ssecurity <s> --token <t> --user <u> --name '风扇' --on")
                return None, None, None
    except:
        pass
    return None, None, None

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", help="Device name (substring match)")
    parser.add_argument("--did", help="Device ID (exact)")
    parser.add_argument("--on", action="store_true")
    parser.add_argument("--off", action="store_true")
    parser.add_argument("--list", action="store_true", help="List all devices")
    parser.add_argument("--ssecurity", help="ssecurity from QR login")
    parser.add_argument("--token", help="serviceToken from QR login")
    parser.add_argument("--user", help="userId from QR login")
    args = parser.parse_args()

    # Load devices.
    if not os.path.exists(CACHE):
        print(f"ERROR: {CACHE} not found")
        sys.exit(1)
    with open(CACHE) as f:
        devs = json.load(f)
    if isinstance(devs, dict):
        devs = list(devs.values())

    if args.list:
        print(f"{'Name':<30} {'Model':<30} {'DID':<20} {'IP':<16}")
        print("-" * 96)
        for d in sorted(devs, key=lambda x: x.get("name", "")):
            print(f"{d.get('name','?'):<30} {d.get('model','?'):<30} {d.get('did','?'):<20} {d.get('ip','?'):<16}")
        return

    # Find target device.
    targets = []
    for d in devs:
        if args.did and d.get("did") == args.did:
            targets.append(d)
        elif args.name and args.name in d.get("name", ""):
            targets.append(d)
        elif not args.name and not args.did:
            # Default: test "风扇" and first "筒灯"
            if "风扇" in d.get("name", "") or "筒灯" in d.get("name", ""):
                targets.append(d)

    if not targets:
        print(f"No device matching '{args.name or args.did or '风扇/筒灯'}'")
        return

    # Get credentials.
    ssecurity = args.ssecurity
    token = args.token
    user_id = args.user

    if not all([ssecurity, token, user_id]):
        ssecurity, token, user_id = load_creds()

    if not all([ssecurity, token, user_id]):
        print("\n❌ Credentials required. Get them from ha-lite QR login logs:")
        print("   journalctl -u halite | grep 'ssecurity=\\|serviceToken=\\|userId='")
        print("\nThen run:")
        print("   python3 test_cloud_control.py --ssecurity <s> --token <t> --user <u> --name '风扇' --on")
        sys.exit(1)

    # Control each device.
    for d in targets:
        name = d.get("name", "?")
        did = d.get("did", "")
        model = d.get("model", "")
        print(f"\n{'='*60}")
        print(f"Device: {name} ({model})  DID: {did}")
        print(f"{'='*60}")

        # Step 1: Get current status.
        print(f"  Reading status...")
        result = api_call(
            f"{API_BASE}/miotspec/prop/get",
            json.dumps({"params": [{"did": did, "siid": 2, "piid": 1}]}, separators=(',', ':')),
            ssecurity, token, user_id)
        if result and result.get("code") == 0:
            props = result.get("result", [])
            for p in props:
                print(f"    siid={p.get('siid')} piid={p.get('piid')} value={p.get('value')}")

        # Step 2: Control if requested.
        if args.on or args.off:
            value = True if args.on else False
            action = "on" if args.on else "off"
            print(f"  Setting power → {action}...")

            result = api_call(
                f"{API_BASE}/miotspec/prop/set",
                json.dumps({"params": [{"did": did, "siid": 2, "piid": 1, "value": value}]}, separators=(',', ':')),
                ssecurity, token, user_id)

            if result and result.get("code") in (0, 1):
                print(f"  ✅ Cloud control SUCCESS: {action}")
            else:
                print(f"  ❌ Failed: {result}")

if __name__ == "__main__":
    main()