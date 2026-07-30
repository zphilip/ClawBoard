#!/usr/bin/env python3
"""
Direct test of Xiaomi QR login API — mimics exactly what ha-lite's Go code does.
Compares with the Go implementation to find bugs in the LP polling.

Usage:
    python3 test_qr_direct.py
    python3 test_qr_direct.py cn   # use China server
"""

import json
import sys
import time
import urllib.request
import urllib.error
import http.cookiejar
from urllib.parse import urlencode

REGION = sys.argv[1] if len(sys.argv) > 1 else "cn"

# ── Colors ─────────────────────────────────────────────────────────────────────
class C:
    R = "\033[0;31m"; G = "\033[0;32m"; Y = "\033[1;33m"
    B = "\033[0;34m"; N = "\033[0m"

# ── Create session with cookie jar (matches Go's cookiejar.New) ───────────────
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

# ──────────────────────────────────────────────────────────────────────────────
# Step 1: Get QR URLs from Xiaomi (matches Go Start() → Step 1)
# ──────────────────────────────────────────────────────────────────────────────
print(f"{C.B}═══ Step 1: Get QR URLs from Xiaomi ═══{C.N}")

params = {
    "_qrsize": "480",
    "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
    "callback": "https://sts.api.io.mi.com/sts",
    "_hasLogo": "false",
    "sid": "xiaomiio",
    "serviceParam": "",
    "_locale": "en_GB",
    "_dc": str(int(time.time() * 1000)),
}

qru = "https://account.xiaomi.com/longPolling/loginUrl"
url = qru + "?" + urlencode(params)
print(f"  URL: {url[:120]}...")

req = urllib.request.Request(url, method="GET")
req.add_header("User-Agent", "MIoT/Android APP/com.xiaomi.mihome APPV/10.5.201")
req.add_header("Accept-Encoding", "identity")

try:
    resp = opener.open(req, timeout=30)
    body = resp.read().decode()
    print(f"  HTTP: {resp.status}")
    print(f"  Cookies after Step 1: {len(list(jar))} cookies")
    for c in list(jar):
        print(f"    {c.name}: {c.value[:20]}... @ {c.domain}")
except Exception as e:
    print(f"  {C.R}❌ Failed: {e}{C.N}")
    sys.exit(1)

# Trim Xiaomi's &&&START&&& wrapper (matches Go's trimPrefix).
if body.startswith("&&&START&&&"):
    body = body[len("&&&START&&&"):]

result = json.loads(body)
print(f"  QR URL: {result.get('qr','')[:80]}...")
print(f"  Login URL: {result.get('loginUrl','')[:80]}...")
print(f"  LP URL: {result.get('lp','')[:80]}...")
print(f"  Timeout: {result.get('timeout','?')}")

qr_image_url = result.get("qr")
login_url = result.get("loginUrl")
lp_url = result.get("lp")
timeout = min(result.get("timeout", 120), 120)

# ──────────────────────────────────────────────────────────────────────────────
# Step 2: Download QR image (matches Go Start() → Step 2)
# ──────────────────────────────────────────────────────────────────────────────
print(f"\n{C.B}═══ Step 2: Download QR Image ═══{C.N}")

try:
    req2 = urllib.request.Request(qr_image_url, method="GET")
    resp2 = opener.open(req2, timeout=30)
    img_data = resp2.read()
    print(f"  HTTP: {resp2.status} | Size: {len(img_data)} bytes")
    print(f"  Cookies after Step 2: {len(list(jar))} cookies")
except Exception as e:
    print(f"  {C.R}❌ Failed: {e}{C.N}")
    img_data = b""

# ──────────────────────────────────────────────────────────────────────────────
# Step 3: Print QR art (same as Go's terminal rendering)
# ──────────────────────────────────────────────────────────────────────────────
print(f"\n{C.B}═══ Step 3: QR Code ═══{C.N}")

if img_data:
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(img_data))
        gray = img.convert("L")
        w, h = gray.size
        # Simple half-block render (dark terminal bg).
        out_w = 60
        scale = max(1, w // out_w)
        pixels = gray.load()
        art = "\n"
        for y in range(0, h, scale * 2):
            for x in range(0, w, scale):
                top = pixels[min(x, w-1), min(y, h-1)] if y < h else 255
                bot = pixels[min(x, w-1), min(y + scale, h-1)] if y + scale < h else 255
                tb = top < 128
                bb = bot < 128
                if tb and bb:   art += " "
                elif tb and not bb: art += "▄"
                elif not tb and bb: art += "▀"
                else:          art += "█"
            art += "\n"
        print(art)
    except ImportError:
        print(f"  {C.Y}⚠️  PIL not installed — can't render QR. Install: pip3 install Pillow{C.N}")
else:
    print(f"  {C.R}No QR image data{C.N}")

print(f"\n  {C.Y}📱 Scan the QR above with Mi Home app (Profile → top-right → Scan){C.N}")
print(f"  {C.CYAN}🔗 Or open: {login_url}{C.N}")

# ──────────────────────────────────────────────────────────────────────────────
# Step 4: Poll LP endpoint (matches Go's longPoll)
# ──────────────────────────────────────────────────────────────────────────────
print(f"\n{C.B}═══ Step 4: Long-Poll for Scan ═══{C.N}")
print(f"  Polling LP URL every 2s (timeout={timeout}s)...")
print(f"  Cookies before poll: {len(list(jar))} cookies")
for c in list(jar):
    print(f"    {c.name}: {c.value[:20]}... @ {c.domain} (path={c.path})")

start = time.time()
poll_count = 0
scanned = False

while time.time() - start < timeout:
    poll_count += 1
    elapsed = time.time() - start

    try:
        req_lp = urllib.request.Request(lp_url, method="GET")
        # The opener will automatically send matching cookies from the jar.
        resp_lp = opener.open(req_lp, timeout=15)  # 15s timeout, retry; LP server controls
        lp_body = resp_lp.read().decode()

        print(f"  [{elapsed:.0f}s] Poll #{poll_count}: HTTP {resp_lp.status}")

        if resp_lp.status == 200:
            if lp_body.startswith("&&&START&&&"):
                lp_body = lp_body[len("&&&START&&&"):]

            poll_result = json.loads(lp_body)
            print(f"    Response: {json.dumps(poll_result, indent=4)[:500]}")
            print(f"\n  {C.G}✅ Scan detected!{C.N}")
            print(f"    userId: {poll_result.get('userId')}")
            print(f"    ssecurity: {poll_result.get('ssecurity','')[:20]}...")
            print(f"    cUserId: {poll_result.get('cUserId')}")
            print(f"    location: {poll_result.get('location','')[:80]}...")

            scanned = True
            # Save for Step 5.
            scan_result = poll_result
            break

        elif resp_lp.status == 404 or resp_lp.status == 500:
            print(f"    {C.R}LP endpoint returned {resp_lp.status} — session may be invalid{C.N}")
            print(f"    Body: {lp_body[:200]}")
            break

        # Non-200, non-error: keep polling.
    except (urllib.error.HTTPError, Exception) as e:
        print(f"  [{elapsed:.0f}s] Poll #{poll_count}: HTTP {e.code}")
    except Exception as e:
        err_str = str(e)[:80]
        print(f"  [{elapsed:.0f}s] Poll #{poll_count}: {C.Y}{err_str}{C.N}")

    time.sleep(2)

if not scanned:
    print(f"\n  {C.R}⏰ Timeout — scan not detected after {timeout}s{C.N}")
    print(f"  Total polls: {poll_count}")
    sys.exit(1)

# ──────────────────────────────────────────────────────────────────────────────
# Step 5: Exchange for service token (matches Go's exchangeServiceToken)
# ──────────────────────────────────────────────────────────────────────────────
print(f"\n{C.B}═══ Step 5: Exchange for Service Token ═══{C.N}")

location = scan_result.get("location", "")
user_id = scan_result.get("userId", "")
ssecurity = scan_result.get("ssecurity", "")
c_user_id = scan_result.get("cUserId", "")
pass_token = scan_result.get("passToken", "")

if not location:
    location = "https://account.xiaomi.com/pass/serviceLogin?sid=xiaomiio&_json=true"
    print(f"  No location, using fallback: {location}")

# Set auth cookies on the jar for the location domain.
from urllib.parse import urlparse
loc_domain = urlparse(location).hostname or "account.xiaomi.com"

if ssecurity:
    jar.set_cookie(http.cookiejar.Cookie(
        version=0, name="ssecurity", value=ssecurity,
        port=None, port_specified=False, domain=loc_domain,
        domain_specified=True, domain_initial_dot=False, path="/",
        path_specified=True, secure=True, expires=None,
        discard=False, comment=None, comment_url=None, rest={},
    ))
    jar.set_cookie(http.cookiejar.Cookie(
        version=0, name="userId", value=user_id,
        port=None, port_specified=False, domain=loc_domain,
        domain_specified=True, domain_initial_dot=False, path="/",
        path_specified=True, secure=True, expires=None,
        discard=False, comment=None, comment_url=None, rest={},
    ))

# Also set on .xiaomi.com for account.xiaomi.com cookies.
jar.set_cookie(http.cookiejar.Cookie(
    version=0, name="ssecurity", value=ssecurity,
    port=None, port_specified=False, domain=".xiaomi.com",
    domain_specified=True, domain_initial_dot=True, path="/",
    path_specified=True, secure=True, expires=None,
    discard=False, comment=None, comment_url=None, rest={},
))
jar.set_cookie(http.cookiejar.Cookie(
    version=0, name="userId", value=user_id,
    port=None, port_specified=False, domain=".xiaomi.com",
    domain_specified=True, domain_initial_dot=True, path="/",
    path_specified=True, secure=True, expires=None,
    discard=False, comment=None, comment_url=None, rest={},
))
if c_user_id:
    jar.set_cookie(http.cookiejar.Cookie(
        version=0, name="cUserId", value=c_user_id,
        port=None, port_specified=False, domain=".xiaomi.com",
        domain_specified=True, domain_initial_dot=True, path="/",
        path_specified=True, secure=True, expires=None,
        discard=False, comment=None, comment_url=None, rest={},
    ))
if pass_token:
    jar.set_cookie(http.cookiejar.Cookie(
        version=0, name="passToken", value=pass_token,
        port=None, port_specified=False, domain=".xiaomi.com",
        domain_specified=True, domain_initial_dot=True, path="/",
        path_specified=True, secure=True, expires=None,
        discard=False, comment=None, comment_url=None, rest={},
    ))

print(f"  Cookies after setting auth: {len(list(jar))}")

try:
    req_ex = urllib.request.Request(location, method="GET")
    resp_ex = opener.open(req_ex, timeout=30)
    ex_body = resp_ex.read().decode()
    print(f"  HTTP: {resp_ex.status}")
    print(f"  Final URL: {resp_ex.url}")

    if ex_body.startswith("&&&START&&&"):
        ex_body = ex_body[len("&&&START&&&"):]

    try:
        ex_result = json.loads(ex_body)
        print(f"  Response: {json.dumps(ex_result, indent=4)[:300]}")
    except json.JSONDecodeError:
        print(f"  Body (first 200): {ex_body[:200]}")

    # Extract serviceToken from cookies.
    service_token = ""
    for c in list(jar):
        if c.name == "serviceToken":
            service_token = c.value
            print(f"\n  {C.G}✅ serviceToken: {c.value[:20]}...{C.N}")

    if not service_token:
        print(f"\n  {C.R}❌ No serviceToken in cookies!{C.N}")
        sys.exit(1)

    # Also get userId from cookies.
    final_user_id = ""
    for c in list(jar):
        if c.name == "userId":
            final_user_id = c.value
    print(f"  userId: {final_user_id}")

except Exception as e:
    print(f"  {C.R}❌ Exchange failed: {e}{C.N}")
    sys.exit(1)

# ──────────────────────────────────────────────────────────────────────────────
# Step 6: List devices (matches Go's DeviceList)
# ──────────────────────────────────────────────────────────────────────────────
print(f"\n{C.B}═══ Step 6: List Devices ═══{C.N}")

api_url = f"https://{'cn' if REGION == 'cn' else REGION}.api.io.mi.com/app/device/all_list"
if REGION == "cn":
    api_url = "https://api.io.mi.com/app/device/all_list"

req_body = json.dumps({
    "data": {
        "getVirtualModel": False,
        "getHuamiDevices": 0,
        "get_splitTv": False,
        "support_smart_home": True,
    }
}).encode()

req_dev = urllib.request.Request(api_url, data=req_body, method="POST")
req_dev.add_header("Content-Type", "application/json")
req_dev.add_header("User-Agent", "MIoT/Android")
req_dev.add_header("x-xiaomi-protocal-flag-cli", "PROTOCAL-HTTP2")
req_dev.add_header("Accept-Encoding", "identity")
# Add cookie header manually.
cookie_str = f"userId={final_user_id}; serviceToken={service_token}"
req_dev.add_header("Cookie", cookie_str)

try:
    resp_dev = opener.open(req_dev, timeout=30)
    dev_body = resp_dev.read().decode()
    dev_result = json.loads(dev_body)
    print(f"  HTTP: {resp_dev.status} | Code: {dev_result.get('code')}")

    if dev_result.get("code") == 0:
        devices = dev_result.get("result", {}).get("list", [])
        print(f"\n  {C.G}📋 {len(devices)} device(s):{C.N}")
        for d in devices:
            print(f"    {d.get('name','?'):<25} {d.get('model','?'):<25} IP={d.get('localip','?')} DID={d.get('did','?')}")
            if d.get("token"):
                print(f"      Token: {d['token']}")
    else:
        print(f"  {C.R}Error: {dev_result.get('message', '?')}{C.N}")

except Exception as e:
    print(f"  {C.R}❌ Failed: {e}{C.N}")

# ──────────────────────────────────────────────────────────────────────────────
print(f"\n{C.B}═══ Test Complete ═══{C.N}")
print(f"  If Step 4 returns non-200: the LP endpoint may need different session handling.")
print(f"  If Step 5 fails: the service token exchange may need different cookie domains.")
print(f"  If Step 6 works: the device list API is accessible.")