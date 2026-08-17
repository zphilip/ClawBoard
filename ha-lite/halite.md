# ha-lite Device Control API

**Server:** `http://localhost:8090`

20MB RAM | Pure Go | Local UDP control | Cloud token auto-sync

---

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/devices` | List all devices with name, model, DID, IP, online status |
| `GET` | `/api/devices/:did` | Get a single device's info |
| `POST` | `/api/devices/import` | Import device tokens from external source (e.g., Xiaomi-Token-Extractor) |
| `POST` | `/api/control` | Send a control command to a device |
| `POST` | `/api/sync` | Force cloud sync to refresh tokens and IPs |
| `GET` | `/api/health` | Server health check + cloud auth status |
| `GET` | `/openclaw/schema` | AI agent tool schema (auto-generated from device list) |

---

## Device Control

### `POST /api/control`

**Request body:**
```json
{
  "did": "<device-id>",
  "action": "<action>"
}
```

**Success response (200):**
```json
{
  "status": "success",
  "did": "12345678",
  "name": "Living Room Light",
  "action": "on",
  "via": "local"
}
```

**Failure response (200):**
```json
{
  "status": "failed",
  "did": "12345678",
  "error": "device unreachable: timeout"
}
```

**Error responses (4xx):**
```json
{"error": "Missing required field: did"}
{"error": "Missing required field: action"}
{"error": "Device not found: <did>"}
```

### Actions by Device Type

| Device Type | Actions |
|-------------|---------|
| **All controllable** | `on`, `off`, `toggle` |
| **Lights** (light, lamp, bulb, candle, downlight, ceiling) | `on`, `off`, `toggle`, `brightness:<1-100>`, `color_temp:<2700-6500>` |
| **Curtains / Blinds** (curtain, blind, window, shade, roller) | `on`, `off`, `toggle`, `position:<0-100>`, `pause` |
| **Air Purifiers** (air, purifier, filter) | `on`, `off`, `toggle`, `fan_speed:<0-3>`, `mode:<auto,sleep,manual>` |
| **Humidifiers** (humidifier, dehumidifier) | `on`, `off`, `toggle`, `humidity:<30-80>`, `fan_speed:<0-3>` |
| **AC** (ac, aircondition, aircon, climate) | `on`, `off`, `toggle`, `temperature:<16-30>`, `mode:<cool,heat,auto,fan,dry>`, `fan_speed:<0-3>` |
| **Fans** (fan, ventilator, ventilation) | `on`, `off`, `toggle`, `fan_speed:<0-3>`, `oscillate:<on,off>` |
| **Heaters** (heater, radiator, warming) | `on`, `off`, `toggle`, `temperature:<16-30>` |
| **Robot Vacuums** (robot, vacuum, sweeper, clean) | `on` (start), `off` (stop/dock) |
| **Switches / Plugs** (plug, outlet, socket, switch, relay) | `on`, `off`, `toggle` |

The `action` string uses a `key:value` convention for parameterized commands (e.g., `brightness:75`, `color_temp:4000`, `temperature:24`). Simple actions are bare words (`on`, `off`, `toggle`). Unknown actions are passed through to the device as-is, so arbitrary MIoT commands can be sent.

### Toggle Behavior

When `toggle` is used, the server first queries the device's current power state via `get_prop` (UDP), then flips it. If the query fails, it defaults to `on`.

### Control Flow

```
POST /api/control → UDP miIO set_properties → device
                         ↓ (failure)
                   Cloud sync → retry UDP → device
                         ↓ (failure)
                   Return error
```

The server first tries local UDP control (sub-30ms latency). If that fails (e.g., token expired), it attempts a cloud sync to refresh the device token, then retries. The frontend (ClawBoard dashboard) adds an additional **3-retry layer** with 0.8s delay between attempts to handle transient UDP packet loss.

---

## Device List

### `GET /api/devices`

Returns all registered devices. By default, probes each device's online status via miIO hello handshake (2s timeout per device, concurrent). Pass `?probe=false` to skip probing.

**Response:**
```json
{
  "count": 3,
  "devices": [
    {
      "did": "12345678",
      "name": "Living Room Light",
      "model": "xiaomi.light.b1",
      "ip": "192.168.1.50",
      "token": "abc123...",
      "online": true,
      "home": "My Home"
    },
    {
      "did": "87654321",
      "name": "Bedroom Fan",
      "model": "zhimi.fan.sa1",
      "ip": "192.168.1.51",
      "token": "def456...",
      "online": true,
      "home": "My Home"
    }
  ]
}
```

### `GET /api/devices/:did`

Get a single device by its DID.

**Response:** Same device object as above, or `{"error": "Device not found: <did>"}` (404).

---

## Device Import (Token Extractor Bridge)

### `POST /api/devices/import`

Accepts device tokens from an external source (e.g., [Xiaomi-Token-Extractor](skills/Xiaomi-Token-Extractor/)) and merges them into the local registry. This is the bridge between the token extractor's output and ha-lite's device cache — use it when the extractor has fresh tokens and ha-lite needs to pick them up without a full cloud re-sync.

**Request body:** Array of device objects (matches the extractor's `devices.json` format):

```json
[
  {
    "name": "热水器",
    "did": "12345678",
    "ip": "192.168.1.10",
    "token": "181f7c047098b594883f88191a9e6c3a",
    "model": "cuco.plug.v3"
  }
]
```

Only `did` and `token` are required. `name`, `ip`, and `model` are optional (will be updated if provided).

**Success response (200):**
```json
{
  "status": "imported",
  "updated": 3,
  "total": 3
}
```

- `updated`: number of devices that were new or had changed tokens/IPs
- `total`: total devices in the import request

**Error responses:**
```json
{"error": "Invalid JSON body: expected array of device objects. ..."}
{"error": "Empty device list"}
```

**Typical flow with Xiaomi-Token-Extractor:**
```
1. extract_tokens.py --server cn          → QR login → devices.json
2. POST /api/devices/import ← devices.json → tokens merged into registry
3. halite_control.py on "热水器"           → now works with fresh token
```

---

## Health Check

### `GET /api/health`

**Response:**
```json
{
  "status": "ok",
  "version": "1.0.0",
  "device_count": 5,
  "cloud_authed": true
}
```

---

## Cloud Sync

### `POST /api/sync`

Forces a full cloud sync: re-authenticates with Xiaomi cloud (using stored credentials or OAuth token), fetches the latest device list, and updates local tokens and IPs.

**Response:**
```json
{"status": "synced", "device_count": 5}
```

On failure:
```json
{"error": "not authenticated — login first"}
```

---

## Authentication

ha-lite supports two login methods:

### QR Code Login (Mi Home app)

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/login/qr/start` | Start QR login flow, returns QR image URL + base64 data URI |
| `GET` | `/api/login/qr/status` | Check QR scan status (`waiting` → `scanned` → `has_service_token`) |
| `POST` | `/api/login/qr/collect` | Complete login, sync devices from cloud |
| `GET` | `/api/login/qr/image` | QR code PNG image |

### OAuth Login (browser-based)

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/login/oauth/start` | Start OAuth flow, returns Xiaomi auth URL |
| `GET` | `/api/login/oauth/status` | Check OAuth status (`waiting` → `authorized` → `has_service_token`) |
| `POST` | `/api/login/oauth/collect` | Complete login, sync devices from cloud |

---

## OpenClaw Schema

### `GET /openclaw/schema`

Returns an auto-generated AI agent tool schema describing all endpoints and available devices with their capabilities. Used by openclaw to register the ha-lite server as an HTTP tool.

**Response structure:**
```json
{
  "name": "ha_lite_device_control",
  "description": "Control Xiaomi smart home devices on the local network...",
  "version": "1.0.0",
  "endpoints": {
    "control": { "method": "POST", "path": "/api/control", "description": "...", "payload": {...} },
    "list_devices": { "method": "GET", "path": "/api/devices", "description": "..." },
    "sync": { "method": "POST", "path": "/api/sync", "description": "..." },
    "health": { "method": "GET", "path": "/api/health", "description": "..." },
    "qr_login": { "method": "POST", "path": "/api/login/qr/start", "description": "..." }
  },
  "devices": [
    {
      "did": "12345678",
      "name": "Living Room Light",
      "model": "xiaomi.light.b1",
      "online": true,
      "ip": "192.168.1.50",
      "capabilities": ["on", "off", "toggle", "brightness:<0-100>", "color_temp:<2700-6500>"]
    }
  ],
  "parameters": {
    "type": "object",
    "properties": {
      "did": { "type": "string", "description": "Device unique ID (DID). Get from /api/devices." },
      "action": { "type": "string", "description": "Action: 'on', 'off', 'toggle', or device-specific commands." }
    },
    "required": ["did", "action"]
  }
}
```

---

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│  OpenClaw   │────▶│  HA Lite     │────▶│  Xiaomi     │
│  AI Agent   │     │  (Pi Zero)   │     │  Device     │
└─────────────┘     └──────────────┘     └─────────────┘
       │                   │                     │
       │ POST /api/control │                     │
       │ {"did":"X",       │                     │
       │  "action":"on"}   │                     │
       │──────────────────▶│                     │
       │                   │ UDP set_properties  │
       │                   │────────────────────▶│
       │                   │    ◀─── OK ─────    │
       │    ◀── success ── │                     │
```

- **Local control:** UDP miIO protocol on port 54321, sub-30ms latency
- **Cloud sync:** Xiaomi cloud API used only for token refresh and device discovery
- **Memory:** ~15-18MB resident on Raspberry Pi Zero 2 W