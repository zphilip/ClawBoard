---
name: mobile-control
description: |
  Controls a PHYSICAL ANDROID PHONE connected via USB — NOT the PC, NOT the desktop, NOT a web browser.
  This skill operates the phone screen step-by-step: it takes a screenshot, sends it to a local VLM
  (GUI-Owl on port 8810), gets back one action (tap/swipe/type/scroll), executes it via ADB, then
  repeats. Each step emits a progress JSON line that you MUST narrate to the user in real time.

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
    --adb_path "$(which adb)"

# With specific device (multi-device setup)
python3 mobile_agent.py \
    --instruction "Open WeChat and send hello to Mom" \
    --adb_path "$(which adb)" \
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
| `--max_steps`   | `20`                          | Hard cap on agent steps (prevents infinite loops)        |
| `--timeout`     | `120`                         | Seconds before the whole run is killed                   |
| `--add_info`    | `""`                          | Extra context injected into the instruction              |
| `--dry_run`     | `false`                       | Only run pre-checks, skip model inference                |
| `--debug`       | `false`                       | Print full model input/output to log                     |
| `--log-file`    | `""` (stderr only)            | Also write all `[mobile-control]` logs to this file      |

### Exit Codes

| Code | Meaning                                   |
| ---- | ----------------------------------------- |
| `0`  | Task completed (FINISH detected)          |
| `1`  | Runtime error (ADB lost, model crash)     |
| `2`  | Clarification needed (vague instruction)  |
| `3`  | Timeout or step limit reached             |
| `4`  | ADB device not found                      |

## Agent Narration Guide

**This is a multi-step skill. You MUST narrate each step to the user as it happens.**

For every `{"type": "progress", ...}` line received:
- Tell the user what action was taken: _"Step 3 — tapped the WeChat icon"_
- If a screenshot path is included, mention you can show it
- Do NOT wait for the final result before talking to the user

For the final `{"type": "result", ...}` line:
- Report the outcome: success / timeout / error
- List key actions taken from the `actions` array
- If status is `clarify`, ask the user for more details

Example narration:
> "I've started controlling your phone. Step 1 — opened the home screen. Step 2 — tapped the WeChat icon. Step 3 — tapped the search bar. Step 4 — typed '妈妈'. Step 5 — tapped the contact. Step 6 — typed the message. Step 7 — tapped Send. Task complete in 7 steps."

## Output Format

The script emits **newline-delimited JSON (JSONL)** to stdout. There are two line types:

### Progress lines (one per step)

Emitted in real time as each step executes. After each action the wrapper waits ~1 second and
captures a **verification screenshot** via `adb exec-out screencap -p`, saving it to
`mobile-control/screenshots/step_NNN_<action>.png`. **Narrate these to the user** so they can
follow along:

```json
{"type": "progress", "step": 3, "action": "click [160, 376]", "message": "Step 3: click [160, 376]", "screenshot": "/path/to/skills/mobile-control/screenshots/step_003_click_160_376_.png"}
{"type": "progress", "step": 4, "action": "type \"搜索\"", "message": "Step 4: type \"搜索\"", "screenshot": "/path/to/skills/mobile-control/screenshots/step_004_type___搜索___.png"}
{"type": "progress", "step": 5, "action": "finish", "message": "Step 5: Task finished ✓", "screenshot": "/path/to/skills/mobile-control/screenshots/step_005_finish.png"}
```

**How to narrate:** After each progress line, tell the user what the agent just did.  
Example: _"Step 3 — tapped [160, 376]. Step 4 — typed '搜索'. Done!"_  
If a `screenshot` path is present, the agent can display it to confirm the result visually.

### Result line (final, always last)

```json
{
  "type": "result",
  "status": "success|timeout|error|clarify",
  "steps": 7,
  "last_action": "click [160, 376]",
  "message": "Task completed in 7 steps.",
  "actions": ["open 百度地图", "click [160,376]", "swipe ..."]
}
```

Parse with: `python3 mobile_agent.py ... | grep '"type":"result"' | python3 -c "import sys,json; r=json.load(sys.stdin); print(r['message'])"`

## Clarification Logic

If the instruction is too vague (matches patterns like "open it", "打开那个", "go there",
"帮我操作"), the script exits with code `2` and prints:

```json
{"status": "clarify", "message": "Please specify: which app and what action?"}
```

OpenClaw should then ask the user: "Which app do you want me to open, and what should I do in it?"

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
