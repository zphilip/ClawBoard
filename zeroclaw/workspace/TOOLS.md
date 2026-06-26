# TOOLS.md — Local Notes

Skills define HOW tools work. This file is for YOUR specifics —
the stuff that's unique to your setup.

## What Goes Here

Things like:
- SSH hosts and aliases
- Device nicknames
- Preferred voices for TTS
- Anything environment-specific

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
cd skills/mobile-control && python3 mobile_agent.py --instruction "<task>"
```

With explicit device (multi-device setups):
```bash
cd skills/mobile-control && python3 mobile_agent.py \
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

---
*Add whatever helps you do your job. This is your cheat sheet.*
