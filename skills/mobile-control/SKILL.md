---
name: mobile-control
description: "Control an Android phone via ADB + local GUI-Owl multimodal model. Use when: user says 'open [app]', 'on my phone', 'send a message', 'set alarm', 'navigate to', 'search in [app]', or any hands-free phone operation. Requires ADB-connected Android device and local llama.cpp GUI-Owl server on port 8810. NOT for: iOS devices, SMS via computer SMS bridges, or tasks that can be done without touching the phone."
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
              "script": "/opt/clawboard/venv/bin/pip install qwen_agent numpy",
              "os": ["linux"],
            },
          ],
      },
  }
---

# mobile-control — Android Phone Control via GUI-Owl

> **ClawBerry / pi-gen images**: `android-tools-adb`, `qwen_agent`, and `numpy`
> are pre-installed by `stage3/00-clawberry/01-run.sh`.
> No manual installation needed on ClawBerry OS.
>
> **Other Linux / macOS hosts**: install manually (see Pre-flight section below).

Controls an ADB-connected Android device using the local `gui-owl-llamacpp` multimodal model
(running on `http://localhost:8810/v1`). The agent takes a screenshot, reasons about the UI,
and executes tap/swipe/type actions in a loop until the task is done or the step limit is hit.

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

```bash
# 1. Confirm device is connected
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

### Exit Codes

| Code | Meaning                                   |
| ---- | ----------------------------------------- |
| `0`  | Task completed (FINISH detected)          |
| `1`  | Runtime error (ADB lost, model crash)     |
| `2`  | Clarification needed (vague instruction)  |
| `3`  | Timeout or step limit reached             |
| `4`  | ADB device not found                      |

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
# Device not found?
adb kill-server && adb start-server && adb devices

# ADB keyboard not working?
adb shell ime list -a | grep -i adb
adb shell ime set com.android.adbkeyboard/.AdbIME

# Model server not responding?
curl -s http://localhost:8810/health

# Kill a stuck run
pkill -f run_gui_owl_1_5_for_mobile.py
```
