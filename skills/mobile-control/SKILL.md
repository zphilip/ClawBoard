---
name: mobile-control
description: |
  Controls a PHYSICAL ANDROID PHONE connected via USB — NOT the PC, NOT the desktop, NOT a web browser.
  This skill operates the phone screen step-by-step: it takes a screenshot, sends it to a local VLM
  (GUI-Owl on port 8810), gets back one action (tap/swipe/type/scroll), executes it via ADB, then
  repeats.

  **Response format:** Emits JSONL to stdout — `{"type":"progress",...}` lines for each step
  (narrate these in real time) followed by a final `{"type":"result",...}` line.  All diagnostic
  logs go to stderr.  Read stdout line-by-line, parse JSON, dispatch on `"type"`.

  USE when: user says "on my phone", "open [app]", "send WeChat message", "set alarm", "navigate home",
  "search in [app]", "play music on phone", "take a screenshot of my phone", or any task that requires
  physically interacting with Android UI.

  DO NOT USE for: PC/desktop tasks, web browsing on the host machine, iOS (iPhone/iPad), file transfers
  (use adb pull/push), anything that doesn't require tapping the phone screen.
homepage: https://github.com/mPLUG-org/GUI-Owl
metadata:
  {
    "openclaw":
      {
        "emoji": "📱",
        "os": ["linux", "darwin", "win32"],
        "requires": { "bins": ["adb", "python3"] },
        "install":
          [
            {
              "id": "adb-linux",
              "kind": "shell",
              "label": "Install ADB (apt)",
              "script": "sudo apt-get install -y android-tools-adb",
              "bins": ["adb"],
              "os": ["linux"],
            },
            {
              "id": "adb-mac",
              "kind": "brew",
              "formula": "android-platform-tools",
              "bins": ["adb"],
              "label": "Install ADB (brew)",
              "os": ["darwin"],
            },
            {
              "id": "python-deps",
              "kind": "shell",
              "label": "Install Python dependencies into ClawBoard venv",
              "script": "/opt/clawboard/venv/bin/pip install openai Pillow numpy",
              "os": ["linux"],
            },
          ],
      },
  }
---

# mobile-control — Android Phone Control via GUI-Owl

> **THIS SKILL CONTROLS A PHYSICAL ANDROID PHONE, NOT THE PC.**  
> It works by taking a phone screenshot → asking a local VLM what to tap → executing the tap via ADB → repeating.
> The task takes **multiple steps** (typically 5–30). After each step a progress JSON is emitted — **narrate
> each step to the user so they know what the agent is doing on their phone.**

> **ClawBerry / pi-gen images**: `android-tools-adb`, `openai`, `Pillow`, `numpy` are pre-installed.
> No manual installation needed on ClawBerry OS.
>
> **Other Linux / macOS hosts**: install manually (see Pre-flight section below).

The agent loop:
1. Takes a screenshot of the phone via ADB
2. Sends screenshot + instruction to GUI-Owl VLM (local llama.cpp, port 8810)
3. VLM returns one action: `click`, `swipe`, `type`, `scroll`, `open`, `finish`, etc.
4. Action is executed via ADB
5. Emits a `{"type":"progress", ...}` JSON line — **you must narrate this to the user**
6. Repeat until `finish` action or step limit reached
7. Emits a final `{"type":"result", ...}` JSON line

## When to Use

✅ **USE this skill when:**

- "Open WeChat / 打开微信"
- "Set an alarm for 7am tomorrow"
- "Search for flights on Ctrip"
- "Send a message to [contact]"
- "Navigate home on Baidu Maps / 导航回家"
- Any instruction involving hands-free phone interaction

## When NOT to Use

❌ **DON'T use this skill when:**

- The phone is not connected via USB (`adb devices` returns empty)
- User wants to read a file from the phone → use `adb pull`
- iOS device (ADB does not work on iPhones)
- Instruction is too vague with no app context → ask for clarification first

## Pre-flight Checks (always run before invoking)

On ClawBerry OS the ADB server is managed by `adb-server.service` and starts
automatically on boot. Enable it once:

```bash
sudo systemctl enable --now adb-server
```

Check status:

```bash
systemctl status adb-server
```

If you are not using the service (or need a manual reset):

```bash
# Kill any stale server and start fresh
sudo adb kill-server && sudo adb start-server
```

Then:

```bash
# 1. Confirm device is connected (should show a device serial)
adb devices

# 2. Wake screen if off
adb shell input keyevent 26

# 3. Force ADB keyboard (required for reliable text input)
adb shell ime set com.android.adbkeyboard/.AdbIME
```

## Invoke the Skill

```bash
# cd to the skill directory first
cd /path/to/ClawBoard/skills/mobile-control

# Basic usage — uses local GUI-Owl server on port 8810
python3 mobile_agent.py \
    --instruction "打开百度地图,导航回家" \
    --adb_path adb

# With specific device (multi-device setup)
python3 mobile_agent.py \
    --instruction "Open WeChat and send hello to Mom" \
    --adb_path adb \
    --device "emulator-5554"

# With custom model endpoint (override port 8810)
python3 mobile_agent.py \
    --instruction "Search for weather in Beijing on Baidu" \
    --base_url "http://localhost:8810/v1" \
    --model "gui-owl"

# Dry run — only checks ADB + screen state, no model calls
python3 mobile_agent.py \
    --instruction "dummy" \
    --dry_run

# Debug mode — writes full log to /tmp/mobile_agent.log
python3 mobile_agent.py \
    --instruction "打开设置" \
    --debug \
    --log-file /tmp/mobile_agent.log

# Watch the log live in another terminal
tail -f /tmp/mobile_agent.log
```

### CLI Parameters

| Parameter       | Default                       | Description                                              |
| --------------- | ----------------------------- | -------------------------------------------------------- |
| `--instruction` | *(required)*                  | Natural language task for the agent                      |
| `--adb_path`    | `adb`                         | Full path to ADB binary                                  |
| `--device`      | *(auto-detect)*               | ADB device serial (for multi-device setups)              |
| `--base_url`    | `http://localhost:8810/v1`    | OpenAI-compatible endpoint of the GUI-Owl server         |
| `--model`       | `gui-owl`                     | Model name as registered in the llama.cpp server         |
| `--api_key`     | `not-needed`                  | API key (leave default for local llama.cpp)              |
| `--max_steps`   | `30`                          | Hard cap on agent steps (prevents infinite loops)        |
| `--timeout`     | `120`                         | Seconds before the whole run is killed                   |
| `--add_info`    | `""`                          | Extra context injected into the instruction              |
| `--dry_run`     | `false`                       | Only run pre-checks, skip model inference                |
| `--debug`       | `false`                       | Print full model input/output to log                     |
| `--log-file`    | `""` (stderr only)            | Also write all `[mobile-control]` logs to this file      |

### Exit Codes

| Code | `status`   | Meaning                                                   |
| ---- | ---------- | --------------------------------------------------------- |
| `0`  | `success`  | Task completed (runner or supervisor confirmed)           |
| `1`  | `error`    | Runtime error (ADB lost, model crash, runner not found)   |
| `2`  | `clarify`  | Clarification needed (vague instruction)                  |
| `3`  | `timeout`  | Timeout or step limit reached without completion          |
| `4`  | `error`    | ADB device not found                                      |

Always check `result.status` (not just exit code) — a timeout may carry extra context in
`result.debug.end_reason` (e.g. `"hard_timeout_watchdog"` vs `"max_steps_reached"`).

## Response Dispatch (for the calling agent)

**Stream separation:** The script writes ONLY JSON to **stdout**. ALL diagnostic logs
(step markers, timing, model output, supervisor verdicts) go to **stderr**.  Read stdout
line-by-line, parse each line as JSON, and dispatch on the `"type"` field:

| `"type"`      | Meaning | Action |
|---------------|---------|--------|
| `"progress"`  | One step executed | Narrate `message` to the user; attach `screenshot` if present |
| `"result"`    | End of run | Read `status` → report outcome; read `message` for summary |
| *(missing)*   | **Should not happen** (all emissions now include `"type"`) | Fall back to `obj.get("status")` |

**All terminal emissions** (success, error, timeout, clarify, dry-run, runner-not-found,
no-device) now include `"type": "result"`.  The agent can safely check `obj["type"]`
on every line to decide whether it is a progress update or the final verdict.

## Agent Narration Guide

**This is a multi-step skill. You MUST narrate each step to the user as it happens.**

For every `{"type": "progress", ...}` line received:
- Tell the user what action was taken: _"Step 3 — tapped the WeChat icon"_
- If a `screenshot` path is included, you can display it to confirm the result visually
- Do NOT wait for the final result before talking to the user
- If `message` starts with `[plan replay]`, the step was replayed from a cached plan (no VLM call)

For the final `{"type": "result", ...}` line:
- Report the outcome: success / timeout / error
- List key actions taken from the `actions` array
- If `status` is `"clarify"`, ask the user for more details
- If `status` is `"timeout"` but `debug.end_reason` says `"hard_timeout_watchdog"`,
  tell the user the runner hung and was forcibly killed
- If `status` is `"error"` and `debug.end_reason` says `"runner_exited_immediately rc=0 step=0"`,
  tell the user the runner crashed on startup

Example narration:
> "I've started controlling your phone. Step 1 — opened the home screen. Step 2 — tapped the WeChat icon. Step 3 — tapped the search bar. Step 4 — typed '妈妈'. Step 5 — tapped the contact. Step 6 — typed the message. Step 7 — tapped Send. Task complete in 7 steps."

## Output Format

The script emits **newline-delimited JSON (JSONL)** to **stdout**.  Everything else goes to
**stderr** — the agent should ignore stderr entirely.  There are exactly two line types:

### Progress lines (`"type": "progress"`)

Emitted in real time as each step executes.  After each action the wrapper waits ~1.2 s and
captures a **verification screenshot** via `adb exec-out screencap -p`, saving it to
`mobile-control/screenshots/run_<timestamp>/step_NNN_<action>.png`:

```json
{
  "type": "progress",
  "step": 3,
  "action": "click [160, 376]",
  "message": "Step 3: click [160, 376]  [Foreground: com.baidu.BaiduMap (百度地图)]",
  "screenshot": "/home/zero/ClawBoard/skills/mobile-control/screenshots/run_20260611_165809/step_003_click_160_376_.png"
}
```

| Field | Type | Notes |
|-------|------|-------|
| `step` | int | Zero-based step counter |
| `action` | string | Short label: `click [x,y]`, `type "text"`, `open "app"`, `swipe`, `finish`, `answer "..."` |
| `message` | string | **Use this for narration.** Includes `[plan replay]` prefix when step was cached. Ends with `[Foreground: <app>]` when the foreground app is known. |
| `screenshot` | string? | Absolute path to a verification PNG, or `null` if capture failed. **The screenshot is ephemeral** — it is deleted after the run completes. Read and display it before the result line arrives. |

### Result line (`"type": "result"`)

Always the **last line** on stdout.  Emitted for every exit path (success, timeout, error,
clarify, dry-run, pre-flight failures):

```json
{
  "type": "result",
  "status": "success",
  "steps": 6,
  "last_action": "answer \"已为您导航至万象城，全程2.6公里...\"",
  "message": "Task completed in 6 steps. [reason: runner_terminated_completed]",
  "actions": [
    "open \"百度地图\"",
    "click [122, 488]",
    "type \"万象城 \"",
    "click [271, 184]",
    "click [827, 937]",
    "answer \"...\""
  ],
  "debug": {
    "end_reason": "runner_terminated_completed",
    "rc": 0,
    "finish_pattern": "",
    "runner_termination_reason": "answer_confirmed_complete",
    "last_runner_line": "[CLEANUP] Screenshot directories removed."
  }
}
```

| Field | Type | Notes |
|-------|------|-------|
| `status` | string | `"success"` / `"timeout"` / `"error"` / `"clarify"` |
| `steps` | int | Total steps executed (0 for pre-flight failures) |
| `last_action` | string | The final action taken, or `""` if none |
| `message` | string | Human-readable summary. **Use this for the final report to the user.** Includes `[reason: ...]` suffix. |
| `actions` | string[] | Ordered list of every action taken (for summarisation) |
| `debug` | object | Diagnostic details (see below) |

### `debug` object reference

| Key | Values | Meaning |
|-----|--------|---------|
| `end_reason` | `runner_terminated_completed` | Runner explicitly signalled task completion (best signal) |
| | `runner_nonzero_exit rc=N` | Runner crashed with non-zero exit code |
| | `runner_exited_immediately rc=0 step=0` | Runner exited instantly with no output — startup crash |
| | `runner_exit_without_completion rc=0` | Runner exited cleanly but never signalled completion |
| | `hard_timeout_elapsed=N limit=M` | Wall-clock timeout reached (output was still flowing) |
| | `hard_timeout_watchdog limit=N` | Watchdog killed a hung subprocess that produced no output |
| | `finish_pattern_matched:<p>` | Mobile-agent wrapper detected a finish pattern in runner output |
| | `max_steps_reached (N)` | Step budget exhausted without completion |
| `rc` | int | Runner subprocess exit code (`-1` if could not be determined) |
| `runner_termination_reason` | string | The value after `[TERMINATION REASON]` in runner output, if any |
| `finish_pattern` | string | Which regex matched (empty if no pattern hit) |
| `last_runner_line` | string | Last line of runner output (capped at 200 chars) |

### Error / pre-flight result examples

**No ADB device** (exit code 4):
```json
{"type": "result", "status": "error", "steps": 0, "last_action": "", "message": "No ADB device found. ...", "actions": []}
```

**Vague instruction** (exit code 2):
```json
{"type": "result", "status": "clarify", "steps": 0, "last_action": "", "message": "Instruction is too vague. ...", "actions": []}
```

**Dry run OK** (exit code 0):
```json
{"type": "result", "status": "success", "steps": 0, "last_action": "", "message": "Dry run OK — device ... ready.", "actions": []}
```

**Runner not found** (exit code 1):
```json
{"type": "result", "status": "error", "steps": 0, "last_action": "", "message": "run_gui_owl_1_5_for_mobile.py not found. ...", "actions": []}
```

All of these now include `"type": "result"` so the agent can dispatch them the same way as
a normal completion — check `status` for the outcome, use `message` for the user.

## Clarification Logic

If the instruction is too vague (matches patterns like "open it", "打开那个", "go there",
"帮我操作"), the script exits with code `2` and prints:

```json
{"type": "result", "status": "clarify", "steps": 0, "last_action": "", "message": "Instruction is too vague. Please specify: which app and what action? Example: '在微信发消息给妈妈说我到家了'", "actions": []}
```

The calling agent should then ask the user: "Which app do you want me to open, and what should I do in it?"

## Loop Detection

If the agent clicks the same coordinate 3+ times in a row with no new action types, the
wrapper injects an additional hint into the next model call:
`"The previous action did not change the screen. Try a different approach."`
and notifies the user: *"Agent is retrying with a different strategy..."*

## ADB Keyboard Setup (one-time)

For reliable text input, install the ADB Keyboard APK on the device:

```bash
# Download and install
wget https://github.com/senzhk/ADBKeyBoard/releases/download/v2.0/ADBKeyboard.apk
adb install ADBKeyboard.apk

# Enable it
adb shell ime set com.android.adbkeyboard/.AdbIME

# Verify
adb shell settings get secure default_input_method
# Should return: com.android.adbkeyboard/.AdbIME
```

## Permission Dialog Auto-handler

The wrapper detects common system permission dialogs by checking for UI elements via
`uiautomator dump` and automatically taps "Allow" / "允许":

```bash
# Manual check
adb shell uiautomator dump /sdcard/ui.xml && adb pull /sdcard/ui.xml /tmp/ui.xml
grep -i "allow\|允许\|grant" /tmp/ui.xml
```

## Troubleshooting

```bash
# Device not found? Restart the ADB server service:
sudo systemctl restart adb-server && adb devices
# Or manually:
sudo adb kill-server && sudo adb start-server && adb devices

# See full execution log:
cat /tmp/mobile_agent.log
# Or watch live:
tail -f /tmp/mobile_agent.log

# See systemd logs for the skill run:
sudo journalctl -u openclaw -n 50 --no-pager | grep mobile

# ADB keyboard not working?
adb shell ime list -a | grep -i adb
adb shell ime set com.android.adbkeyboard/.AdbIME

# Model server not responding?
curl -s http://localhost:8810/health

# Kill a stuck run
pkill -f run_gui_owl_1_5_for_mobile.py
```
