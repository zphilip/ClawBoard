---
name: artemis-control
description: |
  Controls a PHYSICAL ANDROID PHONE connected via USB — powered by Artemis (Google Research).
  This skill wraps Artemis's multi-agent graph engine: Planner → Operator → Validator → Checker.
  Supports two profiles: Flash (fast ~3-5s/step reactive loop) and Pro (full planning + safety net
  + checkpoint verification ~15-40s/step).

  **Key differentiators vs mobile-control:**
  - Pre-execution safety net: XML hierarchy check + pixel VLM fallback before every tap
  - Execution incidents: structured failure tracking with self-healing across turns
  - Explorer element indexer: references elements by index from the accessibility tree
  - History compression: visual summaries replace old screenshots; search_history tool
  - Structured final verification: Checker audits plan milestones against the original goal
  - Click sequence batching: handles transient UI (toasts, auto-dismissing controls)

  **Response format:** Emits JSONL to stdout — `{"type":"progress",...}` lines for each step
  (narrate these in real time) followed by a final `{"type":"result",...}` line. All diagnostic
  logs go to stderr.

  USE when: user says "on my phone", "open [app]", "send WeChat message", "set alarm",
  "navigate home", "search in [app]", "play music on phone", "take a screenshot of my phone",
  or any task that requires physically interacting with Android UI.

  DO NOT USE for: PC/desktop tasks, web browsing on the host machine, iOS (iPhone/iPad),
  file transfers (use adb pull/push), anything that doesn't require tapping the phone screen.
homepage: https://github.com/google/artemis
metadata:
  {
    "openclaw":
      {
        "emoji": "🏹",
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
          ],
      },
  }
---

# artemis-control — Android Phone Control via Artemis

> **THIS SKILL CONTROLS A PHYSICAL ANDROID PHONE, NOT THE PC.**
> It wraps Google Research's Artemis multi-agent engine: take a screenshot → index UI elements
> → ask a VLM what to do → validate with safety net → execute via ADB → verify → repeat.
> The task takes **multiple steps** (typically 3–50). After each step a progress JSON is emitted —
> **narrate each step to the user so they know what the agent is doing on their phone.**

## Architecture

```
  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
  │ Planner  │───▶│ Operator │───▶│Validator │───▶│ Checker  │
  │ (plan)   │    │ (decide) │    │(safety)  │    │(verify)  │
  └──────────┘    └──────────┘    └──────────┘    └──────────┘
       │               │               │               │
       └───────────────┴───────┬───────┴───────────────┘
                               │
                    ┌──────────┴──────────┐
                    │   Safety Net (XML   │
                    │   first, pixel      │
                    │   fallback)         │
                    └─────────────────────┘
```

### Execution Profiles

| Feature | Flash | Pro |
|---------|-------|-----|
| Speed | ~3–5s/step | ~15–40s/step |
| Planning | None | Markdown task plan with milestones |
| Safety net | None | XML + pixel validation per action |
| Verification | None | Checkpoint + final review |
| History | Visual summaries | Full transcript ledger + chunking |
| Best for | Routine tasks | Complex multi-app workflows |

## When to Use

✅ **USE this skill when:**

- "Open WeChat / 打开微信"
- "Set an alarm for 7am tomorrow"
- "Search for flights on Ctrip"
- "Send a message to [contact]"
- "Navigate home on Baidu Maps / 导航回家"
- Long-horizon multi-app workflows ("Open Maps, route home, then text ETA to Mom")
- Any instruction involving hands-free phone interaction

## When NOT to Use

❌ **DON'T use this skill when:**

- The phone is not connected via USB (`adb devices` returns empty)
- User wants to read a file from the phone → use `adb pull`
- iOS device (ADB does not work on iPhones)
- Simple tasks that don't need the phone screen → use mobile-control for lighter weight

## Pre-flight Checks (always run before invoking)

```bash
# 1. Confirm device is connected
adb devices

# 2. Wake screen if off
adb shell input keyevent 26

# 3. Check Artemis is importable
python3 -c "import artemis; print(artemis.__version__)" || echo "Install: cd /workspace/artemis && uv sync"
```

## Invoke the Skill

```bash
# cd to the skill directory first
cd /path/to/ClawBoard/skills/artemis-control

# Basic usage — Flash profile (fast reactive, no plan)
python3 artemis_agent.py \
    --instruction "打开百度地图,导航回家" \
    --profile flash

# Pro profile (full planning + safety net + verification)
python3 artemis_agent.py \
    --instruction "Open Settings, go to Battery, check percentage, take screenshot" \
    --profile pro

# With verification level
python3 artemis_agent.py \
    --instruction "Install the APK, log in, verify no crash dialogs" \
    --profile pro \
    --verification final

# With specific device
python3 artemis_agent.py \
    --instruction "Open WeChat and send hello to Mom" \
    --device "emulator-5554"

# Dry run — only checks ADB + Artemis health
python3 artemis_agent.py \
    --instruction "dummy" \
    --dry_run
```

### CLI Parameters

| Parameter          | Default          | Description |
|--------------------|------------------|-------------|
| `--instruction`    | *(required)*     | Natural language task |
| `--profile`        | `flash`          | Execution profile: `flash` or `pro` |
| `--device`         | *(auto-detect)*  | ADB device serial |
| `--max_steps`      | `50`             | Hard cap on steps |
| `--timeout`        | `900`            | Seconds before kill |
| `--verification`   | `final`          | `off` / `final` / `checkpoints` / `strict` |
| `--explorer`       | `pro`            | Explorer tier: `flash` / `pro` / `ultra` |
| `--dry_run`        | `false`          | Only run pre-checks |
| `--debug`          | `false`          | Verbose logging |
| `--log-file`       | `""`             | Also write logs to file |

### Exit Codes

| Code | `status`   | Meaning |
|------|-----------|---------|
| `0`  | `success`  | Task completed (Checker confirmed) |
| `1`  | `error`    | Runtime error (ADB lost, model crash) |
| `2`  | `clarify`  | Clarification needed |
| `3`  | `timeout`  | Timeout or step limit reached |
| `4`  | `error`    | ADB device not found |

## Response Dispatch (for the calling agent)

**Stream separation:** The script writes ONLY JSON to **stdout**. ALL diagnostic logs go to **stderr**.

| `"type"`      | Meaning | Action |
|---------------|---------|--------|
| `"progress"`  | One step executed | Narrate `message`; attach `screenshot` if present |
| `"incident"`  | Safety net blocked an action | Note the incident; agent will self-heal |
| `"result"`    | End of run | Read `status` → report outcome; read `message` for summary |

### Progress lines (`"type": "progress"`)

```json
{
  "type": "progress",
  "step": 3,
  "action": "tap [160, 376]",
  "message": "Step 3: tap Baidu Maps icon [Foreground: com.baidu.BaiduMap]",
  "screenshot": "/path/to/screenshots/run_xxx/step_003_tap.png",
  "verified": true,
  "safety_check": "xml_passed"
}
```

### Incident lines (`"type": "incident"`)

```json
{
  "type": "incident",
  "step": 5,
  "category": "target_shifted",
  "reason": "Button moved from [200,500] to [210,505]; self-healed",
  "action": "tap",
  "consecutive_failures": 0
}
```

### Result line (`"type": "result"`)

```json
{
  "type": "result",
  "status": "success",
  "steps": 8,
  "last_action": "finish",
  "message": "Task completed in 8 steps. Verification: 3/3 checkpoints passed.",
  "actions": ["open \"Baidu Maps\"", "tap [122,488]", "type \"Home\"", ...],
  "verification": {
    "passed": 3,
    "failed": 0,
    "inconclusive": 0
  },
  "debug": {
    "end_reason": "checker_confirmed",
    "profile": "pro",
    "explorer_tier": "pro"
  }
}
```

## Safety Net (Pre-Execution Validation)

Every `tap`, `long_press`, and `input_text` action in Pro mode passes through:

1. **XML hierarchy check** (fast, deterministic):
   - Reads live UI tree
   - Matches target element by resource_id (0.5), text (0.4), bounds (0.3), coordinates (0.3)
   - Retries 3× with 0.4s delay for screen transitions
   - Self-heals small coordinate drifts

2. **Pixel VLM check** (fallback):
   - Crops target region from decision-time vs live screenshot
   - VLM compares: is the target still present/visible/interactive?

3. **Incident tracking**:
   - Blocked action → `ExecutionIncident` stays in context until next successful action
   - Consecutive failures counted; no separate repair agent needed

## Explorer (Element Locating)

The Explorer indexes the accessibility tree and combines:
- **ScreenIndex**: Resource IDs, text, content descriptions → element indices
- **OCR fusion**: Merged bounding boxes for custom-drawn UIs
- **Visual perception** (ultra tier): VLM-based object detection for Canvas/Flutter

Tiers: `flash` (1-turn visual), `pro` (3-turn loop + UI tree), `ultra` (8-turn deep search).

The Operator references elements by index number; coordinates are the fallback.

## Context Management

- **Visual summaries**: Old screenshots replaced with async-generated text summaries
- **search_history tool**: Keyword/step-range lookup over raw records (no vector DB)
- **Session-relative timestamps**: `T+mm:ss` clock shared across all agents
- **Transcript ledger**: Append-only, byte-stable history with scrub edges

## Element Indexing Guide

When the Operator sees `[42] Baidu Maps`, it taps `target: 42` — the index is resolved
against the current UI tree. Index-addressed actions carry observed metadata
(`target_text`, `target_resource_id`, `target_bounds`); coordinate-addressed actions
carry `target_description` (the model's own statement).

## Troubleshooting

```bash
# Device not found?
adb kill-server && adb start-server && adb devices

# Artemis not importable?
cd /workspace/artemis && uv sync && uv run python -c "import artemis"

# See full execution log:
cat /tmp/artemis_agent.log

# Kill a stuck run:
pkill -f artemis_agent.py
```