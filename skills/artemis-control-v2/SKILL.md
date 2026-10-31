---
name: artemis-control-v2
description: |
  Controls a PHYSICAL ANDROID PHONE over USB via the Artemis (Google Research)
  multi-agent engine: Planner → Operator → Validator → Checker. Two profiles:
  Flash (fast ~3-5s/step) and Pro (full planning + safety net + verification).

  Key features: pre-execution safety net (XML + pixel VLM check), execution
  incident tracking with self-healing, accessibility element indexer, history
  compression, structured final verification.

  Response: JSONL to stdout — {"type":"progress"} lines then a final
  {"type":"result"} line; logs go to stderr.

  USE when: "on my phone", "open [app]", "send WeChat message", "set alarm",
  "navigate home", "search in [app]", "play music", "screenshot", or any task
  requiring physical Android UI interaction.

  DO NOT USE for: PC/desktop tasks, host web browsing, iOS, file transfers
  (use adb pull/push), anything that doesn't require tapping the phone screen.
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
- Simple tasks that don't need the phone screen

## Pre-flight Checks (always run before invoking)

```bash
# 1. Confirm device is connected
adb devices

# 2. Check screen state FIRST — do NOT blindly send keyevent 26: it is the
#    power-key TOGGLE, so on an already-awake screen it turns the screen OFF.
adb shell dumpsys power | grep -i mWakefulness
#    → only if it prints Dozing/Asleep, wake:
adb shell input keyevent 26

# 3. Check the Artemis runner end to end. DO NOT use `python3 -c` (zeroclaw's
#    shell policy blocks -c/-m inline code) and do NOT shell-redirect `> file`.
/opt/clawboard/venv/bin/python3 skills/artemis-control-v2/artemis_agent.py --instruction "x" --dry_run
```

Text input needs **no checks**: artemis injects text itself (uiautomator2
`set_text` → clipboard paste → `input text`). Do NOT probe or install an ADB
keyboard, do NOT run `python3 -c "import uiautomator2"`, do NOT run `ime set` —
every one of those is a wasted tool call that burns the turn budget.

## Invoke the Skill

```bash
# Basic usage — Flash profile (fast reactive, no plan).
# No `cd` needed: artemis_agent.py resolves its own dir (config/, runner/) via
# __file__. `cd` is usually NOT in zeroclaw's allowed_commands — run by path:
/opt/clawboard/venv/bin/python3 skills/artemis-control-v2/artemis_agent.py \
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

### Detached run + log polling — the zeroclaw golden path (≤12 tool calls)

zeroclaw's `shell` tool buffers stdout and returns it only at process exit, and
its timeout blocks for the whole run — so run detached and poll the log file.
**The turn budget is the enemy**: every poll costs one tool call and zeroclaw
caps a turn at `[agent] max_tool_iterations`. Keep the WHOLE task inside this
sequence (~5 pre-flight calls + ~5 poll calls):

```bash
# 5. Launch detached — returns immediately with a run_started JSON
/opt/clawboard/venv/bin/python3 skills/artemis-control-v2/artemis_agent.py \
    --instruction "打开百度地图,搜索南岸花城并开始导航" \
    --profile flash --daemon
#    → {"type":"run_started","log_file":"<LOG>",…}   ← remember <LOG>

# 6. Poll: WAIT via on-device sleep, then read. Repeat ~3-5 times; a step takes
#    ~20-100s so 30s per poll is about right. Stop at {"type":"result"}.
adb shell sleep 30; tail -n 80 <LOG> | grep '"type"'

# 7. Report the final result's status / message / steps to the user.
```

Why exactly this shape — zeroclaw's shell policy (`level="supervised"` + a
non-empty `allowed_commands`) is strict, and these are the ONLY safe patterns:

- ✅ `adb shell sleep N`  — the only way to wait (`sleep` is NOT allowlisted on the host)
- ✅ `tail -n N <LOG>` / `cat <LOG>` / `grep` / `wc` — the only readers
- ❌ `sleep` / `timeout` / `nohup` / `setsid` / `ps` / `pgrep` / `ss` — not allowlisted;
  each attempt is a wasted turn
- ❌ `python3 -c …` / `python3 -m …` — inline code is blocked
- ❌ `cat > file` / `> file` / `tee` — file redirects are blocked
- ❌ `browser` wait / `file_write` poller scripts — may be unavailable or need approval

A `--daemon` run double-forks (new session/process group), so it survives zeroclaw's
shell-tool process-group reap and is NOT bound by `[shell_tool] timeout`. Every
`{"type":"progress"}` / `{"incident"}` / `{"result"}` line is flushed to `<LOG>`
as it happens.

### Stalled run — hand-back protocol

If a `--daemon` run repeats the same `type`/`tap` and no `{"type":"result"}`
appears after ~5 polls, it is stuck (search inputs that never submit, IME
weirdness, stale element references). The calling agent may rescue it, then
MUST hand back:

1. Diagnose with **≤2 tool calls**: `adb shell uiautomator dump` + `grep` (do
   NOT `file_read` screenshots) to see the real on-screen state.
2. Apply the **minimum** fix — e.g. press `KEYCODE_ENTER` to submit, clear a
   duplicated text, or one corrective tap.
3. **Immediately re-launch** artemis with the *remaining* sub-goal
   (`--instruction "<remaining>" --profile flash --daemon`) and poll that log.
4. Never complete the rest of the task with raw `adb shell input tap/text`:
   that violates the "no raw adb" rule and burns the turn budget (each
   dump/tap is one tool call).

### Instruction-writing tips

- **Search/input flows need an explicit submit.** `type` only enters text —
  never Enter. When the task goes through a search box, spell out the submit
  step (e.g. "type GAI, then press Enter / tap the top result"). Without it
  the VLM re-types the same text and stalls until `--max_steps`.

### CLI Parameters

| Parameter          | Default          | Description |
|--------------------|------------------|-------------|
| `--instruction`    | *(required)*     | Natural language task |
| `--profile`        | `flash`          | Execution profile: `flash` or `pro` |
| `--device`         | *(auto-detect)*  | ADB device serial |
| `--max_steps`      | `50`             | Hard cap on steps |
| `--timeout`        | `900`            | Seconds before kill (run ceiling, not a client timeout) |
| `--verification`   | `final`          | `off` / `final` / `checkpoints` / `strict` |
| `--explorer`       | `pro`            | Explorer tier: `flash` / `pro` / `ultra` |
| `--dry_run`        | `false`          | Only run pre-checks |
| `--debug`          | `false`          | Verbose logging |
| `--daemon`         | `false`          | Fork into background, print `run_started` JSON, exit; poll `--log-file` |
| `--log-file`       | `""`             | Append all JSONL/logs here (in `--daemon` mode: the file to poll) |

### Exit Codes

| Code | `status`   | Meaning |
|------|-----------|---------|
| `0`  | `success`  | Task completed (Checker confirmed) |
| `1`  | `error`    | Runtime error (ADB lost, model crash) |
| `2`  | `clarify`  | Clarification needed |
| `3`  | `timeout`  | Timeout or step limit reached |
| `4`  | `error`    | ADB device not found |

### ZeroClaw shell-policy notes

The ZeroClaw shell sandbox (e.g. `level = "supervised"` with a non-empty
`allowed_commands`) is strict. To keep this skill runnable:

- **Timeout**: `[autonomy] shell_timeout_secs` must exceed the run time — the
  60s default kills the agent mid-run. Set `shell_timeout_secs = 900` (or more).
- **No `cd`**: `cd` is usually not in `allowed_commands`. Run the script by path;
  it self-resolves its directory.
- **No `python3 -c` / `-m`**: inline code is blocked by the shell policy
  (`is_args_safe`). Use a script file or `--dry_run` for health checks.
- **No shell `>` file redirects**: only `>/dev/null`, `2>&1`, heredocs are
  allowed. Capture output with `--log-file <path>` (the script writes it
  itself) instead of `> file`.
- **No backgrounding (`&`)**: a `&` job stays in zeroclaw's shell process group
  and gets SIGKILLed on timeout, and a plain foreground run only returns stdout
  at exit. Use `--daemon` instead — it double-forks into a new session so it
  survives, returns a `run_started` JSON immediately, and you poll `--log-file`
  with short `tail`/`cat` calls for live progress.
- **Vision model required**: `config.json` must point at a live VLM (e.g.
  gui-owl), not a text-only model. The default gui-owl endpoint is
  `http://apicn.aiworm.cn:8809/v1`.
- **`--timeout` is the run ceiling, NOT a client timeout.** Pass `--timeout 900`
  (or omit it — the default is already 900). Do NOT shrink it to mirror the
  old shell timeout; a navigation task needs several minutes. Short `--timeout`
  (e.g. 180) makes the script kill itself mid-run (exit code 3).
- **Output is buffered, not streamed.** zeroclaw's `shell` tool returns the
  whole JSONL only when the process exits — there are no intermediate frames.
  The calling client must therefore wait the full run duration: use
  `test_agent.py --timeout 900` (or a higher turn timeout) so the single
  `tool_result` + `done` can arrive.
- **Wait = `adb shell sleep N`; read = `tail -n 80 <LOG> | grep '"type"'`.**
  Host `sleep`/`timeout`/`nohup`/`setsid`/`ps`/`pgrep`/`ss` are NOT in
  `allowed_commands` — each failed attempt wastes a tool iteration. Read the
  final answer from the last `{"type":"result"}` object (`status`/`message`/
  `steps`), not from the `[log]` human-readable lines.
- **NEVER `file_read` a screenshot/`.png`.** Reading a PNG through the calling
  agent's `file_read` tool injects the raw binary bytes into the agent's own LLM
  context and overflows the token window ("context window exceeds limit").
  Artemis already does the screenshot→VLM upload internally and safely; the
  calling agent must NOT re-read those files. To verify the on-screen state, grep
  TEXT instead: `adb shell uiautomator dump` then `grep` the window/XML, or read
  only the `result`/`progress` JSON lines. To actually LOOK at a screenshot file, use
  `image_info` — it returns format/size/dimensions AND attaches the image to
  vision-capable models via an `[IMAGE:…]` marker. Never `file_read` the PNG bytes.
- **Skill-generated files stay under the skill dir.** Artemis writes its own
  screenshots to `skills/artemis-control-v2/screenshots/run_<ts>_<pid>/` and its
  log to `--log-file`. Do NOT `adb pull` screenshots/XML into the workspace root
  and do NOT create stray `zc_*.png` files — pulling binaries to the repo root is
  what led to the accidental `file_read` of PNGs.

## Response Dispatch (for the calling agent)

**Stream separation:** The script writes ONLY JSON to **stdout**. ALL diagnostic logs go to **stderr**.

| `"type"`      | Meaning | Action |
|---------------|---------|--------|
| `"progress"`  | One step executed | Narrate `message`; report the `screenshot` path — NEVER `file_read` the PNG |
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

## Verifying a Finished Run (for the Calling Agent)

`result.status` is self-reported. In `flash` there is no independent check; in
`pro` it is backed by `result.verification` (`passed`/`failed`/`inconclusive`).
Trust it proportionally, in this order:

1. **Read the run log first** — `result.status`, `result.verification`, and
   `memory/notes/_scratchpad.json` (chosen POI / step observations).
2. If more confirmation is needed, use **read-only device state**:
   ```bash
   adb shell dumpsys window | grep mCurrentFocus
   adb shell dumpsys activity services <pkg> | grep -i nav
   ```
3. Only as a last resort, take **one** fresh `screencap` — and never touch the screen.

**Never re-drive the phone after a run finishes.** A second agent's taps/typing
overwrite the completed UI and repeatedly broke the result (e.g. mis-taps into
settings). If trust is genuinely low, re-run the skill with
`--profile pro --verification final` instead of re-verifying manually.

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