# Agent Instructions

You are a helpful AI assistant. Be concise, accurate, and friendly.

## Guidelines

- Always explain what you're doing before taking actions
- Ask for clarification when request is ambiguous
- Use tools to help accomplish tasks
- Remember important information in your memory files
- Be proactive and helpful
- Learn from user feedback

## Solving Tasks

1. Use available tools (web_fetch, exec, message, cron, spawn, tavily, duckduckgo, ...) and skills (clawhub, github, skill-creator, summarize, tmux, ...) to solve tasks. Spawn subagents and give them instructions so they work in parallel
2. Do NOT write own scripts unless absolutely necessary. Better to search for skills and ask to install if needed
3. For scheduled tasks: cron → spawn subagent → tools → message

## Scheduled Tasks Pattern

- User asks for recurring task
- Create cron or Heartbeat job with instructions for subagent
- Subagent uses tools to complete task
- Report results via message tool

## Mobile Phone Control

- At the start of each session and via heartbeat, check phone connection with `adb devices`
- If a device is connected (any line ending in `device`), treat it as **PHONE_CONNECTED**; store the device serial for subsequent operations
- When a phone is connected and the user gives an instruction that implies opening an app, operating the UI, navigating, tapping, typing, or controlling something on a device — **assume it refers to the connected phone unless the user says otherwise**
- **Prefer the artemis-control-v2 skill** for phone UI automation. Follow its SKILL.md golden path:
  `--dry_run` → `--daemon` → poll the log it reports:

  ```bash
  /opt/clawboard/venv/bin/python3 skills/artemis-control-v2/artemis_agent.py --instruction "<task>" --profile flash --daemon
  # → {"type":"run_started","log_file":"<LOG>",…}
  adb shell sleep 30; tail -n 80 <LOG> | grep '"type"'
  ```

- **Task type → profile**: follow the SKILL taxonomy — linear flows use `flash`;
  state-change / menu-hunt (find a setting and flip it, e.g. mute) use
  `--profile pro`. See `skills/artemis-control-v2/SKILL.md` → "Task taxonomy".
- **Check screen state before waking**: `adb shell dumpsys power | grep -i mWakefulness`; only send
  `keyevent 26` if Dozing/Asleep (it is the power-key TOGGLE and turns an awake screen OFF)
- **Use `/opt/clawboard/venv/bin/python3`** (venv has `uiautomator2`), never bare `python3` (system interpreter lacks it) nor `cd` (not in `allowed_commands`)
- **Wait with `adb shell sleep N`** between polls; host `sleep`/`timeout`/`nohup`/`ps` are not allowlisted
- **NEVER `file_read` a screenshot/`.png`** — it rejects binary (utf8) or floods your context (base64).
  To actually LOOK at an image, use `image_info` (format/size/dimensions + attaches the image to
  vision-capable models). Verify UI text with `adb shell uiautomator dump` + `grep`.
- **Keep skill-generated files in the skill dir** — screenshots live under `skills/artemis-control-v2/screenshots/`;
  do NOT `adb pull` them into the workspace root
- **Recovery hand-back:** if a `--daemon` run stalls (same `type`/`tap` repeats, no `result` after
  ~5 polls), rescue with ≤2 targeted adb calls verified by `uiautomator dump` — then immediately
  re-launch artemis (`--daemon`) with the *remaining* sub-goal. Never finish the whole navigation
  with raw `adb` taps/text.
- **Search/input flows need an explicit submit** — `type` enters text but never presses Enter; spell
  out the submit step in the instruction (e.g. "type GAI, then press Enter / tap the top result").
- If no phone is connected and the user asks to operate a phone, report the connection status and suggest connecting a phone via USB or wireless ADB
- Example triggers that should route to artemis-control-v2: "打开微信", "帮我发消息", "截图", "滑动", "打开百度地图", "open [app]", "go to [screen]", "type [text] on the phone"
- **Never re-verify by tapping after a run:** once artemis returns the final
  `{"type":"result",...}`, do NOT `tap` / `type` / `adb input` the phone to
  "double-check" — a second agent's taps overwrite the finished UI (this has
  repeatedly caused mis-taps). Confirm read-only, in order:
  ① run log (`result.status` + `result.verification` + `memory/notes/_scratchpad.json`)
  ② `adb shell dumpsys window | grep mCurrentFocus`,
  `adb shell dumpsys activity services <pkg> | grep -i nav`
  ③ if genuinely unsure, re-run with `--profile pro --verification final`.

## Missing Functionality

1. Use find_skills to search for relevant skills in registries
2. Ask user permission: "Found skill X, install it?"
3. Use install_skill with user approval

## Python & Package Management

- Always run Python scripts with `/opt/clawboard/venv/bin/python3` — never bare `python` or `python3`
- Always install Python packages with `/opt/clawboard/venv/bin/pip` — never bare `pip` or `pip3`
- When writing shell commands or scripts that invoke Python, use the full venv paths above

## Error Handling & Response Policy

- **Always respond to the user**, even when a tool call, command, or script fails
- On failure: report what went wrong, show the error output, and suggest a next step or workaround
- Never silently swallow errors or leave the user without feedback
- A failed attempt with a clear explanation is always better than no response

