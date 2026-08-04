#!/usr/bin/env python3
"""
OAuth 2.0 test service — uses HA's client_id to get Bearer token,
then controls Xiaomi devices via cloud API (no HA required).

How it works:
  1. Start HTTP server on port 8123 (matches HA's redirect_url)
  2. Open OAuth URL in browser → login → authorize
  3. Receive callback → exchange code for access_token
  4. Use token for device control
  5. Save token to cache/oauth_token.json for reuse

Usage:
  python3 test_oauth_control.py                          # login + test
  python3 test_oauth_control.py --token-file token.json   # reuse saved token
  python3 test_oauth_control.py --name "风扇" --on        # control after login
"""

import argparse, hashlib, http.server, json, os, random, string, sys, time, urllib.parse, urllib.request, threading

# ── Constants (from ha_xiaomi_home) ───────────────────────────────────────────
CLIENT_ID = "2882303761520251711"
OAUTH_AUTHORIZE_URL = "https://account.xiaomi.com/oauth2/authorize"
OAUTH_TOKEN_URL = "https://ha.api.io.mi.com/app/v2/ha/oauth/get_token"
API_HOST = "https://ha.api.io.mi.com"
REDIRECT_PORT = 8123
REDIRECT_PATH = "/callback"
OAUTH_SCOPE = "1 3 6000"  # profile, open_id, smart home
STATE = "".join(random.choices(string.ascii_letters, k=16))

# ── Global state ──────────────────────────────────────────────────────────────
auth_code = None
auth_done = threading.Event()
token_file = "cache/oauth_token.json"

# ── HTTP callback server ──────────────────────────────────────────────────────
class OAuthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path == REDIRECT_PATH:
            if "code" in params:
                auth_code = params["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                html = """<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="font-family:sans-serif;text-align:center;padding:40px;">
<h2 style="color:green;">Authorization Successful!</h2>
<p>You can close this window and return to the terminal.</p>
</body></html>"""
                self.wfile.write(html.encode("utf-8"))
                auth_done.set()
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Missing code parameter")
        elif parsed.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # silence logs


def start_callback_server():
    server = http.server.HTTPServer(("0.0.0.0", REDIRECT_PORT), OAuthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ── OAuth flow ────────────────────────────────────────────────────────────────
def get_authorization_url(local_ip="127.0.0.1"):
    redirect_uri = f"http://{local_ip}:{REDIRECT_PORT}{REDIRECT_PATH}"
    # For the OAuth server to accept the redirect, the hostname must match
    # the registered redirect URL. We use homeassistant.local but with an alias.
    # Simpler: just map 127.0.0.1 to homeassistant.local in /etc/hosts
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": OAUTH_SCOPE,
        "state": STATE,
        "skip_confirm": "true",
    }
    return f"{OAUTH_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def exchange_code_for_token(code, local_ip="127.0.0.1"):
    """Exchange auth code for access_token via Xiaomi's custom OAuth endpoint."""
    data = json.dumps({
        "client_id": CLIENT_ID,
        "code": code,
        "grant_type": "authorization_code",
    })
    url = f"{OAUTH_TOKEN_URL}?data={urllib.parse.quote(data)}"
    print(f"  Token endpoint: {url[:120]}...")
    req = urllib.request.Request(url, method="GET")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("X-Client-BizId", "haapi")
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read().decode())
        print(f"  Token response: {json.dumps(result, indent=2)[:500]}")
        return result
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"  Token exchange HTTP {e.code}: {body}")
        return None
    except Exception as e:
        print(f"  Token exchange error: {e}")
        return None


def save_token(token_data, path=token_file):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(token_data, f, indent=2)


def load_token(path=token_file):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None


# ── Device control with Bearer token ──────────────────────────────────────────
def api_get_prop(access_token, did, siid=2, piid=1):
    """Read device property via cloud API."""
    url = f"{API_HOST}/app/v2/miotspec/prop/get"
    body = json.dumps({"datasource": 1, "params": [{"did": did, "siid": siid, "piid": piid}]})
    req = urllib.request.Request(url, data=body.encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer{access_token}")
    req.add_header("X-Client-BizId", "haapi")
    req.add_header("Host", "ha.api.io.mi.com")
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode(errors="replace"))


def api_set_prop(access_token, did, siid, piid, value):
    """Set device property via cloud API."""
    url = f"{API_HOST}/app/v2/miotspec/prop/set"
    body = json.dumps({"params": [{"did": did, "siid": siid, "piid": piid, "value": value}]})
    req = urllib.request.Request(url, data=body.encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer{access_token}")
    req.add_header("X-Client-BizId", "haapi")
    req.add_header("Host", "ha.api.io.mi.com")
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode(errors="replace"))


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="Xiaomi OAuth2 Cloud Control Test")
    p.add_argument("--name", default="风扇", help="Device name to control")
    p.add_argument("--on", action="store_true")
    p.add_argument("--off", action="store_true")
    p.add_argument("--ip", default="127.0.0.1", help="Local IP for redirect (try 192.168.x.x or localhost)")
    args = p.parse_args()

    # Try to load saved token.
    token = load_token()
    if token and token.get("access_token"):
        # Check if expired.
        if token.get("expires_ts", 0) > time.time():
            print(f"✅ Using saved token (expires in {int(token['expires_ts'] - time.time())}s)")
        else:
            print("⚠️  Saved token expired, re-login needed.")
            token = None

    if not token:
        # ── OAuth login ───────────────────────────────────────────────────────
        server = start_callback_server()
        auth_url = get_authorization_url(args.ip)
        print(f"\n{'='*60}")
        print(f"  1. First, map the redirect hostname to your IP:")
        print(f"     sudo echo '{args.ip} homeassistant.local' >> /etc/hosts")
        print(f"  OR: just use IP if it matches the registered redirect.")
        print(f"\n  2. Open this URL in your browser:")
        print(f"\n  \033[1;34m{auth_url}\033[0m")
        print(f"\n  3. Login and authorize. The browser will redirect back.")
        print(f"     Listening on port {REDIRECT_PORT} for callback...")
        print(f"{'='*60}\n")

        if not auth_done.wait(timeout=300):
            print("❌ Timeout waiting for authorization (5 minutes).")
            server.shutdown()
            sys.exit(1)

        server.shutdown()
        print("✅ Authorization code received! Exchanging for token...")

        result = exchange_code_for_token(auth_code, args.ip)
        if not result or "access_token" not in result:
            print("❌ Failed to get access_token.")
            sys.exit(1)

        token = {
            "access_token": result["access_token"],
            "refresh_token": result.get("refresh_token", ""),
            "expires_in": result.get("expires_in", 86400),
            "expires_ts": time.time() + result.get("expires_in", 86400) * 0.7,
        }
        save_token(token)
        print(f"✅ Token saved to {token_file}")

    access_token = token["access_token"]

    # ── Get device list for testing ───────────────────────────────────────────
    print(f"\nGetting device list from cloud...")
    # Use the device list API with Bearer token
    url = f"{API_HOST}/app/v2/home/device_list_page"
    body = json.dumps({"limit": 200, "get_split_device": True, "get_third_device": True})
    req = urllib.request.Request(url, data=body.encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer{access_token}")
    req.add_header("X-Client-BizId", "haapi")
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        devices_resp = json.loads(resp.read().decode())
        devices = devices_resp.get("result", {}).get("list", [])
        print(f"  Found {len(devices)} devices via cloud API")
    except Exception as e:
        print(f"  Device list failed: {e}")
        devices = []

    # ── Find target device ────────────────────────────────────────────────────
    target = None
    for d in devices:
        if args.name in d.get("name", ""):
            target = d
            break

    if not target:
        print(f"Device '{args.name}' not found in cloud device list. Available:")
        for d in sorted(devices, key=lambda x: x.get("name", "")):
            print(f"  {d.get('name','?'):<30} {d.get('model','?'):<25} DID={d.get('did','?')}")
        return

    did = target["did"]
    name = target["name"]
    model = target.get("model", "?")
    print(f"\nDevice: {name} ({model})  DID={did}")

    # ── Read current state ────────────────────────────────────────────────────
    result = api_get_prop(access_token, did)
    if result and result.get("code") == 0:
        for p in result.get("result", []):
            print(f"  Current: siid={p.get('siid')} piid={p.get('piid')} value={p.get('value')}")
    else:
        print(f"  Get props: {result}")

    # ── Control ───────────────────────────────────────────────────────────────
    if args.on or args.off:
        value = True if args.on else False
        action = "on" if args.on else "off"
        print(f"\n  Setting power → {action}...")
        result = api_set_prop(access_token, did, 2, 1, value)
        if result and result.get("code") in (0, 1):
            print(f"  ✅ Cloud control SUCCESS: {action}")
        else:
            print(f"  ❌ Failed: {result}")
    else:
        print(f"\n  No action specified. Use --on or --off to control.")


if __name__ == "__main__":
    main()