# Xiaomi Device Capability Reference

What each device category supports via ha-lite's `/api/control` endpoint.

## Action Format

All actions use the `{did, action}` JSON payload. The `did` is resolved automatically
by `halite_control.py` from the device name — you never need to type it manually.

| Action | Format | Example |
|:---|:---|:---|
| Power on | `on` | `{"did":"...", "action":"on"}` |
| Power off | `off` | `{"did":"...", "action":"off"}` |
| Toggle | `toggle` | `{"did":"...", "action":"toggle"}` |
| Brightness | `brightness:<0-100>` | `{"did":"...", "action":"brightness:75"}` |
| Color temperature | `color_temp:<2700-6500>` | `{"did":"...", "action":"color_temp:4000"}` |
| Query status | `status` | `{"did":"...", "action":"status"}` |

## Category → Capability Map

### 💡 Lights
**Models:** `xiaomi.light.*`, `yeelink.light.*`, `philips.light.*`, `lumi.light.*`
**Actions:** `on`, `off`, `toggle`, `brightness:<0-100>`, `color_temp:<2700-6500>`, `status`
**Notes:** Brightness and color temp are only available on smart bulbs and ceiling lights. Basic white bulbs may only support on/off.

### 🔌 Switches & Plugs
**Models:** `cuco.plug.*`, `chuangmi.plug.*`, `lumi.switch.*`, `lumi.relay.*`
**Actions:** `on`, `off`, `toggle`, `status`
**Notes:** These are basic on/off devices. Some smart power strips expose multiple channels (one per outlet).

### 🌀 Fans
**Models:** `zhimi.fan.*`, `dmaker.fan.*`, `leshow.fan.*`
**Actions:** `on`, `off`, `toggle`, `status`
**Notes:** Fan speed and oscillation are available via direct MIoT commands if needed, but ha-lite's basic control covers power on/off.

### 🌬️ Air Purifiers
**Models:** `zhimi.airpurifier.*`, `zhimi.airfresh.*`, `zhimi.humidifier.*`
**Actions:** `on`, `off`, `toggle`, `status`
**Notes:** Mode selection (auto/sleep/manual) and fan speed are available via direct MIoT commands.

### 🧹 Vacuums
**Models:** `roborock.vacuum.*`, `viomi.vacuum.*`, `dreame.vacuum.*`
**Actions:** `on` (start clean), `off` (stop/dock), `status`
**Notes:** `on` starts the default cleaning cycle. `off` stops and returns to dock.

### 🪟 Curtains / Blinds
**Models:** `lumi.curtain.*`, `dooya.curtain.*`
**Actions:** `on` (open), `off` (close), `toggle`, `status`
**Notes:** Position control (0-100%) is available via direct MIoT commands.

### 🏠 Appliances
**Models:** `chunmi.cooker.*` (rice cooker), `deerma.humidifier.*`, `yunmi.kettle.*`
**Actions:** `on`, `off`, `toggle`, `status`
**Notes:** These are treated as basic on/off switches. Advanced modes (cooking programs, humidity targets) require direct MIoT commands.

### 📡 Sensors (Read-only)
**Models:** `lumi.sensor_*`, `xiaomi.sensor_*`, `aqara.*`
**Actions:** `status` (read-only)
**Notes:** Temperature, humidity, motion, door/window, flood, smoke, gas sensors. No control actions — read-only status reporting.

### 📷 Cameras (Read-only)
**Models:** `xiaovv.camera.*`, `isa.camera.*`, `mijia.camera.*`
**Actions:** `status` (read-only)
**Notes:** Camera stream and PTZ control are not supported via ha-lite. Read-only device presence.

### 🌐 Gateways / Hubs
**Models:** `lumi.gateway.*`, `xiaomi.gateway.*`, `xiaomi.repeater.*`
**Actions:** `status` (read-only)
**Notes:** Zigbee gateways and WiFi repeaters. No control actions — they manage child devices.

### 🔊 Speakers
**Models:** `xiaomi.speaker.*`, `lumi.speaker.*`
**Actions:** `status` (read-only)
**Notes:** Audio playback control is not supported via ha-lite.

### 🔐 Locks
**Models:** `aqara.lock.*`, `xiaomi.lock.*`
**Actions:** `status` (read-only)
**Notes:** Lock/unlock requires security verification — not supported via ha-lite for safety.

## Direct MIoT Fallback

For capabilities not covered by ha-lite's basic actions, use `miiocli` directly:

```bash
# Fan speed (siid=2, piid for fan_level varies by model)
miiocli miotdevice --ip <IP> --token <TOKEN> raw_command set_properties \
  '[{"siid": 2, "piid": 5, "value": 3}]'

# Air purifier mode (usually siid=2, piid=4 or 5)
miiocli miotdevice --ip <IP> --token <TOKEN> set_property_by 2 4 0

# Humidifier target humidity
miiocli miotdevice --ip <IP> --token <TOKEN> raw_command set_properties \
  '[{"siid": 2, "piid": 6, "value": 60}]'
```

Get device IPs and tokens from `halite_control.py list` or `references/my_private_devices.md`.