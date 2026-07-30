#!/usr/bin/env python3
"""
Minimal Xiaomi MIoT encrypted API helper.
Takes credentials as input, returns device list as JSON.

Usage:
  echo '{"userId":"123","serviceToken":"abc","ssecurity":"xyz"}' | python3 miot_api.py
  python3 miot_api.py --userId=123 --serviceToken=abc --ssecurity=xyz
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
import urllib.error
import http.cookiejar
from urllib.parse import urlencode

# ── Crypto helpers ────────────────────────────────────────────────────────────

def generate_nonce(millis):
    nonce_bytes = os.urandom(8) + (int(millis / 60000)).to_bytes(4, byteorder="big")
    return base64.b64encode(nonce_bytes).decode()

def signed_nonce(nonce, ssecurity):
    h = hashlib.sha256(base64.b64decode(ssecurity) + base64.b64decode(nonce))
    return base64.b64encode(h.digest()).decode()

def encrypt_rc4(password, payload):
    try:
        from Crypto.Cipher import ARC4
    except ImportError:
        from Cryptodome.Cipher import ARC4
    r = ARC4.new(base64.b64decode(password))
    r.encrypt(bytes(1024))  # Drop first 1024 bytes of RC4 keystream (security)
    return base64.b64encode(r.encrypt(payload.encode())).decode()

def decrypt_rc4(password, payload):
    try:
        from Crypto.Cipher import ARC4
    except ImportError:
        from Cryptodome.Cipher import ARC4
    r = ARC4.new(base64.b64decode(password))
    r.encrypt(bytes(1024))  # Drop first 1024 bytes of RC4 keystream (security)
    return r.encrypt(base64.b64decode(payload))

def generate_enc_signature(url, method, signed_nonce, params):
    path = url.split("com")[1].replace("/app/", "/") if "com" in url else url
    parts = [method.upper(), path]
    for k, v in params.items():
        parts.append(f"{k}={v}")
    parts.append(signed_nonce)
    sig_str = "&".join(parts)
    return base64.b64encode(hashlib.sha1(sig_str.encode("utf-8")).digest()).decode()

def generate_enc_params(url, method, signed_nonce, nonce, params, ssecurity):
    params["rc4_hash__"] = generate_enc_signature(url, method, signed_nonce, params)
    for k in list(params.keys()):
        params[k] = encrypt_rc4(signed_nonce, params[k])
    params.update({
        "signature": generate_enc_signature(url, method, signed_nonce, params),
        "ssecurity": ssecurity,
        "_nonce": nonce,
    })
    return params

def execute_api_call(url, params, ssecurity, service_token, user_id):
    """Make an encrypted MIoT API call. Returns parsed JSON or None."""
    millis = round(time.time() * 1000)
    nonce = generate_nonce(millis)
    sn = signed_nonce(nonce, ssecurity)
    fields = generate_enc_params(url, "POST", sn, nonce, params, ssecurity)

    headers = {
        "Accept-Encoding": "identity",
        "User-Agent": "MIoT/Android APP/com.xiaomi.mihome APPV/10.5.201",
        "Content-Type": "application/x-www-form-urlencoded",
        "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2",
        "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4",
    }

    cookies = {
        "userId": str(user_id),
        "yetAnotherServiceToken": str(service_token),
        "serviceToken": str(service_token),
        "locale": "en_GB",
        "timezone": "GMT+02:00",
        "is_daylight": "1",
        "dst_offset": "3600000",
        "channel": "MI_APP_STORE",
    }

    full_url = url + "?" + urlencode(fields)
    # Use data=b"" to force POST (urllib defaults to GET when data is None).
    req = urllib.request.Request(full_url, data=b"", method="POST", headers=headers)
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    req.add_header("Cookie", cookie_str)

    try:
        resp = urllib.request.urlopen(req, timeout=15)
        body = resp.read().decode("utf-8")
        if resp.status == 200:
            decrypted = decrypt_rc4(signed_nonce(fields["_nonce"], ssecurity), body)
            return json.loads(decrypted)
    except Exception as e:
        print(f"API error: {e}", file=sys.stderr)
    return None

# ── Main ──────────────────────────────────────────────────────────────────────

def get_devices(user_id, service_token, ssecurity):
    base_url = "https://api.io.mi.com/app"

    # Step 1: Get homes.
    homes_result = execute_api_call(
        base_url + "/v2/homeroom/gethome",
        {"data": '{"fg":true,"fetch_share":true,"fetch_share_dev":true,"limit":300,"app_ver":7}'},
        ssecurity, service_token, user_id
    )

    if not homes_result or homes_result.get("code") != 0:
        print(f"get homes failed: {homes_result}", file=sys.stderr)
        return []

    homes = homes_result.get("result", {}).get("homelist", [])
    print(f"Found {len(homes)} home(s)", file=sys.stderr)

    # Step 2: Get devices for each home.
    all_devices = []
    for h in homes:
        home_name = h.get("name", "?")
        home_id = h.get("id", "")
        owner_id = h.get("uid", "")

        dev_result = execute_api_call(
            base_url + "/v2/home/home_device_list",
            {"data": '{"home_owner":' + str(owner_id) + ',"home_id":' + str(home_id) + ',"limit":200,"get_split_device":true,"support_smart_home":true}'},
            ssecurity, service_token, user_id
        )

        if dev_result and dev_result.get("code") == 0:
            # Devices are in result.device_info (direct list).
            device_list = dev_result.get("result", {}).get("device_info", [])
            if not isinstance(device_list, list):
                device_list = device_list.get("list", []) if isinstance(device_list, dict) else []
            for d in device_list:
                if d.get("did"):
                    all_devices.append({
                        "did": d.get("did", ""),
                        "name": d.get("name", ""),
                        "model": d.get("model", ""),
                        "ip": d.get("localip", ""),
                        "token": d.get("token", ""),
                        "home": home_name,
                    })
            print(f"  Home '{home_name}': {len(dev_result.get('result', {}).get('list', []))} devices", file=sys.stderr)

    return all_devices


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Xiaomi MIoT API device list")
    parser.add_argument("--userId", help="Xiaomi user ID")
    parser.add_argument("--serviceToken", help="Service token")
    parser.add_argument("--ssecurity", help="Ssecurity for encryption")

    args = parser.parse_args()

    # Read from stdin if no args.
    user_id = args.userId
    service_token = args.serviceToken
    ssecurity = args.ssecurity

    if not all([user_id, service_token, ssecurity]):
        data = json.load(sys.stdin)
        user_id = data.get("userId", data.get("user_id", ""))
        service_token = data.get("serviceToken", data.get("service_token", ""))
        ssecurity = data.get("ssecurity", "")

    if not all([user_id, service_token, ssecurity]):
        print("ERROR: missing required credentials", file=sys.stderr)
        print(json.dumps({"error": "missing credentials"}))
        sys.exit(1)

    devices = get_devices(str(user_id), str(service_token), str(ssecurity))
    print(json.dumps({"count": len(devices), "devices": devices}, indent=2, ensure_ascii=False))