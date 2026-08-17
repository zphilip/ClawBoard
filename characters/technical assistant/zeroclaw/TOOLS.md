# TOOLS.md — Local Notes

Skills define HOW tools work. This file is for YOUR specifics —
the stuff that's unique to your setup.

## What Goes Here

Things like:
- SSH hosts and aliases
- Device nicknames
- Preferred voices for TTS
- Anything environment-specific

## Python Environment

→ See AGENTS.md **"Python & Package Management"** — always use `/opt/clawboard/venv/bin/python3`

## Built-in Tools

- **shell** — Execute terminal commands
- Use when: running local checks, build/test commands, or diagnostics.
- Don't use when: a safer dedicated tool exists, or command is destructive without approval.
- **file_read** — Read file contents
- Use when: inspecting project files, configs, or logs.
- Don't use when: you only need a quick string search (prefer targeted search first).
- **file_write** — Write file contents
- Use when: applying focused edits, scaffolding files, or updating docs/code.
- Don't use when: unsure about side effects or when the file should remain user-owned.
- **memory_store** — Save to memory
- Use when: preserving durable preferences, decisions, or key context.
- Don't use when: info is transient, noisy, or sensitive without explicit need.
- **memory_recall** — Search memory
- Use when: you need prior decisions, user preferences, or historical context.
- Don't use when: the answer is already in current files/conversation.
- **memory_forget** — Delete a memory entry
- Use when: memory is incorrect, stale, or explicitly requested to be removed.
- Don't use when: uncertain about impact; verify before deleting.

## mobile-control — Phone UI Automation

- **ALWAYS use the mobile-control skill for phone UI tasks — NEVER use raw `adb shell`
  to open apps or tap the screen.** The skill provides a VLM-powered agent loop with
  progress narration, error handling, loop detection, and permission-auto-tapping that
  raw ADB cannot match.

### Invocation

```bash
cd skills/mobile-control && /opt/clawboard/venv/bin/python3 mobile_agent.py --instruction "<task>"
```

With explicit device (multi-device setups):
```bash
cd skills/mobile-control && /opt/clawboard/venv/bin/python3 mobile_agent.py \
    --instruction "Open WeChat and send hello to Mom" \
    --device "emulator-5554"
```

### Pre-flight

```bash
adb devices                          # confirm device is connected
adb shell input keyevent 26          # wake screen if off
adb shell ime set com.android.adbkeyboard/.AdbIME  # force ADB keyboard
```

### Key Parameters

| Param | Default | Notes |
|---|---|---|
| `--max_steps` | 30 | Raise for complex multi-app tasks |
| `--timeout` | 120 | Seconds before kill |
| `--debug` | false | Writes full log to /tmp/mobile_agent.log |
| `--dry_run` | false | Only check ADB + screen, no model calls |

### When NOT to Use

- No device connected (`adb devices` returns empty) — tell the user, don't try
- File transfer — use `adb pull` / `adb push` instead
- iOS devices — ADB doesn't work on iPhones

### Troubleshooting

```bash
sudo systemctl restart adb-server && adb devices   # reset ADB
tail -f /tmp/mobile_agent.log                       # watch live log
pkill -f run_gui_owl_1_5_for_mobile.py              # kill stuck run
```

## xiaomi-home — Smart Home Control

Control Xiaomi/Mi Home smart devices via ha-lite REST API on the local Pi.

- **Primary path:** `skills/Xiaomi-home-halite/scripts/halite_control.py` — wraps ha-lite's REST API
- **Server:** `http://localhost:8090` (ha-lite on Pi)
- **Fallback:** `skills/Xiaomi-Token-Extractor/scripts/extract_tokens.py` — extracts tokens from Xiaomi Cloud when ha-lite auth is broken

### Quick Reference

```bash
# Discovery
python3 skills/Xiaomi-home-halite/scripts/halite_control.py list
python3 skills/Xiaomi-home-halite/scripts/halite_control.py list --online
python3 skills/Xiaomi-home-halite/scripts/halite_control.py categories

# Control (resolves name → DID automatically)
python3 skills/Xiaomi-home-halite/scripts/halite_control.py on "热水器"
python3 skills/Xiaomi-home-halite/scripts/halite_control.py off "Living Room Light"
python3 skills/Xiaomi-home-halite/scripts/halite_control.py toggle "Bedroom Fan"
python3 skills/Xiaomi-home-halite/scripts/halite_control.py brightness "Desk Lamp" 75
python3 skills/Xiaomi-home-halite/scripts/halite_control.py color_temp "Desk Lamp" 4000

# Status
python3 skills/Xiaomi-home-halite/scripts/halite_control.py status "热水器"
python3 skills/Xiaomi-home-halite/scripts/halite_control.py status --all

# Maintenance
python3 skills/Xiaomi-home-halite/scripts/halite_control.py health
python3 skills/Xiaomi-home-halite/scripts/halite_control.py sync
```

### Health Check

```bash
curl -s http://localhost:8090/api/health
# → {"status":"ok","version":"0.11.0","device_count":50,"cloud_authed":true}
```

### Token Refresh Fallback

When `halite_control.py health` reports `cloud_authed: false` or device control fails with token errors:

1. **Extract fresh tokens from Xiaomi Cloud:**
   ```bash
   python3 skills/Xiaomi-Token-Extractor/scripts/extract_tokens.py --server cn
   ```
   → Show QR URL to user → scan with Mi Home app → collect DEVICE= lines

2. **Import tokens into ha-lite:**
   ```bash
   # Collect all DEVICE= JSON lines from extractor output, wrap in array, POST to ha-lite:
   curl -s -X POST http://localhost:8090/api/devices/import \
     -H 'Content-Type: application/json' \
     -d '[{"name":"热水器","did":"12345678","ip":"192.168.1.10","token":"abc123...","model":"cuco.plug.v3"}]'
   ```

3. **Retry the control command** — now works with fresh tokens.

### Scenes (Multi-Device)

**"晚安" / "Goodnight"** — Turn off all lights:
```bash
python3 skills/Xiaomi-home-halite/scripts/halite_control.py list --online --category lights | \
  while read -r line; do
    did=$(echo "$line" | grep -oP 'DID: \K\S+')
    [ -n "$did" ] && curl -s -X POST http://localhost:8090/api/control \
      -H 'Content-Type: application/json' -d "{\"did\":\"$did\",\"action\":\"off\"}"
  done
```

**"早上好" / "Good morning"** — Turn on morning devices:
```bash
python3 skills/Xiaomi-home-halite/scripts/halite_control.py on "热水器"
python3 skills/Xiaomi-home-halite/scripts/halite_control.py on "Living Room Light"
```

**"出门" / "Leaving home"** — Turn off everything:
```bash
python3 skills/Xiaomi-home-halite/scripts/halite_control.py list --online | \
  while read -r line; do
    did=$(echo "$line" | grep -oP 'DID: \K\S+')
    [ -n "$did" ] && curl -s -X POST http://localhost:8090/api/control \
      -H 'Content-Type: application/json' -d "{\"did\":\"$did\",\"action\":\"off\"}"
  done
```

---
*Add whatever helps you do your job. This is your cheat sheet.*
