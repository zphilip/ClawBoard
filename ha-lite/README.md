# 🏠 HA Lite Server

**Ultra-lightweight Xiaomi smart home control server for Raspberry Pi Zero 2 W, built for AI agents.**

20MB RAM | Pure Go | No Python | No multimedia | Cloud token auto-sync

## Why HA Lite?

- **512MB RAM is enough.** Traditional Home Assistant / micloud MCP Server need 4GB+.
- **AI-first design.** Exposes OpenClaw-compatible tool schemas so your AI assistant can control devices directly.
- **Local control, cloud sync.** Devices are controlled via local UDP (sub-30ms latency). Xiaomi cloud is only used to refresh tokens/IPs when they expire.

## Quick Start

### 1. Configure

Edit `halite.yaml` or set environment variables:

```bash
export HALITE_XIAOMI_USERNAME="your_xiaomi_phone"
export HALITE_XIAOMI_PASSWORD="your_xiaomi_password"
export HALITE_XIAOMI_REGION="cn"
```

### 2. Run

```bash
# Development (from source)
go run .

# Or build and run
make build
./halite
```

### 3. Verify

```bash
curl http://localhost:8090/api/health
curl http://localhost:8090/api/devices
curl http://localhost:8090/openclaw/schema | jq
```

### 4. Control a device

```bash
curl -X POST http://localhost:8090/api/control \
  -H "Content-Type: application/json" \
  -d '{"did":"12345678","action":"on"}'
```

## Cross-Compile for Raspberry Pi

### Prerequisites

```bash
# On your dev machine (x86_64 → ARM64)
go install golang.org/dl/go1.24.0@latest
# Or just use the built-in cross-compilation:
```

### Build

```bash
# Build for Pi Zero 2 W (ARM64)
make pi-zero

# Copy to Pi
scp halite-arm64 pi@raspberrypi.local:/home/pi/ha-lite/halite
scp halite.yaml pi@raspberrypi.local:/home/pi/ha-lite/
```

### Install as systemd service

```bash
# On the Pi:
sudo cp halite.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable halite
sudo systemctl start halite

# Check status:
systemctl status halite
journalctl -u halite -f
```

## OpenClaw Integration

### Method 1: HTTP Tool (recommended)

In `~/.openclaw/openclaw.json`:

```json
{
  "agents": {
    "defaults": {
      "tools": [
        {
          "type": "http",
          "config": {
            "schemaUrl": "http://<pi-ip>:8090/openclaw/schema",
            "executionUrl": "http://<pi-ip>:8090/api/control",
            "method": "POST"
          }
        }
      ]
    }
  }
}
```

### Method 2: Skill File

Create `~/.openclaw/skills/ha-lite/SKILL.md`:

```markdown
---
name: ha-lite
description: Control Xiaomi smart home devices via HA Lite server.
metadata:
  openclaw:
    emoji: "🏠"
---

# HA Lite Device Control

Use the HA Lite server at `http://<pi-ip>:8090` to control Xiaomi devices.

## Control a device

POST /api/control with JSON body `{"did": "<device-id>", "action": "<action>"}`

Actions: `on`, `off`, `toggle`, `brightness:<0-100>`, `color_temp:<2700-6500>`

## List devices

GET /api/devices

## Force sync

POST /api/sync
```

## API Reference

### `GET /openclaw/schema`
Returns the AI agent tool schema with device capabilities.

### `GET /api/devices`
List all registered devices.

### `GET /api/devices/:did`
Get a single device's info.

### `POST /api/control`
Control a device. Body: `{"did": "...", "action": "on|off|toggle|brightness:N|..."}`

### `POST /api/sync`
Force a cloud sync to refresh tokens and IPs.

### `GET /api/health`
Server health check.

## How Token Auto-Refresh Works

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
       │                   │                     │
       │   ... later, device reboots, token changes ... │
       │                   │                     │
       │ POST /api/control │                     │
       │──────────────────▶│                     │
       │                   │ UDP set_properties  │
       │                   │────────────────────▶│
       │                   │    ◀─── 401 ────    │
       │                   │                     │
       │                   │ Xiaomi Cloud Login  │
       │                   │─────────────────────│
       │                   │    ◀── new token ── │
       │                   │                     │
       │                   │ UDP set_properties  │
       │                   │ (with new token)    │
       │                   │────────────────────▶│
       │                   │    ◀─── OK ─────    │
       │    ◀── success ── │                     │
```

## Memory Footprint

Tested on Raspberry Pi Zero 2 W (512MB RAM):

| Component | Memory |
|-----------|--------|
| Go runtime + HTTP server | ~8MB |
| Device registry (10 devices) | ~2MB |
| Cloud client (cookies + buffers) | ~3MB |
| Network buffers | ~2MB |
| **Total resident** | **~15-18MB** |

## 2FA / Captcha

If your Xiaomi account has 2FA enabled, the first login will fail with a message containing a verification URL. You have two options:

1. **Temporarily disable 2FA** for the sync account (recommended for dedicated smart home accounts).
2. **Use a dedicated Xiaomi account** without 2FA that shares devices via Mi Home family sharing.

## Hardware Requirements

| Requirement | Minimum | Recommended |
|-------------|---------|-------------|
| RAM | 256MB | 512MB |
| CPU | ARMv6 | ARMv8 (Pi Zero 2 W) |
| Storage | 10MB | 50MB |
| Network | WiFi/LAN | LAN (lower latency) |

## License

MIT — see [LICENSE](/workspace/ClawBoard/LICENSE) for details.