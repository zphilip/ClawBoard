---
name: xiaomi-home
description: "[English] Control Xiaomi Home devices via ha-lite REST API. Supports on/off, brightness, color temperature, status queries, cloud sync, and scheduled operations. | [中文] 通过 ha-lite REST API 控制米家智能设备。支持开关、亮度、色温、状态查询、云端同步和定时操作。"
metadata: {"clawdbot":{"emoji":"🏠","requires":{"bins":["python3"]},"install":[{"id":"halite-control","kind":"exec","command":"pip3 install requests --break-system-packages 2>/dev/null; pip3 install requests --user 2>/dev/null; echo 'Ready'","label":"Install Python dependencies for halite_control.py"}]}}
---

# Xiaomi Home Control 🏠 | 小米家居控制

[English] | [中文](#中文说明)

---

## English

Control Xiaomi (Mi Home) smart home devices through **ha-lite** — a lightweight Go server
that provides a REST API for device discovery, control, and cloud sync.

### 🚀 Features

- **Device Control**: On/off, toggle, brightness (0-100%), color temperature (2700-6500K)
- **Auto-Discovery**: Devices automatically synced from Xiaomi Cloud via QR or OAuth login
- **Online Detection**: Real miIO reachability probing — knows which devices are actually online
- **Status Queries**: Read current power state, brightness, and color temperature
- **Scheduled Operations**: Turn devices on/off at specific times or on a schedule
- **Multi-Device Scenes**: "Goodnight" turns off all lights, "Good morning" turns on the coffee maker
- **Friendly Names**: Control devices by name — no need to remember DIDs, IPs, or tokens
- **Cloud Sync**: One-click token refresh when device IPs change

### 🛠️ Setup

1. **Ensure ha-lite is running** on a device on your local network (typically a Raspberry Pi):
   ```bash
   # Check if ha-lite is reachable
   python3 scripts/halite_control.py health
   ```

2. **Login to Xiaomi Cloud** (one-time setup):
   ```bash
   # Option A: QR code login (scan with Mi Home app)
   python3 scripts/halite_control.py login qr

   # Option B: OAuth browser login (no phone needed)
   python3 scripts/halite_control.py login oauth
   ```

3. **Verify devices are synced**:
   ```bash
   python3 scripts/halite_control.py list
   ```

4. **Optional**: Store device details in `references/my_private_devices.md` as a backup.
   Run `python3 scripts/token_extractor.py` to export tokens directly from Xiaomi Cloud.

### 📋 Quick Reference

```bash
# Discovery
halite_control.py list                          # All devices with status
halite_control.py list --online                 # Only online devices
halite_control.py list --category lights        # Only lights
halite_control.py categories                    # Category summary

# Control
halite_control.py on "Living Room Light"        # Turn on
halite_control.py off "热水器"                   # Turn off
halite_control.py toggle "Bedroom Fan"         # Toggle
halite_control.py brightness "Desk Lamp" 75    # Set brightness
halite_control.py color_temp "Desk Lamp" 4000  # Set color temp

# Status
halite_control.py status "热水器"               # Single device status
halite_control.py status --all                 # Online/offline summary

# Server
halite_control.py health                       # Server health check
halite_control.py sync                         # Force cloud sync
```

### 🤖 Natural Language Intents

When the user gives a command in natural language, map it to the appropriate
`halite_control.py` call.

#### Device Discovery

| User Intent | Command |
|:---|:---|
| "What devices do I have?" | `python3 scripts/halite_control.py list` |
| "Which devices are online?" | `python3 scripts/halite_control.py list --online` |
| "Show me all lights" | `python3 scripts/halite_control.py list --category lights` |
| "List all my switches" | `python3 scripts/halite_control.py list --category switch` |
| "What categories do I have?" | `python3 scripts/halite_control.py categories` |
| "Is the server running?" | `python3 scripts/halite_control.py health` |

#### Power Control

| User Intent | Command |
|:---|:---|
| "Turn on [device]" | `python3 scripts/halite_control.py on "[device]"` |
| "Turn off [device]" | `python3 scripts/halite_control.py off "[device]"` |
| "Toggle [device]" | `python3 scripts/halite_control.py toggle "[device]"` |
| "Switch on [device]" | `python3 scripts/halite_control.py on "[device]"` |
| "Power off [device]" | `python3 scripts/halite_control.py off "[device]"` |
| "Turn on the water heater" | `python3 scripts/halite_control.py on "热水器"` |
| "Turn off all lights" | **See Scenes below** |

#### Light Control

| User Intent | Command |
|:---|:---|
| "Set [device] brightness to X%" | `python3 scripts/halite_control.py brightness "[device]" X` |
| "Dim [device]" | `python3 scripts/halite_control.py brightness "[device]" 25` |
| "Max brightness [device]" | `python3 scripts/halite_control.py brightness "[device]" 100` |
| "Make [device] warmer" | `python3 scripts/halite_control.py color_temp "[device]" 3000` |
| "Make [device] cooler" | `python3 scripts/halite_control.py color_temp "[device]" 5000` |
| "Set [device] to daylight" | `python3 scripts/halite_control.py color_temp "[device]" 5500` |

#### Status Queries

| User Intent | Command |
|:---|:---|
| "What's the status of [device]?" | `python3 scripts/halite_control.py status "[device]"` |
| "Is [device] on?" | `python3 scripts/halite_control.py status "[device]"` |
| "How many devices are online?" | `python3 scripts/halite_control.py status --all` |
| "Check if [device] is working" | `python3 scripts/halite_control.py status "[device]"` |

#### Maintenance

| User Intent | Command |
|:---|:---|
| "Sync devices from cloud" | `python3 scripts/halite_control.py sync` |
| "Refresh device tokens" | `python3 scripts/halite_control.py sync` |
| "Login to Xiaomi" | `python3 scripts/halite_control.py login qr` |
| "Check server health" | `python3 scripts/halite_control.py health` |

### 🎬 Scenes (Multi-Device Routines)

Scenes are multi-device operations. When the user invokes a scene, call the
appropriate commands in sequence.

**"Goodnight" / "晚安"** — Turn off all lights and switches:
```bash
# Get all online lights and switches, turn them off
python3 scripts/halite_control.py list --online --category lights | grep "DID:" | \
  while read -r line; do
    did=$(echo "$line" | grep -oP 'DID: \K\S+')
    [ -n "$did" ] && curl -s -X POST http://localhost:8090/api/control \
      -H 'Content-Type: application/json' -d "{\"did\":\"$did\",\"action\":\"off\"}"
  done
```

**"Good morning" / "早上好"** — Turn on designated morning devices:
```bash
python3 scripts/halite_control.py on "热水器"
python3 scripts/halite_control.py on "Living Room Light"
```

**"Leaving home" / "出门"** — Turn off everything:
```bash
# Get all online devices, turn them off
python3 scripts/halite_control.py list --online | grep "DID:" | \
  while read -r line; do
    did=$(echo "$line" | grep -oP 'DID: \K\S+')
    [ -n "$did" ] && curl -s -X POST http://localhost:8090/api/control \
      -H 'Content-Type: application/json' -d "{\"did\":\"$did\",\"action\":\"off\"}"
  done
```

### ⏰ Scheduled Operations

To schedule a device to turn on/off at a specific time, use `at` (Linux/macOS):

```bash
# Turn on water heater at 7:00 AM
echo "python3 scripts/halite_control.py on '热水器'" | at 07:00

# Turn off living room light at 11:00 PM
echo "python3 scripts/halite_control.py off 'Living Room Light'" | at 23:00
```

For recurring schedules, add a cron job:
```bash
# Turn on water heater every morning at 7am
0 7 * * * cd /path/to/skills/xiaomi-home && python3 scripts/halite_control.py on "热水器"

# Turn off all lights every night at 11pm
0 23 * * * cd /path/to/skills/xiaomi-home && python3 scripts/halite_control.py off "Living Room Light"
```

### 🔧 Fallback: Direct Device Control

If ha-lite is unavailable, you can still control devices directly via `miiocli`
using the tokens from `references/my_private_devices.md`:

```bash
# Power on/off
miiocli miotdevice --ip <IP> --token <TOKEN> raw_command set_properties \
  '[{"siid": 2, "piid": 1, "value": true}]'

# Query status
miiocli miotdevice --ip <IP> --token <TOKEN> raw_command get_properties \
  '[{"siid": 2, "piid": 1}]'
```

See `references/capabilities.md` for per-model MIoT property IDs.

### 📁 File Reference

| File | Purpose |
|:---|:---|
| `scripts/halite_control.py` | Primary CLI for ha-lite REST API |
| `scripts/token_extractor.py` | Fallback: extract tokens directly from Xiaomi Cloud |
| `references/capabilities.md` | Per-device-category action reference and MIoT fallback |
| `references/devices.md` | Device registry template |
| `references/my_private_devices.md` | Your private device IPs and tokens (DO NOT PUBLISH) |

---

## 中文说明

通过 **ha-lite** 轻量级 Go 服务器控制小米（米家）智能家居设备。
ha-lite 提供 REST API，支持设备发现、控制和云端同步。

### 🚀 核心特性

- **设备控制**：开关、切换、亮度调节（0-100%）、色温调节（2700-6500K）
- **自动发现**：通过二维码或 OAuth 登录从小米云自动同步设备
- **在线检测**：真正的 miIO 可达性探测 — 准确知道哪些设备在线
- **状态查询**：读取当前电源状态、亮度、色温
- **定时操作**：在指定时间或按计划开关设备
- **多设备场景**："晚安"关闭所有灯，"早上好"打开热水器
- **友好名称**：按设备名称控制 — 无需记住 DID、IP 或 Token
- **云端同步**：设备 IP 变化时一键刷新 Token

### 🛠️ 快速开始

1. **确保 ha-lite 正在运行**（通常在树莓派上）：
   ```bash
   python3 scripts/halite_control.py health
   ```

2. **登录小米云**（一次性设置）：
   ```bash
   # 方式 A：二维码登录（用米家 App 扫描）
   python3 scripts/halite_control.py login qr

   # 方式 B：OAuth 浏览器登录（无需手机）
   python3 scripts/halite_control.py login oauth
   ```

3. **验证设备已同步**：
   ```bash
   python3 scripts/halite_control.py list
   ```

4. **备选方案**：将设备信息存入 `references/my_private_devices.md` 作为备份。
   运行 `python3 scripts/token_extractor.py` 直接从小米云导出 Token。

### 🤖 自然语言指令映射

#### 设备发现

| 用户意图 | 命令 |
|:---|:---|
| "我有哪些设备？" | `python3 scripts/halite_control.py list` |
| "哪些设备在线？" | `python3 scripts/halite_control.py list --online` |
| "显示所有灯" | `python3 scripts/halite_control.py list --category lights` |
| "列出所有开关" | `python3 scripts/halite_control.py list --category switch` |
| "服务器在运行吗？" | `python3 scripts/halite_control.py health` |

#### 电源控制

| 用户意图 | 命令 |
|:---|:---|
| "打开 [设备]" | `python3 scripts/halite_control.py on "[设备]"` |
| "关闭 [设备]" | `python3 scripts/halite_control.py off "[设备]"` |
| "切换 [设备]" | `python3 scripts/halite_control.py toggle "[设备]"` |
| "打开热水器" | `python3 scripts/halite_control.py on "热水器"` |
| "关掉所有灯" | **见下方场景部分** |

#### 灯光控制

| 用户意图 | 命令 |
|:---|:---|
| "把 [设备] 亮度调到 X%" | `python3 scripts/halite_control.py brightness "[设备]" X` |
| "调暗 [设备]" | `python3 scripts/halite_control.py brightness "[设备]" 25` |
| "把 [设备] 调到最亮" | `python3 scripts/halite_control.py brightness "[设备]" 100` |
| "把 [设备] 调暖一点" | `python3 scripts/halite_control.py color_temp "[设备]" 3000` |
| "把 [设备] 调冷一点" | `python3 scripts/halite_control.py color_temp "[设备]" 5000` |

#### 状态查询

| 用户意图 | 命令 |
|:---|:---|
| "[设备] 的状态？" | `python3 scripts/halite_control.py status "[设备]"` |
| "[设备] 开着吗？" | `python3 scripts/halite_control.py status "[设备]"` |
| "有多少设备在线？" | `python3 scripts/halite_control.py status --all` |

#### 维护

| 用户意图 | 命令 |
|:---|:---|
| "从云端同步设备" | `python3 scripts/halite_control.py sync` |
| "刷新设备 Token" | `python3 scripts/halite_control.py sync` |
| "登录小米" | `python3 scripts/halite_control.py login qr` |

### 🎬 场景（多设备联动）

**"晚安"** — 关闭所有灯和开关：
```bash
python3 scripts/halite_control.py list --online --category lights | grep "DID:" | \
  while read -r line; do
    did=$(echo "$line" | grep -oP 'DID: \K\S+')
    [ -n "$did" ] && curl -s -X POST http://localhost:8090/api/control \
      -H 'Content-Type: application/json' -d "{\"did\":\"$did\",\"action\":\"off\"}"
  done
```

**"早上好"** — 打开早晨需要的设备：
```bash
python3 scripts/halite_control.py on "热水器"
python3 scripts/halite_control.py on "Living Room Light"
```

**"出门"** — 关闭所有设备：
```bash
python3 scripts/halite_control.py list --online | grep "DID:" | \
  while read -r line; do
    did=$(echo "$line" | grep -oP 'DID: \K\S+')
    [ -n "$did" ] && curl -s -X POST http://localhost:8090/api/control \
      -H 'Content-Type: application/json' -d "{\"did\":\"$did\",\"action\":\"off\"}"
  done
```

### ⏰ 定时操作

使用 `at` 命令（Linux/macOS）定时执行：

```bash
# 早上 7:00 打开热水器
echo "python3 scripts/halite_control.py on '热水器'" | at 07:00

# 晚上 11:00 关闭客厅灯
echo "python3 scripts/halite_control.py off 'Living Room Light'" | at 23:00
```

使用 cron 实现重复定时：
```bash
# 每天早上 7 点打开热水器
0 7 * * * cd /path/to/skills/xiaomi-home && python3 scripts/halite_control.py on "热水器"
```

### 🔧 备选方案：直接控制设备

如果 ha-lite 不可用，可以使用 `references/my_private_devices.md` 中的 Token 通过 `miiocli` 直接控制：

```bash
miiocli miotdevice --ip <IP> --token <TOKEN> raw_command set_properties \
  '[{"siid": 2, "piid": 1, "value": true}]'
```

详见 `references/capabilities.md` 了解各型号的 MIoT 属性 ID。

---

## 🔗 Links | 相关链接
- **ClawdHub**: [https://www.clawhub.ai/s/xiaomi-home](https://www.clawhub.ai/s/xiaomi-home)
- **GitHub**: [https://github.com/Pegasus02/clawdbot-xiaomi-home](https://github.com/Pegasus02/clawdbot-xiaomi-home)

Developed with 🦞 by **@Pegasus02** · Extended with ha-lite integration