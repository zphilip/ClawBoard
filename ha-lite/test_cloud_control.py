#!/usr/bin/env python3
"""
Test Xiaomi Cloud Device Control — tries multiple endpoints on api.io.mi.com.
Usage:
  python3 test_cloud_control.py --ssecurity <s> --token <t> --user <u> --name "风扇" --on
"""

import argparse, base64, hashlib, json, os, sys, time, urllib.request, urllib.error
from urllib.parse import urlencode

try:
    from Crypto.Cipher import ARC4
except ImportError:
    from Cryptodome.Cipher import ARC4

CACHE = "cache/mi_tokens.json"
API_BASE = "https://api.io.mi.com/app"

# ── Crypto ────────────────────────────────────────────────────────────────────

def gen_nonce(millis=None):
    if millis is None: millis = round(time.time() * 1000)
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
    millis = round(time.time() * 1000)
    nonce = gen_nonce(millis)
    sn = signed_nonce(nonce, ssecurity)
    fields = enc_params(url, "POST", sn, nonce, {"data": data}, ssecurity)

    full_url = url + "?" + urlencode(fields)
    req = urllib.request.Request(full_url, data=b"", method="POST")
    req.add_header("Accept-Encoding", "identity")
    req.add_header("User-Agent", "MIoT/Android APP/com.xiaomi.mihome APPV/10.5.201")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("x-xiaomi-protocal-flag-cli", "PROTOCAL-HTTP2")
    req.add_header("MIOT-ENCRYPT-ALGORITHM", "ENCRYPT-RC4")
    req.add_header("Cookie", f"userId={user_id}; serviceToken={service_token}; yetAnotherServiceToken={service_token}; cUserId={user_id}; locale=en_GB; timezone=GMT+02:00; is_daylight=1; dst_offset=3600000; channel=MI_APP_STORE; countryCode=CN")

    try:
        resp = urllib.request.urlopen(req, timeout=15)
        if resp.status == 200:
            decrypted = decrypt_rc4(signed_nonce(fields["_nonce"], ssecurity), resp.read().decode())
            return json.loads(decrypted)
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode(errors="replace"))
        except:
            pass
    except Exception as e:
        print(f"    ERROR: {e}", file=sys.stderr)
    return None

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--name", default="风扇")
    p.add_argument("--on", action="store_true")
    p.add_argument("--off", action="store_true")
    p.add_argument("--ssecurity", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--user", required=True)
    args = p.parse_args()

    with open(CACHE) as f:
        devs = [v for v in json.load(f).values() if args.name in v.get("name", "")]
    if not devs:
        print(f"No device matching '{args.name}'"); return
    d = devs[0]

    name, did, model = d["name"], d["did"], d["model"]
    print(f"Device: {name} ({model}) DID={did}")

    # Try multiple endpoints — all on api.io.mi.com.
    endpoints = [
        "/miotspec/prop/set",
        "/miotspec/prop/get",
        "/v2/device/prop/set",
        "/v2/device/prop/get",
        "/app/device/rpc",
        "/home/rpc",
    ]

    value = True if args.on else False
    action = "on" if args.on else "off"

    for ep in endpoints:
        payload = json.dumps({"params": [{"did": did, "siid": 2, "piid": 1, "value": value}]}, separators=(',', ':'))
        result = api_call(f"{API_BASE}{ep}", payload, args.ssecurity, args.token, args.user)
        if result:
            code = result.get("code", -1)
            if code in (0, 1):
                print(f"  ✅ {ep} → SUCCESS (code={code})")
                break
            else:
                print(f"  ❌ {ep} → code={code} msg={result.get('message','?')}")
        else:
            print(f"  ❌ {ep} → no response")
    else:
        print("\nNo endpoint worked on api.io.mi.com.")

if __name__ == "__main__":
    main()