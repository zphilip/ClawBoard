# ha-lite Device Control — Solution Analysis

## Device Classification

Based on extensive testing with python-miio reference implementation:

| Category | Models | Local UDP | Cloud API |
|----------|--------|-----------|-----------|
| **WiFi devices** | zhimi.fan.v2, chuangmi.plug.m1, roborock.vacuum.t6, etc. | ✅ handshake + set_power | ✅ OAuth Bearer |
| **Zigbee devices (with IP)** | philips.light.downlight, lumi.* sensors | ❌ No miIO server on port 54321 | ✅ OAuth Bearer |
| **BLE devices (no IP)** | cleargrass.sensor_ht, miaomiaoce.*, lumi.flood | ❌ No IP | ✅ OAuth Bearer |
| **Gateway devices** | lumi.gateway.v3, lumi.gateway.mgl03 | ✅ (gateway itself) | ✅ OAuth Bearer |

## Why Philips Lights Have IP But Can't Be Controlled Locally

Philips lights (`philips.light.downlight`) connect via **Zigbee** to a Xiaomi Gateway.
The IP shown in the device list is the gateway's IP, not the light's own IP.
They do not run a miIO server — the gateway handles all communication.
Python-miio confirms: `DeviceException: Unable to discover the device <IP>`.

## Implementation Plan

### Phase 1: Local UDP Control (DONE — v0.5.0)
- miIO handshake (miIO.info hello → get device_id → send command)
- set_power method (matches python-miio)
- Works for: WiFi devices with IP

### Phase 2: Cloud OAuth Control (PLANNED)
- OAuth 2.0 flow using HA's client_id=2882303761520251711
- Bearer token → ha.api.io.mi.com/app/v2/miotspec/prop/set
- QR code to get user authorization on phone → paste code to dashboard
- refresh_token for 3-day auto-renewal

#### OAuth Flow
```
1. GET /api/oauth/url
   → Returns {"url": "https://account.xiaomi.com/oauth2/authorize?...", "qr": "data:image/png;base64,..."}

2. User scans QR with phone → opens Xiaomi login → authorizes
   → Redirects to http://homeassistant.local:8123/callback?code=XXXX
   → Browser can't resolve → user copies code from URL bar

3. POST /api/oauth/exchange {"code": "XXXX"}
   → POST ha.api.io.mi.com/app/v2/ha/oauth/get_token
   → Saves access_token + refresh_token to cache/oauth_token.json

4. POST /api/control {"did":"...","action":"on"}
   → Local UDP first (WiFi devices)
   → Fallback: OAuth Bearer → ha.api.io.mi.com/app/v2/miotspec/prop/set
```

#### Required Headers for Cloud Control
```
Content-Type: application/json
Authorization: Bearer{access_token}
X-Client-BizId: haapi
X-Client-AppId: 2882303761520251711
```

#### Control Endpoints
```
GET PROP:  POST /app/v2/miotspec/prop/get   {"datasource":1,"params":[{"did","siid","piid"}]}
SET PROP:  POST /app/v2/miotspec/prop/set   {"params":[{"did","siid","piid","value"}]}
ACTION:    POST /app/v2/miotspec/action      {"params":{"did","siid","aiid","in":[]}}
DEVICE LIST: POST /app/v2/home/device_list_page  {"limit":200,"get_split_device":true}
```

### Phase 3: Unified Control Logic
```
controlDevice(did, action)
  ├─ Device has IP AND is WiFi model?
  │   └─ local UDP miIO → success? ✅ via: local
  ├─ Device has IP? (might be Zigbee via gateway)
  │   └─ local UDP miIO → try anyway → success? ✅ via: local
  ├─ Have OAuth token?
  │   └─ cloud API → ✅ via: cloud
  └─ ❌ No control path available
```

### Phase 4: Dashboard Integration
- OAuth login button → shows QR → code input → auto-exchange
- Device cards with cloud control toggle
- Token expiry warnings

## Test Scripts

| Script | Purpose |
|--------|---------|
| `test_miio_debug.py` | Test local UDP control with python-miio |
| `test_device_control.py` | Test device control via ha-lite API |
| `test_cloud_control.py` | Test RC4-encrypted cloud API (sid=xiaomiio) |
| `test_oauth_control.py` | Test OAuth Bearer cloud API |
| `test_miio_direct.py` | Test raw UDP miIO without ha-lite |