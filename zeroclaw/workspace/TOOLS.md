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
  multi-agent VLM loop with a Planner → Operator → Validator → Checker safety net.
- `mobile-control` is a lighter-weight single-app alternative. Never use raw `adb shell`
  to open apps or tap the screen — either skill handles that better.

### artemis-control-v2 invocation (zeroclaw-safe)

```bash
# From workspace root, by path (no `cd`, bare python3):
python3 skills/artemis-control-v2/artemis_agent.py --instruction "<task>" --profile flash --daemon
# → {"type":"run_started","log_file":"<LOG>",…}
adb shell sleep 30; tail -n 80 <LOG> | grep '"type"'   # wait + read; repeat until {"type":"result"}
```

### mobile-control invocation (fallback)

```bash
python3 skills/mobile-control/mobile_agent.py --instruction "<task>"
```

With explicit device (multi-device setups):
```bash
python3 skills/mobile-control/mobile_agent.py \
    --instruction "Open WeChat and send hello to Mom" \
    --device "emulator-5554"
```

### Pre-flight (both skills)

```bash
adb devices                                     # confirm device is connected
adb shell dumpsys power | grep -i mWakefulness  # check screen state FIRST
adb shell input keyevent 26                     # wake ONLY if Dozing/Asleep (this key toggles)
```

### ZeroClaw shell-policy gotchas (BLOCKED — don't try them)

- `cd …` — not in `allowed_commands`; run scripts by path instead
- `/opt/clawboard/venv/bin/python3` — `/opt` is in `forbidden_paths`; use bare `python3`
- `python3 -c/-m …` — inline code is blocked (`is_args_safe`)
- host `sleep` / `timeout` / `nohup` / `setsid` / `ps` / `pgrep` / `ss` — not allowlisted
- `> file` / `cat > file` / `tee` — file redirects blocked; use the skill's `--log-file`
- **Wait via `adb shell sleep N`; read via `tail`/`grep`/`cat`** — those are the only safe ones
- `file_read` on a **PNG/screenshot** — loads binary bytes into your context → token overflow.
  Verify screens with text (`adb shell uiautomator dump` + `grep`) or `image_info` (metadata only).
- `adb pull` of screenshots/XML into the workspace root — skill files stay under the skill dir
  (`skills/artemis-control-v2/screenshots/`); pulling binaries to the root is what made them
  tempting to `file_read`.

### When NOT to Use

- No device connected (`adb devices` returns empty) — tell the user, don't try
- File transfer — use `adb pull` / `adb push` instead
- iOS devices — ADB doesn't work on iPhones

---
*Add whatever helps you do your job. This is your cheat sheet.*
