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

## Phone UI Automation — artemis-control-v2 (primary) / mobile-control (fallback)

- **For phone UI tasks use the artemis-control-v2 skill** — it wraps the Artemis
  multi-agent VLM loop (Planner → Operator → Validator → Checker) with progress
  narration, loop detection, and permission-auto-tapping. It runs detached and
  survives the shell-tool timeout via `--daemon`.
- `mobile-control` is a lighter-weight single-app fallback. Never use raw `adb shell`
  to open apps or tap the screen — either skill handles that better.

### artemis-control-v2 invocation (zeroclaw-safe golden path)

```bash
python3 skills/artemis-control-v2/artemis_agent.py --instruction "<task>" --profile flash --daemon
# → {"type":"run_started","log_file":"<LOG>",…}
adb shell sleep 30; tail -n 80 <LOG> | grep '"type"'   # repeat until {"type":"result"}
```

### mobile-control invocation (fallback)

```bash
python3 skills/mobile-control/mobile_agent.py --instruction "<task>"
```

### Pre-flight (both skills)

```bash
adb devices                                     # confirm device is connected
adb shell dumpsys power | grep -i mWakefulness  # check screen state FIRST
adb shell input keyevent 26                     # wake ONLY if Dozing/Asleep (this key toggles)
```

### ZeroClaw shell-policy gotchas (BLOCKED — don't try them)

- `cd …` — not in `allowed_commands`; run scripts by path
- `/opt/clawboard/venv/bin/python3` — `/opt` is in `forbidden_paths`; use bare `python3`
- `python3 -c/-m …` — inline code is blocked
- host `sleep` / `timeout` / `nohup` / `setsid` / `ps` / `pgrep` / `ss` — not allowlisted
- `> file` / `cat > file` / `tee` — file redirects blocked; use the skill's `--log-file`
- **Wait via `adb shell sleep N`; read via `tail`/`grep`/`cat`** — those are the only safe ones
- `file_read` on a **PNG/screenshot** — binary bytes flood your context → token overflow.
  Verify screens with text (`adb shell uiautomator dump` + `grep`) or `image_info` (metadata only).
- `adb pull` of screenshots/XML into the workspace root — skill files stay under the skill dir
  (`skills/artemis-control-v2/screenshots/`); don't pull binaries to the repo root.

### When NOT to Use

- No device connected (`adb devices` returns empty) — tell the user, don't try
- File transfer — use `adb pull` / `adb push` instead
- iOS devices — ADB doesn't work on iPhones

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

### Token Refresh Fallback (Two-Phase QR Login)

When `halite_control.py health` reports `cloud_authed: false`, device control fails with token errors, or the user explicitly asks to "login with xxxclaw" / "刷新小米Token":

This is a two-phase process using the **Xiaomi-Token-Extractor** skill. Phase 1 exits fast (~2s) so the QR is fresh when the user scans.

**Phase 1 — Extract QR (exits ~2s):**
```bash
python3 skills/Xiaomi-Token-Extractor/scripts/extract_tokens.py --server cn
```
→ Parses `QR_IMAGE_URL` (show to user), `SESSION_FILE`, and `QR_COLLECT_CMD` (save for Phase 2)
→ Show user: "Open **[QR_IMAGE_URL]** on your phone, scan with Mi Home app"
→ ⚠️ NEVER pass `--interactive` — it blocks for 120s and QR expires before agent sees output

**Phase 2 — Collect tokens (blocking, foreground):**
```bash
# Run the EXACT QR_COLLECT_CMD from Phase 1 output:
python3 skills/Xiaomi-Token-Extractor/scripts/extract_tokens.py --collect /tmp/qr_session_xxxx.json
```
→ ⚠️ MUST use blocking/foreground exec — NOT background/spawn
→ Long-polls until user scans; emits `DEVICE=` JSON lines and `DONE count=N`

**Import into ha-lite:**
```bash
# Collect all DEVICE= JSON lines, wrap in array, POST to ha-lite:
curl -s -X POST http://localhost:8090/api/devices/import \
  -H 'Content-Type: application/json' \
  -d '[<all DEVICE JSON objects>]'
```

**If timeout** (`STATUS=login_timeout`): tell user session expired, re-run from Phase 1.

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
