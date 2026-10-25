#!/usr/bin/env python3
"""
artemis_agent.py — Self-contained Android automation agent (Artemis port)

Multi-agent graph for mobile device automation with two profiles:
  - Flash: Plan-guided reactive loop with history + context budget (fast, ~5-8s/step)
  - Pro:   Full graph — Planner → Operator → Validator → SafetyNet → Execute → Checker

No external SDK dependency — pure Python + ADB + VLM/LLM.

Components (in runner/):
  - planner     — Task plan grammar, plan generation, plan-guided execution
  - validator   — Action validator: independent VLM judges Operator's proposal
  - checker     — Final task completion audit (VLM or text LLM)
  - safety_net  — Pre-execution XML + pixel precondition validation
  - incidents   — Structured failure tracking with self-healing
  - explorer    — UI element indexer (accessibility tree)
  - context     — History store with keyword search and context budget tracking

Usage:
    python3 artemis_agent.py --instruction "打开百度地图,导航回家"
    python3 artemis_agent.py --instruction "Open WeChat" --profile pro

Exit codes:
    0  Task completed (success)
    1  Runtime error
    2  Clarification needed
    3  Timeout or max-steps reached
    4  ADB device not found
"""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import urllib.request
import urllib.error
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

# App package name resolution (bidirectional map from packages.py)
from runner.packages import NAME_PACKAGE_DICT, normalize_package_name

# Self-contained runner modules (no external SDK dependency)
from runner.safety_net import SafetyNet, SafetyNetConfig
from runner.incidents import IncidentTracker
from runner.explorer import ElementIndexer, draw_indexed_dots, screenshot_diff_count
from runner.context import ContextBudget, HistoryStore, StepRecord
from runner.planner import (
    apply_plan_updates,
    build_operator_plan_system_prompt,
    generate_plan,
    is_task_complete,
    parse_plan,
)
from runner.validator import call_validator
from runner.checker import call_checker
from runner.memory_runtime import MemoryRuntime
from runner.diagnoser import call_diagnoser
from runner.post_run_report import build_post_run_report, write_report

# ---------------------------------------------------------------------------
# Global log handles
# ---------------------------------------------------------------------------
_LOG_FILE = None
_DEBUG = False
_TRACE_LOG = Path(__file__).parent / "skill_trace.log"

def _trace(msg: str) -> None:
    """Append a timestamped line to skill_trace.log in the skill directory."""
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        with _TRACE_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_MAX_STEPS = 50
DEFAULT_TIMEOUT = 900  # 15 minutes
LOOP_THRESHOLD = 3
VERSION = "1.1.0"  # bump on every meaningful change

# Common Chinese app → Android package name mapping
APP_PACKAGE_MAP = {
    "微信": "com.tencent.mm", "wechat": "com.tencent.mm",
    "支付宝": "com.eg.android.AlipayGphone",
    "淘宝": "com.taobao.taobao", "taobao": "com.taobao.taobao",
    "京东": "com.jingdong.app.mall",
    "抖音": "com.ss.android.ugc.aweme",
    "百度地图": "com.baidu.BaiduMap",
    "高德地图": "com.autonavi.minimap",
    "设置": "com.android.settings", "settings": "com.android.settings",
    "美团": "com.sankuai.meituan",
    "饿了么": "me.ele",
    "拼多多": "com.xunmeng.pinduoduo",
    "小红书": "com.xingin.xhs",
    "哔哩哔哩": "tv.danmaku.bili", "bilibili": "tv.danmaku.bili",
    "QQ": "com.tencent.mobileqq",
    "网易云音乐": "com.netease.cloudmusic",
    "酷狗音乐": "com.kugou.android",
    "百度": "com.baidu.searchbox",
    "知乎": "com.zhihu.android",
    "微博": "com.sina.weibo",
    "今日头条": "com.ss.android.article.news",
    "滴滴出行": "com.sdu.didi.psnger",
    "闲鱼": "com.taobao.idlefish",
    "钉钉": "com.alibaba.android.rimet",
    "企业微信": "com.tencent.wework",
    "keep": "com.gotokeep.keep",
    "携程": "ctrip.android.view",
    "飞猪": "com.taobao.trip",
    "大众点评": "com.dianping.v1",
}

# Vague instruction patterns → request clarification
VAGUE_PATTERNS = [
    r"^(打开它|打开那个|开一下|open it|go there|帮我操作|操作一下)$",
    r"^(做一下|处理|搞一下|help me|do it|fix it)$",
]

# Cached skill config (loaded lazily from config.json)
_CONFIG: Optional[dict] = None
_CONFIG_PATH = Path(__file__).parent / "config.json"

# VLM system prompt for action decisions — three-phase cognitive protocol
SYSTEM_PROMPT_FALLBACK = """## Role
You are an Android device operator. Your priority is execution accuracy — a single
wrong tap can navigate away from the target and cost several steps to recover.
Think before you act.

## Output Format
Respond with a single JSON object. No other text.

Available actions:
  {"action": "open_app", "app_name": "微信"}              — launch an app by name
  {"action": "tap", "target": 3}                          — tap element N by INDEX (REQUIRED if element is indexed)
  {"action": "tap", "coordinate": [x, y]}                 — tap at pixel (only when element NOT in list)
  {"action": "long_press", "target": 3}                   — long-press element N
  {"action": "swipe", "coordinate1": [x1,y1], "coordinate2": [x2,y2], "duration": 300}
  {"action": "type", "text": "hello"}                     — type text into focused field
  {"action": "system_button", "button": "back"}           — back / home / enter / recent_apps
  {"action": "finish"}                                    — task completed
  {"action": "fail", "reason": "why"}                     — task cannot be completed

Required field:
  "target_description" — WHERE you are tapping and WHY. NOT "tap element 3" —
                          describe what the element IS. Examples: "搜索框 Search bar",
                          "百度地图 app icon", "允许 button in permission dialog".
                          REQUIRED for tap, long_press, and swipe.

Optional fields:
  "plan_updates" — see Plan Tracking section above (plan-guided only)
  "notes"        — dict of key→content for persistent scratchpad memory.
                    Use this to record findings between steps, e.g.:
                    {"notes": {"login_page": "Found login page — use 手机登录 tab next"}}

## Vision-First Priority
**The screenshot is the primary source of truth** for the real device state.
Always visually inspect the screen layout on the screenshot BEFORE consulting the
element list. The 'Visible UI Elements' list is an auxiliary lookup table — use
it to retrieve an element's index and center AFTER you have visually identified
the target on the screenshot.

## Cognitive Protocol (follow in every turn)

### 1. Observation — read the screen
- What app is currently open? (check the status bar, app title, UI elements)
- Am I on the right screen, or did I navigate somewhere unexpected?
- Are there loading spinners, popups, permission dialogs, or ads blocking the UI?
- Check PREVIOUS ACTIONS — have I already tried this element? Don't repeat failures.
- Look at the BACKGROUND of the screenshot, not just elements — the UI index may
  miss floating overlays, toasts, or keyboard state.

### 2. Planning — decide what to do
- What is the smallest, safest step toward the next milestone?
- Is there a loading/animation that will finish before my action lands?
  (If yes: wait briefly instead of tapping mid-transition)
- Am I about to tap something destructive?
  (delete, uninstall, logout, purchase, send message, factory reset)
- What will the screen look like AFTER this action? If uncertain, reconsider.

### 3. Execution — pick the right target
- **Vision-First**: Find the element VISUALLY on the screenshot, THEN look it up
  in the element list to get its index. Output {"action": "tap", "target": N}.
  This is MANDATORY when the element IS in the list — it's more reliable than
  guessing coordinates.
- **Coordinate fallback**: ONLY use {"action": "tap", "coordinate": [x, y]} if
  the target is NOT in the element list (custom-drawn UI, Canvas, WebView, Flutter,
  or splash-screen ads). When you do use coordinates, you MUST also provide an
  accurate target_description.
- For text input: FIRST tap the input field (verify it has focus), THEN use
  {"action": "type", "text": "..."}. Never type without first ensuring focus.
- If typing in a search bar: tap the bar first, confirm keyboard is up, then type.
- For back navigation: press back N times to reach a known state, not just once.

## Anti-Patterns (DO NOT DO)
- Tapping the same coordinate 3+ times — if it didn't work twice, try a DIFFERENT
  element or approach.
- Tapping a different element from what target_description says.
- Using {"action": "tap", "target": N} with the WRONG index — always visually
  verify which element matches which index before using it.
- Typing text without first tapping the target field — text goes nowhere.
- Using open_app repeatedly when the app is already open — check the screen first.
- Finishing prematurely — verify the goal was met, don't just assume.
- Ignoring popups/dialogs — dismiss them (tap outside, press back) before continuing.
- Repeating the same tap after PREVIOUS ACTIONS shows it already failed 2+ times.
  If a tap at the same element/area didn't work twice, the element may not be
  tappable, the app may be in a different state, or you may be targeting the
  wrong thing.  SWITCH to a DIFFERENT approach (different element, swipe, back).

## Error Recovery
- If you see an unexpected screen: press BACK once and re-evaluate.
- If the app crashed: re-launch it with open_app.
- If a permission dialog appears: tap "允许" (Allow) or "始终允许" (Always allow).
- If loading takes too long: press BACK and try an alternative path.
- If completely lost: press HOME, then start over with open_app.

## When to Finish
- All required actions were performed AND confirmed visually.
- The final screen shows the expected result (app open, setting changed, search done).
- PREVIOUS ACTIONS shows you already completed the task — don't keep going.
- If the task is "打开X" (open X): finish as soon as X's main screen is visible.

## When to Fail
- The target app is not installed (check app drawer or search).
- You tried 3+ different approaches and all failed.
- A required account login / SMS verification blocks progress.
- The device is locked with a PIN/password/pattern you don't know.

## Element Selection Rules
- UI elements marked "clickable" AND "enabled" are safe tap targets.
- Class prefixes: [EditText]=text input, [Button]=button, [ImageView]=icon/image,
  [TextView]=text label, [CheckBox]=checkbox, [Switch]=toggle switch.
  Use them to tell an input field from an icon at a glance.
- Avoid elements with no label AND no resource-id — they're likely decorative.
- Prefer elements with Chinese text matching your task (e.g. "设置" for Settings).
- For scrolling: swipe from the CENTER of a scrollable region, not from an element.
- If an element's bounds overlap with another, prefer the one with a specific
  resource-id over one without."""

# Compact system prompt — optimised for smaller/weaker vision models
# (qwen3.8-flash, gui-owl, etc).  Drops the verbose protocol in favour of
# direct instruction.  ~40% of the full prompt size.
SYSTEM_PROMPT_COMPACT = """## Role
Android device operator. Output ONLY a JSON object per turn.

## Actions
{"action":"tap","target":3}                          — tap element N by INDEX (REQUIRED if element is indexed)
{"action":"tap","coordinate":[x,y]}                    — tap at pixel (only when element NOT in list)
{"action":"long_press","target":3}                     — long-press element N
{"action":"swipe","coordinate1":[x1,y1],"coordinate2":[x2,y2],"duration":300}
{"action":"type","text":"text to type"}
{"action":"system_button","button":"back|home|enter|recent_apps"}
{"action":"open_app","app_name":"app name"}
{"action":"finish"}
{"action":"fail","reason":"why"}

## Rules (MUST follow)
1. **Vision-First Priority**: Look at the screenshot FIRST. Visually find the target,
   THEN look it up in the element list to get its index.
2. **Use {"action":"tap","target":N}** — this is more reliable than raw coordinates.
   The system resolves the index to the exact pixel center automatically.
3. **Class prefixes tell you element type**: [EditText]=text input, [Button]=button,
   [ImageView]=icon/image, [TextView]=label. Use them to tell INPUT from ICON.
4. **Tap BEFORE typing.** Always tap a text field before using type action.
5. **Never repeat the same tap 3 times.** If a tap doesn't work, try a different element.
6. **Check if app is already open.** Don't use open_app if you're already in the right app.
7. **Dismiss popups immediately.** Permission dialogs, ads, update prompts — tap outside or press back.
8. **Only finish when the goal is VISUALLY confirmed.** Don't guess.
9. **Always include target_description** — describe WHAT the element IS, not just its index.

## Element Format
Each UI element: [N] [ClassName] "label" center=(cx,cy) flags
Example: [3] [EditText] "Search" center=(500,80) rid=search_box clickable
Use the INDEX number with target=N. ClassName tells you if it's an input or icon.
Trust elements with Chinese labels matching your task (e.g. "搜索" for search).

## Recovery
- Wrong screen → press back once, re-evaluate
- App crashed → open_app to re-launch
- Lost → press home, then open_app
- Permission dialog → tap "允许" or "始终允许"
- Loading spinner → wait one step, then try

## Plan Updates (plan-guided only)
```json
{"plan_updates":{"set_active":"milestone text","mark_done":"milestone text"}}
```
Copy milestone text from the plan shown in the user message. Omit if no change."""


# ── OpenCode gateway support ────────────────────────────────────────────

# One session id per agent run so opencode gateways (zen, go) can do sticky
# routing / cache affinity across calls.  Generated once at import time.
_SESSION_ID = uuid.uuid4().hex[:12]

# The x-opencode-client value the probe script confirmed works.
_OPENCODE_CLIENT = "vscode-copilot-chat"
_OPENCODE_UA = "everything-copilot-chat/artemis-control"


def _resolve_api_key(cfg: dict) -> str:
    """Return the API key: prefer env var *api_key_env* over inline *api_key*."""
    env_var = cfg.get("api_key_env", "").strip()
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]
    return cfg.get("api_key", "")


def _build_openai_headers(cfg: dict, api_key: str) -> dict[str, str]:
    """Build the HTTP headers for an OpenAI-compatible POST request.

    When ``opencode_headers`` is set in config, injects the 4 extra headers
    (x-opencode-session, x-opencode-request, x-opencode-client, User-Agent)
    that opencode gateways require.  Otherwise returns vanilla Bearer-auth
    headers.
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    if cfg.get("opencode_headers"):
        headers.update({
            "x-opencode-session": _SESSION_ID,
            "x-opencode-request": uuid.uuid4().hex,
            "x-opencode-client": _OPENCODE_CLIENT,
            "User-Agent": _OPENCODE_UA,
        })
    return headers


def load_skill_config() -> dict:
    """Load config.json, falling back to hard-coded defaults."""
    global _CONFIG
    if _CONFIG is not None:
        return _CONFIG

    defaults = {
        "api_key": "not-needed",
        "model": "gui-owl",
        "base_url": "http://apicn.aiworm.cn:8809/v1",
        "api_key_env": "",
        "opencode_headers": False,
        "thinking": False,
    }

    try:
        if _CONFIG_PATH.exists():
            raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            # ── Resolve provider: string → named profile, dict → inline ──
            provider = raw.get("provider", {})
            if isinstance(provider, str):
                registry = raw.get("providers", {})
                if provider not in registry:
                    _log(f"[config] provider '{provider}' not found in providers registry (keys: {sorted(registry.keys())})")
                provider = registry.get(provider, {})
                # Warn about common JSON key typos (trailing spaces, etc.)
                for key in list(provider.keys()):
                    stripped = key.strip()
                    if stripped != key:
                        _log(f"[config] WARNING: provider key {key!r} has whitespace — did you mean {stripped!r}?")
                        provider[stripped] = provider.pop(key)
                _log(f"[config] resolved named provider '{raw.get('provider')}' → model={provider.get('model','?')} base_url={provider.get('base_url','?')}")
            # ── Apply provider to defaults ──
            # A provider is "active" when it has an api_key (inline or env var)
            # or a non-default model set.  opencode-go uses api_key_env + an
            # empty inline api_key, so the old api_key-alone check missed it.
            if provider and (
                provider.get("api_key", "").strip()
                or provider.get("api_key_env", "").strip()
                or provider.get("model", "").strip()
            ):
                defaults["api_key"] = provider.get("api_key", "").strip()
                defaults["model"] = provider.get("model", defaults["model"])
                defaults["base_url"] = provider.get("base_url", defaults["base_url"])
                defaults["api_key_env"] = provider.get("api_key_env", "").strip()
                defaults["opencode_headers"] = bool(provider.get("opencode_headers"))
                defaults["thinking"] = provider.get("thinking", False)
                _log(f"[config] using provider → model={defaults['model']} base_url={defaults['base_url']}")
            else:
                fb = raw.get("fallback_provider", {})
                if fb:
                    defaults["api_key"] = fb.get("api_key", defaults["api_key"])
                    defaults["model"] = fb.get("model", defaults["model"])
                    defaults["base_url"] = fb.get("base_url", defaults["base_url"])
                    defaults["api_key_env"] = fb.get("api_key_env", "").strip()
                    defaults["opencode_headers"] = bool(fb.get("opencode_headers"))
                    defaults["thinking"] = bool(fb.get("thinking"))
                    _log(f"[config] using fallback_provider → model={defaults['model']}")
                else:
                    _log(f"[config] no provider matched, using hardcoded defaults")

            # Carry non-provider sections (profiles / context / memory /
            # safety_net / explorer / supervisor_provider and any top-level
            # scalars) through to _CONFIG.  Without this they are silently
            # dropped and every run would use hard-coded defaults (e.g. the
            # memory section would never take effect).
            for _key, _value in raw.items():
                if _key in ("providers", "provider", "fallback_provider"):
                    continue
                if _key not in defaults:
                    defaults[_key] = _value
        else:
            _log(f"[config] config file not found at {_CONFIG_PATH}")
    except json.JSONDecodeError as e:
        _log(f"[config] JSON parse error in {_CONFIG_PATH}: {e}")
        _trace(f"[config] JSON parse error: {e}")
    except Exception as e:
        _log(f"[config] Failed to load config.json: {e}")
        _trace(f"[config] Failed to load config.json: {e}")

    _CONFIG = defaults
    api_key_display = "env:" + defaults["api_key_env"] if defaults["api_key_env"] else ("<set>" if defaults["api_key"] else "<empty>")
    _trace(f"[config] model={defaults['model']} base_url={defaults['base_url']} api_key={api_key_display} opencode_headers={defaults['opencode_headers']}")
    return defaults


def _get_operator_system_prompt() -> str:
    """Choose system prompt based on model capability.

    Smaller/weaker models (qwen flash, gui-owl, etc.) get a compact prompt
    which is ~40% shorter and uses simpler language.  Full-size models
    (gemini, gpt-4o, claude, MiniMax) get the full cognitive protocol.
    """
    cfg = load_skill_config()
    model = (cfg.get("model", "") or "").lower()

    # Heuristic: models with "flash", "mini", "small", "tiny", "nano" in the
    # name, or known weak vision models, get the compact prompt.
    COMPACT_KEYWORDS = (
        "flash", "mini", "small", "tiny", "nano", "lite",
        "gui-owl", "qwen2-vl", "qwen-vl",
    )
    if any(kw in model for kw in COMPACT_KEYWORDS):
        return SYSTEM_PROMPT_COMPACT
    return SYSTEM_PROMPT_FALLBACK

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError as e:
        return -1, "", str(e)

def _adb(args: list[str], adb_path: str = "adb", device: Optional[str] = None,
         timeout: int = 10) -> tuple[int, str, str]:
    cmd = [adb_path]
    if device:
        cmd += ["-s", device]
    cmd += args
    return _run(cmd, timeout=timeout)

def _ts_now() -> str:
    """ISO-8601 timestamp with milliseconds, used for timing diagnostics."""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(time.time() * 1000) % 1000:03d}"


def _emit(obj: dict) -> None:
    """Print a JSON object to stdout — consumed by OpenClaw."""
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _emit_progress(step: int, action: str, message: str,
                   screenshot: Optional[str] = None,
                   verified: Optional[bool] = None,
                   safety_check: Optional[str] = None,
                   step_start_s: float | None = None) -> None:
    """Emit a JSONL progress line."""
    obj: dict = {
        "type": "progress", "step": step, "action": action, "message": message,
        "timestamp": _ts_now(),
    }
    if step_start_s is not None:
        obj["elapsed_s"] = round(time.time() - step_start_s, 2)
    if screenshot:
        obj["screenshot"] = screenshot
    if verified is not None:
        obj["verified"] = verified
    if safety_check:
        obj["safety_check"] = safety_check
    _emit(obj)

def _emit_incident(step: int, category: str, reason: str, action: str,
                   consecutive_failures: int = 0) -> None:
    """Emit an incident notification for safety-net blocks."""
    _emit({
        "type": "incident",
        "step": step,
        "category": category,
        "reason": reason,
        "action": action,
        "consecutive_failures": consecutive_failures,
    })

def _emit_plan(plan_text: str, milestones: list[str], total: int) -> None:
    """Emit the generated task plan as a JSON event."""
    _emit({
        "type": "plan",
        "plan_text": plan_text,
        "milestones": milestones,
        "total_milestones": total,
    })


def _log(msg: str) -> None:
    _now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{_now}] [artemis-control] {msg}"
    print(line, file=sys.stderr, flush=True)
    if _LOG_FILE:
        try:
            _LOG_FILE.write(line + "\n")
            _LOG_FILE.flush()
        except Exception:
            pass

def _debug(msg: str) -> None:
    if _DEBUG:
        _log(f"[DEBUG] {msg}")

# ---------------------------------------------------------------------------
# Pre-checks
# ---------------------------------------------------------------------------

def check_adb_device(adb_path: str, device: Optional[str]) -> Optional[str]:
    """Return the device serial to use, or None if no device found."""
    rc, out, _ = _adb(["devices"], adb_path=adb_path, timeout=10)
    if rc != 0:
        return None
    lines = [l for l in out.splitlines() if "\t" in l]
    online = [l.split("\t")[0] for l in lines if "device" in l.split("\t")[1]]
    if not online:
        return None
    if device:
        return device if device in online else None
    return online[0]

def ensure_screen_on(adb_path: str, device: str) -> None:
    """Wake the screen if it is off."""
    rc, out, _ = _adb(
        ["shell", "dumpsys", "window", "policy"], adb_path=adb_path, device=device
    )
    if "isInteractive=false" in out or "mInteractive=false" in out:
        _log("Screen off — waking...")
        _adb(["shell", "input", "keyevent", "26"], adb_path=adb_path, device=device)
        time.sleep(1)
        _adb(["shell", "input", "swipe", "540", "1600", "540", "800", "300"],
             adb_path=adb_path, device=device)
        time.sleep(1)
    else:
        _log("Screen is on.")

def needs_clarification(instruction: str) -> bool:
    """Return True if the instruction is too vague."""
    s = instruction.strip()
    if len(s) < 4:
        return True
    for pattern in VAGUE_PATTERNS:
        if re.fullmatch(pattern, s, re.IGNORECASE):
            return True
    return False

# ---------------------------------------------------------------------------
# Screenshot capture
# ---------------------------------------------------------------------------

SCREENSHOTS_ROOT = Path(__file__).parent / "screenshots"

def _make_run_screenshots_dir() -> Path:
    _ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    _pid = os.getpid()
    run_dir = SCREENSHOTS_ROOT / f"run_{_ts}_{_pid}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir

def _take_screenshot(adb_path: str, device: Optional[str], dest: Path,
                     max_age_seconds: int = 30) -> bool:
    """Capture a screenshot from the device via ADB and save to dest."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    import subprocess as _sp
    cmd = [adb_path]
    if device:
        cmd += ["-s", device]
    cmd += ["exec-out", "screencap", "-p"]
    try:
        result = _sp.run(cmd, capture_output=True, timeout=15)
        if result.returncode == 0 and result.stdout:
            dest.write_bytes(result.stdout)

            # Cache device resolution from the screenshot (first capture only)
            global _DEVICE_RESOLUTION
            if _DEVICE_RESOLUTION is None:
                try:
                    from PIL import Image
                    with Image.open(dest) as img:
                        _DEVICE_RESOLUTION = img.size
                    _trace(f"[screen] device resolution = {_DEVICE_RESOLUTION[0]}x{_DEVICE_RESOLUTION[1]}")
                except Exception:
                    pass
            age = time.time() - dest.stat().st_mtime
            if age > max_age_seconds:
                _log(f"Screenshot {dest.name} is {age:.1f}s old; discarding")
                dest.unlink(missing_ok=True)
                return False
            return True
    except Exception as e:
        _log(f"Screenshot failed: {e}")
    return False

# ---------------------------------------------------------------------------
# Artemis availability check
# ---------------------------------------------------------------------------
# Core runner: self-contained multi-agent graph (no Artemis SDK dependency)
# ---------------------------------------------------------------------------

def run_artemis_agent(
    instruction: str,
    adb_path: str,
    device: str,
    profile: str,
    max_steps: int,
    timeout: int,
    verification: str,
    explorer_tier: str,
) -> dict:
    """Run the Artemis mobile agent and emit progress events.

    This is a self-contained port of the Artemis multi-agent graph.
    No external SDK dependency — pure Python + ADB + VLM.

    Routes to the appropriate fallback loop based on profile:
      - ``flash``: Plan-guided reactive loop (planner + memory, no safety net)
      - ``pro``:   Full multi-agent graph (planner → operator → validator →
                   safety net → execute → checker)
    """
    _log(f"Running {profile} fallback loop v1.1.0 — model={load_skill_config().get('model', '?')}")

    if profile == "pro":
        return _run_plan_guided_fallback(
            instruction, adb_path, device, max_steps, timeout,
            verification=verification, explorer_tier=explorer_tier,
        )
    return _run_reactive_fallback(instruction, adb_path, device, max_steps, timeout, profile=profile)


def _finalize_run(
    *,
    mem,
    scratchpad: dict[str, str],
    plan_text: str,
    profile: str,
    instruction: str,
    status: str,
    end_reason: str,
    step: int,
    actions: list[str],
    start: float,
    validator_stats: dict | None = None,
    incident_count: int = 0,
    checker: dict | None = None,
    memory_replay: bool = False,
) -> Path | None:
    """Persist cross-run notes + write the deterministic post-run report.

    The report replaces upstream's LLM post-mortem agents; the notes persist
    the VLM-written scratchpad and the final task plan across runs.
    """
    try:
        mem.persist_scratchpad(scratchpad)
        if plan_text:
            mem.save_note("task_plan", plan_text)
    except Exception as e:
        _debug(f"notes persist error: {e}")

    try:
        report_md = build_post_run_report(
            profile=profile,
            instruction=instruction,
            status=status,
            end_reason=end_reason,
            steps=step,
            actions=actions,
            plan_text=plan_text,
            validator_stats=validator_stats,
            incident_count=incident_count,
            checker=checker,
            memory_replay=memory_replay,
            elapsed_s=time.time() - start,
        )
        report_path = write_report(Path(__file__).parent, profile, report_md)
        _emit({"type": "post_run_report", "profile": profile,
               "path": str(report_path), "report": report_md})
        _log(f"[report] post-run report → {report_path}")
        return report_path
    except Exception as e:
        _debug(f"post-run report error: {e}")
        return None


def _render_notes_for_diagnoser(mem) -> str:
    """Render persisted notes for the Diagnoser prompt (bounded)."""
    try:
        info = mem.list_notes()
    except Exception:
        return ""
    parts: list[str] = []
    for key, _lines in sorted(info.items()):
        try:
            content = mem.read_note(key, 1, 40)
        except Exception:
            continue
        parts.append(f"### {key}\n{content}")
    return "\n\n".join(parts)


def _memory_replay_shortcut(screenshots_dir: Path, profile: str, replay: dict) -> dict:
    """Build the success result emitted when a stored plan was replayed."""
    try:
        if screenshots_dir and screenshots_dir.exists():
            shutil.rmtree(screenshots_dir, ignore_errors=True)
    except Exception:
        pass
    replayed_steps = int(replay.get("steps", 0))
    replayed_actions = list(replay.get("actions") or [])
    return {
        "type": "result",
        "status": "success",
        "steps": replayed_steps,
        "last_action": replayed_actions[-1] if replayed_actions else "",
        "message": (
            f"{profile} loop: success — replayed {replayed_steps} steps from "
            "cross-run memory. [reason: memory_replay_completed]"
        ),
        "actions": replayed_actions,
        "verification": {"passed": 0, "failed": 0, "inconclusive": 0},
        "debug": {
            "end_reason": "memory_replay_completed",
            "profile": profile,
            "memory_replay": True,
        },
        "plan": None,
    }


def _run_reactive_fallback(
    instruction: str,
    adb_path: str,
    device: str,
    max_steps: int,
    timeout: int,
    profile: str = "flash",
) -> dict:
    """
    Fallback reactive observe→think→act loop when Artemis SDK is not available.
    Uses the safety net and explorer modules from this skill directly.

    When profile is "pro", delegates to ``_run_plan_guided_fallback`` which adds
    milestone-based planning and plan-guided execution.
    """
    if profile == "pro":
        return _run_plan_guided_fallback(
            instruction=instruction,
            adb_path=adb_path,
            device=device,
            max_steps=max_steps,
            timeout=timeout,
        )
    # (runner imports are now module-level — no local duplication)

    # Read profile config
    _cfg = load_skill_config()
    _profile_cfg = _cfg.get("profiles", {}).get(profile, {})
    _ctx_cfg = _cfg.get("context", {})
    _explorer_cfg = _cfg.get("explorer", {})

    # Resolve max_steps: CLI arg takes precedence, else profile max_turns, else 50
    if max_steps <= 0:
        max_steps = _profile_cfg.get("max_turns", 0) or 50
    explorer_mode = _profile_cfg.get("explorer_mode", "flash")

    _log(f"Running flash fallback loop v{VERSION} — model={_cfg['model']} — plan + history")

    screenshots_dir = _make_run_screenshots_dir()
    start = time.time()
    actions: list[str] = []
    step = 0
    status = "timeout"
    end_reason = "flash_started"
    last_coords: list[str] = []
    last_action_types: list[str] = []  # Track recent action types for loop detection
    previous_post_shot: Optional[Path] = None  # For screenshot similarity detection
    targeted_indices: dict[int, int] = {}  # element index → tap count (anti-loop)
    last_target_info: tuple[int, str] | None = None  # (idx, desc) of last targeted element
    indexer = ElementIndexer(adb_path=adb_path, device=device)

    # ── Cross-run memory (Phase 0) ─────────────────────────────────────
    _mem_cfg = _cfg.get("memory", {})
    mem = MemoryRuntime(
        instruction=instruction,
        memory_cfg=_mem_cfg,
        skill_dir=str(Path(__file__).parent),
        execute_fn=lambda at, aa, els: _execute_action_adb(adb_path, device, at, aa, elements=els),
        indexer=indexer,
        log_fn=_log,
    )
    mem.setup()

    # Replay a stored task plan before spending any VLM budget.
    if mem.enforce:
        replay = mem.try_plan_replay(
            emit_fn=lambda i, act, msg: _emit_progress(i, act, msg)
        )
        if replay.get("status") == "completed":
            _log(f"Flash: task replayed from memory ({replay.get('steps', 0)} steps) — skipping VLM")
            return _memory_replay_shortcut(screenshots_dir, "flash", replay)

    history = HistoryStore(
        max_stored_screenshots=_ctx_cfg.get("max_stored_screenshots", 6),
    )
    context_budget = ContextBudget(
        limit_tokens=_ctx_cfg.get("budget_limit", 80000),
        warn_at_pct=_ctx_cfg.get("budget_warn_pct", 0.75),
        critical_at_pct=_ctx_cfg.get("budget_critical_pct", 0.90),
    )
    plan_text = ""
    plan_generated = False
    last_budget_warning_pct = 0.0  # Avoid repeated warnings for same level
    scratchpad: dict[str, str] = {}  # Persistent cross-turn notes

    # Reload the scratchpad persisted by previous runs (Phase 1 notes).
    if mem.enabled:
        persisted = mem.load_scratchpad()
        if persisted:
            scratchpad.update(persisted)
            _log(f"[MEMORY] loaded persisted scratchpad ({len(persisted)} keys)")

    # ── Phase 1: Generate Plan ──────────────────────────────────────────
    _emit_progress(0, "planning", "Flash: generating task plan...")
    _log("Flash: generating plan...")

    plan_shot = screenshots_dir / "flash_plan_initial.png"
    if _take_screenshot(adb_path, device, plan_shot):
        plan_result = generate_plan(
            instruction=instruction,
            screenshot_path=str(plan_shot),
            vlm_caller=_call_vlm_simple,
        )
    else:
        plan_result = {"success": False, "error": "Screenshot failed", "plan_text": "", "plan_snapshot": parse_plan("")}

    if plan_result.get("success"):
        plan_text = plan_result["plan_text"]
        snapshot = plan_result["plan_snapshot"]
        plan_generated = True
        _log(f"Flash plan generated: {snapshot.total_count} milestones")
        _trace(f"[flash-plan] milestones={snapshot.total_count}")
        _emit_plan(
            plan_text=plan_text,
            milestones=list(snapshot.milestone_texts()),
            total=snapshot.total_count,
        )
        _emit_progress(0, "plan_ready",
                       f"Task plan: {snapshot.total_count} milestones")
    else:
        _log(f"Flash plan generation failed: {plan_result.get('error')} — continuing reactive")

    # ── Phase 2: Plan-Guided Reactive Loop ───────────────────────────────
    _emit_progress(1 if step == 0 else step, "init",
                   f"Starting plan-guided loop for: {instruction[:60]}...")

    while step < max_steps and (time.time() - start) < timeout:
        step += 1
        t_step = time.time()
        _debug(f"Step {step}/{max_steps}")

        # Check if all milestones done
        if plan_text and is_task_complete(plan_text):
            _log("All milestones complete — task finished")
            status = "success"
            end_reason = "all_milestones_done"
            break

        # 1. Take screenshot
        ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:19]
        shot_path = screenshots_dir / f"step_{step:03d}_{ts}_pre.png"
        t_shot = time.time()
        if not _take_screenshot(adb_path, device, shot_path):
            _log("Screenshot capture failed")
            continue
        _log(f"[timing] step {step} screenshot  → {time.time() - t_shot:.2f}s  (total {time.time() - t_step:.2f}s)")

        # 2. Index UI elements
        t_index = time.time()
        try:
            elements = indexer.build_index(shot_path)
            _debug(f"Indexed {len(elements)} UI elements")
        except Exception as e:
            _debug(f"Element indexing skipped: {e}")
            elements = []
        _log(f"[timing] step {step} index       → {time.time() - t_index:.2f}s  (total {time.time() - t_step:.2f}s)")

        # 2.5 Draw indexed dots on screenshot for Vision-First targeting
        vlm_shot_path = shot_path  # default: raw screenshot
        if elements:
            t_dots = time.time()
            annotated = screenshots_dir / f"step_{step:03d}_{ts}_annotated.jpg"
            if draw_indexed_dots(str(shot_path), elements, str(annotated)):
                vlm_shot_path = annotated
                _debug(f"Annotated screenshot with {len(elements)} dots")
                _log(f"[timing] step {step} dots        → {time.time() - t_dots:.2f}s  (total {time.time() - t_step:.2f}s)")

        # Screenshot similarity check against previous post-action screenshot
        # If screen unchanged, inject a note for the VLM
        similarity_note = ""
        if previous_post_shot and previous_post_shot.exists():
            diff = screenshot_diff_count(str(previous_post_shot), str(shot_path))
            if diff <= 3:
                if last_target_info:
                    tidx, tdesc = last_target_info
                    similarity_note = (
                        f"\n⚠️  STALE SCREEN: Element [{tidx}] '{tdesc}' was tapped → "
                        f"screen is pixel-identical. Do NOT target [{tidx}] again. "
                        f"Use a DIFFERENT element, coordinate tap, or action (type/swipe/back).\n"
                    )
                else:
                    similarity_note = "\n⚠️  NOTE: The screen is pixel-identical to the previous step. Your last action had NO visible effect. Try a DIFFERENT approach.\n"
                _log(f"[loop] Screen unchanged detected (diff={diff}px)")

        # Action-type loop detection — warn VLM about repeated same actions
        action_loop_note = ""
        if len(last_action_types) >= 4 and len(set(last_action_types[-4:])) == 1:
            repeated = last_action_types[-1]
            count = len([a for a in last_action_types if a == repeated])
            action_loop_note = (f"\n⚠️  ACTION LOOP: {repeated} ×{count} consecutive. "
                               f"Screen may not be changing. Use a DIFFERENT action type.\n")
            _log(f"[LOOP] Same action {repeated} repeated {count} consecutive times")

        # 3. Determine action — memory fastpath first, then VLM (plan-guided)
        t_vlm = time.time()
        mem_state = mem.describe_state(elements) if mem.enabled else None
        fast = mem.fastpath(mem_state, elements)
        if fast:
            action_type, action_args, action_desc = fast
            plan_updates = None
            notes_updates = None
            _log(f"[memory] step {step} fastpath hit: {action_desc}")
            _log(f"[timing] step {step} VLM decide  → 0.00s (memory fastpath)  (total {time.time() - t_step:.2f}s)")
        else:
            action_type, action_args, action_desc, plan_updates, notes_updates = (
                _decide_action_plan_guided(
                    instruction, step, elements, None, last_coords,
                    str(vlm_shot_path), actions, plan_text, history=history,
                    scratchpad=scratchpad, similarity_note=similarity_note,
                    targeted_indices=targeted_indices,
                    action_loop_note=action_loop_note,
                )
            )
            _log(f"[timing] step {step} VLM decide  → {time.time() - t_vlm:.2f}s  (total {time.time() - t_step:.2f}s)")

        # Merge any notes written by the VLM into the scratchpad
        if notes_updates and isinstance(notes_updates, dict):
            scratchpad.update(notes_updates)

        # 4. Apply plan updates from VLM
        if plan_updates and plan_text:
            new_plan_text, changed = apply_plan_updates(plan_text, plan_updates)
            if changed:
                plan_text = new_plan_text
                snap = parse_plan(plan_text)
                _trace(f"[flash-plan] step {step}: progress {snap.done_count}/{snap.total_count}")

        # 5. Execute action (no safety net in flash mode)
        t_exec = time.time()
        _execute_action_adb(adb_path, device, action_type, action_args, elements=elements)
        actions.append(action_desc)
        # Anti-loop: track which element indices we've targeted
        if action_type == "tap" and isinstance(action_args.get("target"), int):
            tidx = action_args["target"]
            targeted_indices[tidx] = targeted_indices.get(tidx, 0) + 1
            last_target_info = (tidx, action_desc)
        time.sleep(1.2)
        _log(f"[timing] step {step} execute     → {time.time() - t_exec:.2f}s  (total {time.time() - t_step:.2f}s)")

        # 5.5 Memory: record the successful step (action + task plan)
        if mem.enabled:
            try:
                target_el = None
                target_rid = ""
                if action_type == "tap" and isinstance(action_args.get("target"), int):
                    ti = action_args["target"]
                    if 0 <= ti < len(elements):
                        target_el = elements[ti]
                        target_rid = getattr(target_el, "resource_id", "") or ""
                mem.record_action(
                    mem_state, action_type, action_args, action_desc,
                    target_rid=target_rid, post_pkg=mem.fg_pkg(elements),
                    target_el=target_el,
                )
            except Exception as e:
                _debug(f"memory record error: {e}")

        # 6. Record in history
        try:
            history.add(StepRecord(
                step=step,
                action=action_desc,
                action_type=action_type,
                action_args=action_args,
            ))
            # Context budget monitoring
            warning = context_budget.check(history)
            if warning and warning.usage_pct >= last_budget_warning_pct + 0.1:
                last_budget_warning_pct = warning.usage_pct
                _log(f"Context budget: {warning.usage_pct:.0%} used ({warning.used_tokens}/{warning.limit_tokens} tokens)")
                _emit({
                    "type": "budget_warning",
                    "usage_pct": round(warning.usage_pct, 2),
                    "used_tokens": warning.used_tokens,
                    "limit_tokens": warning.limit_tokens,
                    "message": warning.message,
                })
        except Exception:
            pass

        # 7. Verification screenshot
        t_post = time.time()
        vshot_path = screenshots_dir / f"step_{step:03d}_{ts}_post.png"
        _take_screenshot(adb_path, device, vshot_path)
        previous_post_shot = vshot_path  # save for next step's similarity check
        _log(f"[timing] step {step} post-shot   → {time.time() - t_post:.2f}s  (total {time.time() - t_step:.2f}s)")

        _emit_progress(
            step, action_desc,
            f"Step {step}: {action_desc}",
            screenshot=str(vshot_path) if vshot_path.exists() else None,
            step_start_s=t_step,
        )
        _log(f"[timing] step {step} TOTAL       → {time.time() - t_step:.2f}s")

        # 8. Loop detection — bucketed coordinates (collapses small-variance taps)
        if action_type == "tap" and "coordinate" in action_args:
            # Bucket to nearest 100 — taps on the same button (within ±50 px)
            # all collapse to one bucket while different regions stay separate.
            bx = round(action_args["coordinate"][0] / 100) * 100
            by = round(action_args["coordinate"][1] / 100) * 100
            coord_bucket = f"{bx},{by}"
            last_coords.append(coord_bucket)
            if len(last_coords) > LOOP_THRESHOLD:
                last_coords = last_coords[-LOOP_THRESHOLD:]
            if len(last_coords) == LOOP_THRESHOLD and len(set(last_coords)) == 1:
                _log(f"[LOOP] Same coordinate bucket {coord_bucket} tapped {LOOP_THRESHOLD}+ times — STUCK, need different approach!")
        else:
            last_coords = []

        # 8b. Loop detection — repeated open_app + dismiss cycles
        last_action_types.append(action_type)
        if len(last_action_types) > 6:
            last_action_types = last_action_types[-6:]
        recent = last_action_types[-4:]
        if (
            len(recent) >= 4
            and recent.count("open_app") >= 2
            and any(a in ("press", "system_button") for a in recent)
        ):
            _log("Action loop detected — repeated open_app + dismiss cycle")

        # 9. Check task completion
        if action_type == "finish":
            if plan_text and not is_task_complete(plan_text):
                snapshot = parse_plan(plan_text)
                if snapshot.pending_count == 0:
                    # No [ ] milestones left — auto-complete remaining [/] items
                    _log("VLM says finish with 0 pending milestones — auto-completing plan")
                    new_lines = []
                    for line in plan_text.splitlines():
                        m = re.match(r'^(\s*)- \[/\]\s', line)
                        if m:
                            line = m.group(1) + '- [x] ' + line[m.end():]
                        new_lines.append(line)
                    plan_text = "\n".join(new_lines)
                    if is_task_complete(plan_text):
                        _log("Plan auto-completed successfully")
                        status = "success"
                        end_reason = "flash_completed_auto"
                        break
                remaining = snapshot.pending_count
                _log(f"VLM says finish but {remaining} milestones still pending — continuing")
                if remaining > 0:
                    continue
            status = "success"
            end_reason = "flash_completed"
            break

    # Memory: persist task plan on success
    mem.on_run_end(status)

    # Persist cross-run notes + write the deterministic post-run report.
    report_path = _finalize_run(
        mem=mem, scratchpad=scratchpad, plan_text=plan_text,
        profile="flash", instruction=instruction, status=status,
        end_reason=end_reason, step=step, actions=actions, start=start,
    )

    # Cleanup
    try:
        if screenshots_dir.exists():
            shutil.rmtree(screenshots_dir, ignore_errors=True)
    except Exception:
        pass

    final_snapshot = parse_plan(plan_text) if plan_text else None
    plan_done = final_snapshot.done_count if final_snapshot else 0
    plan_total = final_snapshot.total_count if final_snapshot else 0

    message = f"Flash loop: {status} after {step} steps. Plan: {plan_done}/{plan_total} milestones. [reason: {end_reason}]"
    return {
        "type": "result",
        "status": status,
        "steps": step,
        "last_action": actions[-1] if actions else "",
        "message": message,
        "actions": actions,
        "verification": {"passed": 0, "failed": 0, "inconclusive": 0},
        "debug": {
            "end_reason": end_reason,
            "profile": "flash",
            "plan_milestones_total": plan_total,
            "plan_milestones_done": plan_done,
            "history_steps": history.step_count,
            "post_run_report": str(report_path) if report_path else None,
        },
        "plan": {
            "text": plan_text,
            "milestones_done": plan_done,
            "milestones_total": plan_total,
        } if plan_text else None,
    }


def _run_plan_guided_fallback(
    instruction: str,
    adb_path: str,
    device: str,
    max_steps: int,
    timeout: int,
    verification: str = "final",
    explorer_tier: str = "pro",
) -> dict:
    """Pro profile fallback: full multi-agent graph with separate VLM calls per node.

    Graph: Planner (once) → [Operator → Validator → SafetyNet → Execute] → Checker (final)

    Each node is an independent VLM/LLM call with its own system prompt:
    - Planner VLM: generates milestone plan from screenshot + goal
    - Operator VLM: proposes next action given current state + plan
    - Validator VLM: independently judges the proposed action (safety, progress, correctness)
    - SafetyNet (deterministic): XML + pixel pre-execution validation
    - Checker VLM/LLM: final audit of goal achievement vs execution history
    """
    # (runner imports are now module-level — no local duplication)

    # Read profile and safety-net config
    _cfg = load_skill_config()
    _profile_cfg = _cfg.get("profiles", {}).get("pro", {})
    _ctx_cfg = _cfg.get("context", {})
    _safety_cfg = _cfg.get("safety_net", {})
    _explorer_cfg = _cfg.get("explorer", {})

    # Resolve max_steps: CLI arg takes precedence, else profile max_turns, else 50
    if max_steps <= 0:
        max_steps = _profile_cfg.get("max_turns", 0) or 50
    verification_level = verification or _profile_cfg.get("verification", "final")
    explorer_mode = explorer_tier or _profile_cfg.get("explorer_mode", "pro")

    _log(f"Running Pro multi-agent graph v{VERSION} — model={_cfg['model']}")

    screenshots_dir = _make_run_screenshots_dir()
    start = time.time()
    actions: list[str] = []
    step = 0
    status = "timeout"
    end_reason = "pro_started"
    last_coords: list[str] = []
    last_action_types: list[str] = []  # Track recent action types for loop detection
    plan_text = ""
    plan_generated = False
    validator_stats = {"approved": 0, "warned": 0, "rejected": 0}
    incident_tracker = IncidentTracker()
    history = HistoryStore(
        max_stored_screenshots=_ctx_cfg.get("max_stored_screenshots", 6),
    )
    context_budget = ContextBudget(
        limit_tokens=_ctx_cfg.get("budget_limit", 80000),
        warn_at_pct=_ctx_cfg.get("budget_warn_pct", 0.75),
        critical_at_pct=_ctx_cfg.get("budget_critical_pct", 0.90),
    )
    last_budget_warning_pct = 0.0
    scratchpad: dict[str, str] = {}  # Persistent cross-turn notes
    previous_post_shot: Optional[Path] = None  # For screenshot similarity detection
    targeted_indices: dict[int, int] = {}  # element index → tap count (anti-loop)
    last_target_info: tuple[int, str] | None = None  # (idx, desc) of last targeted element

    # Reload the scratchpad persisted by previous runs (Phase 1 notes).
    if mem.enabled:
        persisted = mem.load_scratchpad()
        if persisted:
            scratchpad.update(persisted)
            _log(f"[MEMORY] loaded persisted scratchpad ({len(persisted)} keys)")

    # Lazy Diagnoser (Phase 2): fires once per failure burst when the
    # IncidentTracker's consecutive_failures crosses ``consecutive_failures``.
    _diag_cfg = _cfg.get("diagnoser", {})
    diag_enabled = bool(_diag_cfg.get("enabled", True))
    diag_threshold = max(1, int(_diag_cfg.get("consecutive_failures", 2) or 2))
    diagnoser_fired_for = 0  # consecutive_failures value at which we last fired
    safety_net = SafetyNet(SafetyNetConfig(
        adb_path=adb_path,
        device=device,
        xml_enabled=_safety_cfg.get("xml_enabled", True),
        pixel_fallback=_safety_cfg.get("pixel_fallback", True),
        retry_attempts=_safety_cfg.get("retry_attempts", 3),
        retry_delay=_safety_cfg.get("retry_delay", 0.4),
    ))
    indexer = ElementIndexer(adb_path=adb_path, device=device)

    # ── Cross-run memory (Phase 0) ─────────────────────────────────────
    _mem_cfg = _cfg.get("memory", {})
    mem = MemoryRuntime(
        instruction=instruction,
        memory_cfg=_mem_cfg,
        skill_dir=str(Path(__file__).parent),
        execute_fn=lambda at, aa, els: _execute_action_adb(adb_path, device, at, aa, elements=els),
        indexer=indexer,
        log_fn=_log,
    )
    mem.setup()

    # Replay a stored task plan before spending any VLM budget
    # (skips Planner → Operator → Validator → Checker on a clean replay).
    if mem.enforce:
        replay = mem.try_plan_replay(
            emit_fn=lambda i, act, msg: _emit_progress(i, act, msg)
        )
        if replay.get("status") == "completed":
            _log(f"Pro: task replayed from memory ({replay.get('steps', 0)} steps) — skipping VLM")
            return _memory_replay_shortcut(screenshots_dir, "pro", replay)

    # ── Phase 1: Planner Node ──────────────────────────────────────────
    _emit_progress(0, "planning", "Pro: Planner node — generating task plan...")
    _log("Pro Phase 1: Planner node")

    plan_shot = screenshots_dir / "pro_plan_initial.png"
    if _take_screenshot(adb_path, device, plan_shot):
        plan_result = generate_plan(
            instruction=instruction,
            screenshot_path=str(plan_shot),
            vlm_caller=_call_vlm_simple,
        )
    else:
        _log("Screenshot failed for planning — falling back to flash loop")
        plan_result = {"success": False, "error": "Screenshot failed", "plan_text": "", "plan_snapshot": parse_plan("")}

    if plan_result.get("success"):
        plan_text = plan_result["plan_text"]
        snapshot = plan_result["plan_snapshot"]
        plan_generated = True
        _log(f"Pro plan generated: {snapshot.total_count} milestones")
        _trace(f"[pro-plan] milestones={snapshot.total_count} text={plan_text[:200]!r}")
        _emit_plan(
            plan_text=plan_text,
            milestones=list(snapshot.milestone_texts()),
            total=snapshot.total_count,
        )
        _emit_progress(0, "plan_ready",
                       f"Pro task plan: {snapshot.total_count} milestones")
    else:
        error_msg = plan_result.get("error", "unknown")
        _log(f"Pro plan generation failed: {error_msg} — falling back to flash loop")
        return _run_reactive_fallback(
            instruction=instruction, adb_path=adb_path, device=device,
            max_steps=max_steps, timeout=timeout, profile="flash",
        )

    def _maybe_diagnose() -> None:
        """Run the lazy Diagnoser once per failure burst."""
        nonlocal diagnoser_fired_for
        if not diag_enabled:
            return
        cf = incident_tracker.consecutive_failures
        if cf < diag_threshold or cf <= diagnoser_fired_for:
            return
        diagnoser_fired_for = cf
        _log(f"[diagnoser] firing at {cf} consecutive failures...")
        try:
            diag = call_diagnoser(
                instruction=instruction,
                plan_text=plan_text,
                actions=actions,
                incidents=incident_tracker.get_recent(6),
                notes_text=_render_notes_for_diagnoser(mem),
                text_llm_caller=_get_text_llm_caller(),
            )
        except Exception as e:
            _debug(f"diagnoser error: {e}")
            return
        _emit({
            "type": "diagnosis",
            "root_cause": diag.get("root_cause", ""),
            "recovery_hints": diag.get("recovery_hints", []),
            "notes": diag.get("notes", {}),
        })
        for hint in diag.get("recovery_hints") or []:
            if hint:
                incident_tracker.add_recovery_hint(hint)
        for k, v in (diag.get("notes") or {}).items():
            if k and v:
                try:
                    mem.save_note(str(k), str(v))
                except Exception as e:
                    _debug(f"diagnoser note save error: {e}")
        _log(f"[diagnoser] hints delivered ({len(diag.get('recovery_hints') or [])})")

    # ── Phase 2: Operator → Validator → SafetyNet → Execute ─────────────
    _log("Pro Phase 2: Operator → Validator → SafetyNet → Execute loop")

    while step < max_steps and (time.time() - start) < timeout:
        step += 1
        t_step = time.time()
        _debug(f"Pro Step {step}/{max_steps}")

        # Milestone completion check
        if plan_text and is_task_complete(plan_text):
            _log("All milestones complete — proceeding to Checker")
            status = "success"
            end_reason = "all_milestones_done"
            break

        # 1. Screenshot + element index
        ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:19]
        shot_path = screenshots_dir / f"step_{step:03d}_{ts}_pre.png"
        t_shot = time.time()
        if not _take_screenshot(adb_path, device, shot_path):
            _log("Screenshot capture failed")
            continue
        _log(f"[timing] step {step} screenshot  → {time.time() - t_shot:.2f}s  (total {time.time() - t_step:.2f}s)")

        t_index = time.time()
        try:
            elements = indexer.build_index(shot_path)
            _debug(f"Indexed {len(elements)} UI elements")
        except Exception as e:
            _debug(f"Element indexing skipped: {e}")
            elements = []
        _log(f"[timing] step {step} index       → {time.time() - t_index:.2f}s  (total {time.time() - t_step:.2f}s)")

        # 1.5 Draw indexed dots and check screenshot similarity
        vlm_shot_path = shot_path
        if elements:
            t_dots = time.time()
            annotated = screenshots_dir / f"step_{step:03d}_{ts}_annotated.jpg"
            if draw_indexed_dots(str(shot_path), elements, str(annotated)):
                vlm_shot_path = annotated
                _debug(f"Annotated screenshot with {len(elements)} dots")
                _log(f"[timing] step {step} dots        → {time.time() - t_dots:.2f}s  (total {time.time() - t_step:.2f}s)")

        similarity_note = ""
        if previous_post_shot and previous_post_shot.exists():
            diff = screenshot_diff_count(str(previous_post_shot), str(shot_path))
            if diff <= 3:
                if last_target_info:
                    tidx, tdesc = last_target_info
                    similarity_note = (
                        f"\n⚠️  STALE SCREEN: Element [{tidx}] '{tdesc}' was tapped → "
                        f"screen is pixel-identical. Do NOT target [{tidx}] again. "
                        f"Use a DIFFERENT element, coordinate tap, or action (type/swipe/back).\n"
                    )
                else:
                    similarity_note = "\n⚠️  NOTE: The screen is pixel-identical to the previous step. Your last action had NO visible effect. Try a DIFFERENT approach.\n"
                _log(f"[loop] Screen unchanged detected (diff={diff}px)")

        # Action-type loop detection — warn VLM about repeated same actions
        action_loop_note = ""
        if len(last_action_types) >= 4 and len(set(last_action_types[-4:])) == 1:
            repeated = last_action_types[-1]
            count = len([a for a in last_action_types if a == repeated])
            action_loop_note = (f"\n⚠️  ACTION LOOP: {repeated} ×{count} consecutive. "
                               f"Screen may not be changing. Use a DIFFERENT action type.\n")
            _log(f"[LOOP] Same action {repeated} repeated {count} consecutive times")

        # ── 2. Operator Node: memory fastpath first, then VLM ────────────
        t_oper = time.time()
        mem_state = mem.describe_state(elements) if mem.enabled else None
        fast = mem.fastpath(mem_state, elements)
        if fast:
            action_type, action_args, action_desc = fast
            plan_updates = None
            notes_updates = None
            _log(f"[memory] step {step} fastpath hit: {action_desc}")
            _log(f"[timing] step {step} operator    → 0.00s (memory fastpath)  (total {time.time() - t_step:.2f}s)")
        else:
            action_type, action_args, action_desc, plan_updates, notes_updates = _decide_action_plan_guided(
                instruction, step, elements, incident_tracker, last_coords,
                str(vlm_shot_path), actions, plan_text, history=history,
                scratchpad=scratchpad, similarity_note=similarity_note,
                targeted_indices=targeted_indices,
                action_loop_note=action_loop_note,
            )
            _log(f"[timing] step {step} operator    → {time.time() - t_oper:.2f}s  (total {time.time() - t_step:.2f}s)")

        # Merge any notes written by the VLM into the scratchpad
        if notes_updates and isinstance(notes_updates, dict):
            scratchpad.update(notes_updates)

        # If operator says finish, check milestone completion
        if action_type == "finish":
            if plan_text and not is_task_complete(plan_text):
                snapshot = parse_plan(plan_text)
                if snapshot.pending_count == 0:
                    # No [ ] milestones left — auto-complete remaining [/] items
                    _log("Operator says finish with 0 pending milestones — auto-completing plan")
                    new_lines = []
                    for line in plan_text.splitlines():
                        m = re.match(r'^(\s*)- \[/\]\s', line)
                        if m:
                            line = m.group(1) + '- [x] ' + line[m.end():]
                        new_lines.append(line)
                    plan_text = "\n".join(new_lines)
                    if is_task_complete(plan_text):
                        _log("Plan auto-completed successfully")
                        status = "success"
                        end_reason = "operator_finished_auto"
                        break
                pending = snapshot.pending_count
                _log(f"Operator says finish but {pending} milestones pending — continuing")
                if pending > 0:
                    continue
            status = "success"
            end_reason = "operator_finished"
            break

        # ── 3. Validator Node: independent judgment ─────────────────
        t_vali = time.time()
        incident_context = ""
        if incident_tracker.has_active_incident:
            incident_context = incident_tracker.render_for_prompt()

        validation = call_validator(
            instruction=instruction,
            plan_text=plan_text,
            proposed_action=action_desc,
            action_type=action_type,
            action_args=action_args,
            screenshot_path=str(shot_path),
            vlm_caller=_call_vlm_simple,
            previous_incidents=incident_context,
        )
        _log(f"[timing] step {step} validator   → {time.time() - t_vali:.2f}s  (total {time.time() - t_step:.2f}s)")

        verdict = validation.get("verdict", "approve")
        _trace(f"[pro-validator] step {step}: verdict={verdict} reason={validation.get('reason', '')[:100]}")

        if verdict == "reject":
            _log(f"Validator REJECTED: {validation.get('reason', '')[:120]}")
            validator_stats["rejected"] += 1
            incident_tracker.add_incident(
                category="validator_rejected",
                reason=validation.get("reason", "Validator rejected the action"),
                action=action_type,
                step=step,
                action_args=action_args,
                action_description=action_desc,
                kind="safety_net",
            )
            _emit_incident(
                step, "validator_rejected",
                validation.get("reason", "Validator rejected"),
                action_type,
                incident_tracker.consecutive_failures,
            )
            # Feed suggestion back to next Operator call via incident
            suggestion = validation.get("suggestion", "")
            if suggestion:
                incident_tracker.add_recovery_hint(suggestion)
            _maybe_diagnose()
            continue

        elif verdict == "warn":
            validator_stats["warned"] += 1
            _log(f"Validator warned: {validation.get('concern', '')[:120]}")
            # Execute anyway but log the concern
            concern = validation.get("concern", "")
            if concern:
                incident_tracker.add_recovery_hint(f"Validator concern: {concern}")
        else:
            validator_stats["approved"] += 1

        # ── 4. Safety Net: deterministic XML + pixel check ───────────
        if action_type in ("tap", "long_press", "type"):
            passed, category, reason = safety_net.validate(
                action_type, action_args, shot_path
            )
            if not passed:
                incident_tracker.add_incident(
                    category=category, reason=reason,
                    action=action_type, step=step, action_args=action_args,
                )
                _emit_incident(step, category, reason, action_type,
                               incident_tracker.consecutive_failures)
                _log(f"Safety net blocked: {category} — {reason}")
                _maybe_diagnose()
                continue
            else:
                incident_tracker.clear_on_success()
                diagnoser_fired_for = 0
            safety_check = "xml_passed"
        else:
            safety_check = None

        # ── 5. Execute ──────────────────────────────────────────────
        t_exec = time.time()
        _execute_action_adb(adb_path, device, action_type, action_args, elements=elements)
        actions.append(action_desc)
        # Anti-loop: track which element indices we've targeted
        if action_type == "tap" and isinstance(action_args.get("target"), int):
            tidx = action_args["target"]
            targeted_indices[tidx] = targeted_indices.get(tidx, 0) + 1
            last_target_info = (tidx, action_desc)
        time.sleep(1.2)
        _log(f"[timing] step {step} execute     → {time.time() - t_exec:.2f}s  (total {time.time() - t_step:.2f}s)")

        # Memory: record the successful step (action + task plan)
        if mem.enabled:
            try:
                target_el = None
                target_rid = ""
                if action_type == "tap" and isinstance(action_args.get("target"), int):
                    ti = action_args["target"]
                    if 0 <= ti < len(elements):
                        target_el = elements[ti]
                        target_rid = getattr(target_el, "resource_id", "") or ""
                mem.record_action(
                    mem_state, action_type, action_args, action_desc,
                    target_rid=target_rid, post_pkg=mem.fg_pkg(elements),
                    target_el=target_el,
                )
            except Exception as e:
                _debug(f"memory record error: {e}")

        # Record in history
        try:
            history.add(StepRecord(
                step=step, action=action_desc,
                action_type=action_type, action_args=action_args,
                safety_check=safety_check or "",
            ))
            # Context budget monitoring
            warning = context_budget.check(history)
            if warning and warning.usage_pct >= last_budget_warning_pct + 0.1:
                last_budget_warning_pct = warning.usage_pct
                _log(f"Context budget: {warning.usage_pct:.0%} used ({warning.used_tokens}/{warning.limit_tokens} tokens)")
                _emit({
                    "type": "budget_warning",
                    "usage_pct": round(warning.usage_pct, 2),
                    "used_tokens": warning.used_tokens,
                    "limit_tokens": warning.limit_tokens,
                    "message": warning.message,
                })
        except Exception:
            pass

        # Apply plan updates
        if plan_updates and plan_text:
            new_plan_text, changed = apply_plan_updates(plan_text, plan_updates)
            if changed:
                plan_text = new_plan_text
                snap = parse_plan(plan_text)
                _trace(f"[pro-plan] step {step}: {snap.done_count}/{snap.total_count} done")

        # Verification screenshot
        t_post = time.time()
        vshot_path = screenshots_dir / f"step_{step:03d}_{ts}_post.png"
        _take_screenshot(adb_path, device, vshot_path)
        previous_post_shot = vshot_path  # save for next step's similarity check
        _log(f"[timing] step {step} post-shot   → {time.time() - t_post:.2f}s  (total {time.time() - t_step:.2f}s)")

        _emit_progress(
            step, action_desc,
            f"Step {step}: {action_desc} [validator: {verdict}]",
            screenshot=str(vshot_path) if vshot_path.exists() else None,
            verified=(safety_check is not None),
            safety_check=safety_check,
            step_start_s=t_step,
        )
        _log(f"[timing] step {step} TOTAL       → {time.time() - t_step:.2f}s")

        # Loop detection — bucketed coordinates
        if action_type == "tap" and "coordinate" in action_args:
            bx = round(action_args["coordinate"][0] / 100) * 100
            by = round(action_args["coordinate"][1] / 100) * 100
            coord_bucket = f"{bx},{by}"
            last_coords.append(coord_bucket)
            if len(last_coords) > LOOP_THRESHOLD:
                last_coords = last_coords[-LOOP_THRESHOLD:]
            if len(last_coords) == LOOP_THRESHOLD and len(set(last_coords)) == 1:
                _log(f"[LOOP] Same coordinate bucket {coord_bucket} tapped {LOOP_THRESHOLD}+ times — STUCK, need different approach!")
                incident_tracker.add_recovery_hint(
                    "Same coordinate tapped repeatedly. Try a different approach."
                )
        else:
            last_coords = []

        # Loop detection — repeated open_app + dismiss cycles
        last_action_types.append(action_type)
        if len(last_action_types) > 6:
            last_action_types = last_action_types[-6:]
        recent = last_action_types[-4:]
        if (
            len(recent) >= 4
            and recent.count("open_app") >= 2
            and any(a in ("press", "system_button") for a in recent)
        ):
            _log("Action loop detected — repeated open_app + dismiss cycle")
            incident_tracker.add_recovery_hint(
                "ACTION LOOP: You are repeatedly opening and leaving the app. "
                "The app may already be open. Look at the current screen carefully."
            )

    # ── Phase 3: Checker Node — final audit ──────────────────────────────
    _log("Pro Phase 3: Checker node — auditing goal achievement...")
    _emit_progress(step, "checking", "Checker node: auditing task completion...")

    # Take final screenshot for checker
    final_shot = screenshots_dir / "pro_checker_final.png"
    _take_screenshot(adb_path, device, final_shot)

    final_snapshot = parse_plan(plan_text) if plan_text else None
    plan_done = final_snapshot.done_count if final_snapshot else 0
    plan_total = final_snapshot.total_count if final_snapshot else 0

    # Build verification summary
    verification_summary = (
        f"Milestones: {plan_done}/{plan_total} complete\n"
        f"Validator stats: {validator_stats['approved']} approved, "
        f"{validator_stats['warned']} warned, "
        f"{validator_stats['rejected']} rejected\n"
        f"Safety incidents: {incident_tracker.count}"
    )

    checker_result = call_checker(
        instruction=instruction,
        plan_text=plan_text,
        actions=actions,
        steps=step,
        screenshot_path=str(final_shot) if final_shot.exists() else "",
        vlm_caller=_call_vlm_simple,
        text_llm_caller=_get_text_llm_caller(),
        verification_summary=verification_summary,
    )

    _emit({
        "type": "checker_result",
        "goal_achieved": checker_result.get("goal_achieved", True),
        "confidence": checker_result.get("confidence", 0.5),
        "summary": checker_result.get("summary", ""),
        "evidence": checker_result.get("evidence", []),
        "concerns": checker_result.get("concerns", []),
        "recommendation": checker_result.get("recommendation", ""),
    })

    checker_verdict = checker_result.get("goal_achieved", True)
    if not checker_verdict:
        _log(f"Checker verdict: GOAL NOT ACHIEVED — {checker_result.get('summary', '')[:120]}")
        # Task completed execution but goal was not met
        if status == "success":
            status = "checker_failed"

    # Memory: persist task plan only when the run ultimately succeeded
    mem.on_run_end("success" if checker_verdict else "failed")

    # Persist cross-run notes + write the deterministic post-run report.
    report_path = _finalize_run(
        mem=mem, scratchpad=scratchpad, plan_text=plan_text,
        profile="pro", instruction=instruction, status=status,
        end_reason=end_reason, step=step, actions=actions, start=start,
        validator_stats=validator_stats,
        incident_count=incident_tracker.count,
        checker=checker_result,
    )

    # Cleanup
    try:
        if screenshots_dir.exists():
            shutil.rmtree(screenshots_dir, ignore_errors=True)
    except Exception:
        pass

    message = (
        f"Pro graph: {status} after {step} steps. "
        f"Plan: {plan_done}/{plan_total} milestones. "
        f"Checker: {'✓ passed' if checker_verdict else '✗ failed'} "
        f"(confidence: {checker_result.get('confidence', 0):.0%}). "
        f"[reason: {end_reason}]"
    )
    return {
        "type": "result",
        "status": "success" if checker_verdict else "checker_failed",
        "steps": step,
        "last_action": actions[-1] if actions else "",
        "message": message,
        "actions": actions,
        "verification": {
            "passed": plan_done,
            "failed": 0,
            "inconclusive": max(0, plan_total - plan_done),
        },
        "debug": {
            "end_reason": end_reason,
            "profile": "pro",
            "plan_milestones_total": plan_total,
            "plan_milestones_done": plan_done,
            "incidents": incident_tracker.count,
            "validator_approved": validator_stats["approved"],
            "validator_warned": validator_stats["warned"],
            "validator_rejected": validator_stats["rejected"],
            "checker_confidence": checker_result.get("confidence", 0),
            "checker_goal_achieved": checker_verdict,
            "post_run_report": str(report_path) if report_path else None,
        },
        "plan": {
            "text": plan_text,
            "milestones_done": plan_done,
            "milestones_total": plan_total,
        } if plan_text else None,
        "checker": {
            "goal_achieved": checker_verdict,
            "confidence": checker_result.get("confidence", 0),
            "summary": checker_result.get("summary", ""),
            "recommendation": checker_result.get("recommendation", ""),
        },
    }


# Cached device screen resolution — set on first screenshot capture
_DEVICE_RESOLUTION: tuple[int, int] | None = None

# Maximum dimension for image normalization (preserves aspect ratio).
# 2000 gives VLM 4× more pixels than 1000, narrowing coordinate errors
# from ±3.1→±1.6 device-px per image-px on landscape devices.
NORM_MAX_DIM = 2000


def _encode_image(image_path: str) -> tuple[str, int, int]:
    """Read screenshot, resize for bandwidth, return (base64, norm_w, norm_h).

    Resizes to fit within NORM_MAX_DIM × NORM_MAX_DIM, preserving aspect ratio.
    Always returns (1000, 1000) as the coordinate space — the canonical convention
    that VLMs output in. The higher NORM_MAX_DIM (2000) reduces coordinate error
    amplification while keeping the 0-1000 output contract unchanged.
    """
    try:
        from PIL import Image
        from io import BytesIO
        img = Image.open(image_path)
        orig_w, orig_h = img.size

        scale = NORM_MAX_DIM / max(orig_w, orig_h)
        new_w = max(1, round(orig_w * scale))
        new_h = max(1, round(orig_h * scale))

        if scale < 1.0:
            img = img.resize((new_w, new_h), Image.LANCZOS)

        buf = BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        # Always report 1000×1000 as the coordinate space — the canonical
        # convention that VLM mobile agents are trained on (Artemis, mobile-skills).
        # The image may be up to NORM_MAX_DIM (2000) in its long edge but the
        # VLM still outputs in 0-1000 space; scaling in _scale_coordinate handles
        # the mapping to device pixels.
        return b64, 1000, 1000
    except ImportError:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        if _DEVICE_RESOLUTION:
            return b64, _DEVICE_RESOLUTION[0], _DEVICE_RESOLUTION[1]
        return b64, 1080, 2400


def _call_vlm(system_prompt: str, user_prompt: str, image_path: str,
              max_tokens: int = 2048,
              timeout: int | None = None,
              retries: int | None = None) -> tuple[Optional[str], str, int, int]:
    """Send a prompt + screenshot to the configured VLM.

    Returns (response_text, finish_reason, norm_w, norm_h) where norm_w/norm_h
    are the dimensions of the normalized image sent to the VLM.  VLM coordinates
    are relative to this space — scale them to device pixels via _scale_coordinate.

    On failure returns (None, "error", 0, 0).
    """
    cfg = load_skill_config()
    base_url = cfg["base_url"].rstrip("/")
    api_key = _resolve_api_key(cfg)
    model = cfg["model"]

    actual_timeout = timeout if timeout is not None else cfg.get("vlm_timeout", 120)
    actual_retries = retries if retries is not None else cfg.get("vlm_retries", 2)

    b64, norm_w, norm_h = _encode_image(image_path)
    t_encode = time.time()
    data_uri = f"data:image/png;base64,{b64}"
    _trace(f"[vlm] image encode: {time.time() - t_encode:.2f}s  ({len(b64)} chars base64)")

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": data_uri}},
                {"type": "text", "text": user_prompt},
            ]},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }
    if cfg.get("thinking"):
        body["thinking"] = {"type": "enabled"}

    url = f"{base_url}/chat/completions"
    last_error = ""
    for attempt in range(actual_retries + 1):
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers=_build_openai_headers(cfg, api_key),
                method="POST",
            )
            t_call = time.time()
            with urllib.request.urlopen(req, timeout=actual_timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                if not result.get("choices"):
                    raise ValueError("no choices in VLM response")
                choice = result["choices"][0]
                message = choice.get("message") or {}
                content = message.get("content")
                # Thinking models (glm / qwen / deepseek-reasoner) occasionally
                # return content=null and put the answer in reasoning_content
                # only — crashing on len(None) otherwise.
                if content is None:
                    content = message.get("reasoning_content") or ""
                content = str(content)
                if not content.strip():
                    raise ValueError("empty assistant content")
                finish = choice.get("finish_reason", "unknown")
                _trace(f"[vlm] API round-trip: {time.time() - t_call:.2f}s  model={model}  tokens={len(content)}")
                return content, finish, norm_w, norm_h
        except (urllib.error.URLError, TimeoutError, ConnectionResetError,
                OSError, ValueError) as e:
            last_error = str(e)
            if attempt < actual_retries:
                delay = (attempt + 1) * 1.5
                _log(f"VLM call failed (attempt {attempt+1}/{actual_retries+1}, retry in {delay:.1f}s): {last_error[:80]}")
                time.sleep(delay)
            else:
                _log(f"VLM call failed after {actual_retries+1} attempts ({url}): {last_error}")
                _trace(f"[vlm] call failed after {actual_retries+1} attempts: {last_error}")
        except Exception as e:
            _log(f"VLM call failed ({url}): {e}")
            _trace(f"[vlm] call failed: {e}")
            return None, "error", 0, 0

    return None, "error", 0, 0


def _call_vlm_simple(system_prompt: str, user_prompt: str, image_path: str,
                     max_tokens: int = 2048) -> tuple[Optional[str], str]:
    """Adapter for callers that don't need coordinate normalization
    (planner, validator, checker)."""
    raw, reason, _w, _h = _call_vlm(system_prompt, user_prompt, image_path, max_tokens)
    return raw, reason


def _scale_coordinate(vlm_x: int, vlm_y: int,
                      norm_w: int = 1000, norm_h: int = 1000) -> tuple[int, int]:
    """Scale a VLM coordinate (0-1000 canonical space) to device pixels.

    The VLM always works in a uniform 0-1000 range regardless of aspect ratio.
    We map 0-1000 to actual device pixels.  This is the same convention used by
    the original Artemis (``_to_px``) and mobile-skills.

    ``norm_w`` / ``norm_h`` are kept for backward compatibility but always 1000.
    """
    if _DEVICE_RESOLUTION is None:
        return vlm_x, vlm_y

    dev_w, dev_h = _DEVICE_RESOLUTION
    scaled_x = round(vlm_x / 1000 * dev_w)
    scaled_y = round(vlm_y / 1000 * dev_h)
    scaled_x = max(0, min(scaled_x, dev_w - 1))
    scaled_y = max(0, min(scaled_y, dev_h - 1))

    _log(f"[coord] VLM({vlm_x},{vlm_y}) ÷1000 → DEV({scaled_x},{scaled_y}) dev({dev_w}x{dev_h})")
    return scaled_x, scaled_y


def _get_norm_dimensions(image_path: str) -> tuple[int, int]:
    """Return the canonical coordinate space dimensions: always 1000×1000.

    The image is resized to fit within 1000×1000 and the VLM always works in
    a uniform 0-1000 range — same convention as the original Artemis and
    mobile-skills.  Aspect-ratio-aware scaling would stretch X on portrait
    screens, causing systematic coordinate errors.
    """
    return 1000, 1000


def _parse_xml_tool_calls(raw: str) -> str | None:
    """Parse XML <tool_calls> format (deepseek/qwen function-calling) into JSON.

    Example input:
        <tool_calls>
        <invoke name="tap">
        <parameter name="coordinate" string="true">[720, 2400]</parameter>
        </invoke>
        </tool_calls>

    Returns a JSON action string like: {"action":"tap","coordinate":[720,2400]}
    """
    if "<tool_calls>" not in raw and "<invoke " not in raw:
        return None

    # Extract action name from <invoke name="...">
    m_action = re.search(r'<invoke\s+name="(\w+)"', raw)
    if not m_action:
        return None
    action = m_action.group(1)

    # Extract parameters: <parameter name="X">value</parameter>
    params = {}
    for m in re.finditer(
        r'<parameter\s+name="(\w+)"[^>]*>(.+?)</parameter>',
        raw, re.DOTALL,
    ):
        name = m.group(1)
        value = m.group(2).strip()
        # Parse value: could be [x,y] array or quoted string
        if value.startswith("[") and value.endswith("]"):
            try:
                params[name] = json.loads(value)
            except json.JSONDecodeError:
                params[name] = value
        elif value.startswith('"') and value.endswith('"'):
            params[name] = value[1:-1]
        else:
            params[name] = value

    # Map XML action names to our action names
    action_map = {
        "tap": "tap",
        "click": "tap",
        "long_press": "long_press",
        "swipe": "swipe",
        "type": "type",
        "input_text": "type",
        "system_button": "system_button",
        "press": "system_button",
        "open_app": "open_app",
        "launch": "open_app",
        "finish": "finish",
        "fail": "fail",
    }
    mapped_action = action_map.get(action, action)

    result = {"action": mapped_action}

    # Map parameters to our JSON format
    if mapped_action in ("tap", "click"):
        coord = params.get("coordinate", [0, 0])
        result["coordinate"] = coord
    elif mapped_action == "long_press":
        coord = params.get("coordinate", [0, 0])
        result["coordinate"] = coord
    elif mapped_action == "swipe":
        result["coordinate1"] = params.get("coordinate1", params.get("coordinate", [0, 0]))
        result["coordinate2"] = params.get("coordinate2", [0, 0])
        result["duration"] = int(params.get("duration", 300))
    elif mapped_action == "type":
        result["text"] = params.get("text", "")
    elif mapped_action == "system_button":
        result["button"] = params.get("button", "back")
    elif mapped_action == "open_app":
        result["app_name"] = params.get("app_name", "")

    return json.dumps(result)


def _repair_json(raw: str, finish_reason: str) -> str | None:
    """Attempt to repair truncated or malformed output from the VLM.

    Handles:
    - JSON: truncated, multi-block, unescaped Chinese quotes
    - XML: ``<tool_calls>`` function-calling format (deepseek, qwen, etc.)

    Returns a valid JSON action string, or None if repair fails.
    """
    # Stage 0: XML <tool_calls> format
    xml_result = _parse_xml_tool_calls(raw)
    if xml_result:
        return xml_result

    # Stage 1: find complete { ... } blocks (depth returns to 0)
    candidates = []
    depth = 0
    start = -1
    for i, ch in enumerate(raw):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                candidates.append(raw[start:i + 1])
                start = -1

    for cand in candidates:
        try:
            json.loads(cand)
            return cand
        except json.JSONDecodeError:
            continue

    # Stage 2: no valid complete block — repair the text
    idx = raw.find("{")
    if idx < 0:
        return None

    partial = raw[idx:]

    for repair_attempt in _gen_repairs(partial):
        try:
            json.loads(repair_attempt)
            return repair_attempt
        except json.JSONDecodeError:
            continue

    return None


def _gen_repairs(partial: str):
    """Generate candidate repairs for truncated JSON, best-first."""
    open_count = partial.count("{")
    close_count = partial.count("}")

    # ---------- attempt 1: close unterminated string value ----------
    yield partial + '"' + "}" * max(0, open_count - close_count)

    # ---------- attempt 2: strip last incomplete field, recount braces ----------
    # Find last field separator that suggests a complete preceding value
    for sep in ('}",', '"],', '",', '],'):
        pos = partial.rfind(sep)
        if pos < 0:
            continue
        cut_pos = pos + len(sep) - 1  # include the comma
        stripped = partial[:cut_pos].rstrip(",")
        # Recount braces on stripped text
        s_open = stripped.count("{")
        s_close = stripped.count("}")
        if s_open > s_close:
            yield stripped + "}" * (s_open - s_close)

    # ---------- attempt 3: escape unescaped quotes in last string ----------
    # Model sometimes outputs Chinese quotes like "搜索"南岸花城" — try escaping
    idx_last_open = partial.rfind('"')
    idx_second_last = partial.rfind('"', 0, idx_last_open)
    if idx_second_last > 0 and idx_last_open > idx_second_last:
        # Content between second-last " and last " has internal quotes
        suffix = partial[idx_second_last + 1:]
        # Replace internal " with escaped \"
        fixed_suffix = suffix[:-1].replace('"', '\\"') + '"'
        escaped = partial[:idx_second_last + 1] + fixed_suffix
        e_open = escaped.count("{")
        e_close = escaped.count("}")
        if e_open > e_close:
            yield escaped + "}" * (e_open - e_close)

    # ---------- attempt 4: just close braces ----------
    if open_count > close_count:
        yield partial + "}" * (open_count - close_count)


def _call_text_llm(system_prompt: str, user_prompt: str,
                   max_tokens: int = 1024,
                   timeout: int | None = None,
                   retries: int | None = None) -> tuple[Optional[str], str]:
    """Send a text-only prompt to the configured supervisor LLM.

    Uses ``supervisor_provider`` from config.json when configured. Falls back
    to the primary VLM provider with a text-only message (no image).

    Returns (response_text, finish_reason). On failure returns (None, "error").
    Retries on transient failures with exponential backoff.
    Timeout and retries are read from config.json (keys: ``vlm_timeout``, ``vlm_retries``).
    """
    cfg = load_skill_config()

    actual_timeout = timeout if timeout is not None else cfg.get("vlm_timeout", 120)
    actual_retries = retries if retries is not None else cfg.get("vlm_retries", 2)

    # Check if supervisor_provider is explicitly configured
    sup = cfg.get("supervisor_provider", {})
    sup_model = sup.get("model", "")
    sup_url = sup.get("base_url", "")
    sup_key = sup.get("api_key", "")

    if sup_model and sup_url:
        # Use dedicated supervisor provider
        base_url = sup_url.rstrip("/")
        api_key = sup_key or _resolve_api_key(cfg)
        model = sup_model
    else:
        # Fall back to primary VLM provider (text-only, no image)
        base_url = cfg["base_url"].rstrip("/")
        api_key = _resolve_api_key(cfg)
        model = cfg["model"]

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }
    if cfg.get("thinking"):
        body["thinking"] = {"type": "enabled"}

    url = f"{base_url}/chat/completions"
    last_error = ""
    for attempt in range(actual_retries + 1):
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers=_build_openai_headers(cfg, api_key),
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=actual_timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                choice = result["choices"][0]
                return choice["message"]["content"], choice.get("finish_reason", "unknown")
        except (urllib.error.URLError, TimeoutError, ConnectionResetError,
                OSError) as e:
            last_error = str(e)
            if attempt < retries:
                delay = (attempt + 1) * 1.5
                _log(f"Text LLM call failed (attempt {attempt+1}/{retries+1}, retry in {delay:.1f}s): {last_error[:80]}")
                time.sleep(delay)
            else:
                _log(f"Text LLM call failed after {retries+1} attempts ({url}): {last_error}")
                _trace(f"[text_llm] call failed after {retries+1} attempts: {last_error}")
        except Exception as e:
            _log(f"Text LLM call failed ({url}): {e}")
            _trace(f"[text_llm] call failed: {e}")
            return None, "error"

    return None, "error"


def _get_text_llm_caller():
    """Return the text-LLM caller used by the Checker / Diagnoser.

    Previously returned None unless ``supervisor_provider.model`` was set —
    which silently disabled the Checker's preferred text path on every
    default install.  Now always returns ``_call_text_llm``, which prefers
    ``supervisor_provider`` when configured and otherwise falls back to the
    primary provider as a text-only request.  (The Checker retries with the
    VLM on failure, so a text-capable primary model is safe to try.)
    """
    return _call_text_llm


# ── Helper: build description with element index so history is traceable ──
def _desc_with_idx(prefix: str, idx, text: str) -> str:
    """Return ``[N] prefix text`` for element-targeted actions.
    The index is always included so the VLM can match PREVIOUS ACTIONS
    to the current element list."""
    base = text if text else f"element [{idx}]"
    return f"[{idx}] {prefix} {base}"


def _decide_action_fallback(
    instruction: str,
    step: int,
    elements: list,
    incident_tracker,
    last_coords: list[str],
    screenshot_path,
    previous_actions: list[str] = None,
) -> tuple[str, dict, str]:
    """
    Call the VLM with screenshot + element index + instruction to decide the next action.

    Returns (action_type, action_args, human_readable_description).
    On VLM failure, returns ("system_button", {"button": "back"}, "VLM failure: back").
    """
    if previous_actions is None:
        previous_actions = []

    # Compute normalized coordinate space — needed for element centers and prompt
    norm_w, norm_h = _get_norm_dimensions(screenshot_path)

    if elements:
        MAX_ELEMENTS = 80
        shown = elements[:MAX_ELEMENTS]
        lines = [f"UI ELEMENTS ({len(shown)} shown of {len(elements)} indexed):"]
        for el in shown:
            lines.append(el.render_index_line())
        index_text = "\n".join(lines)
    else:
        index_text = "UI ELEMENTS: (unavailable — use visual analysis)"

    # Previous actions — so VLM knows what already happened
    prev_text = ""
    if previous_actions:
        recent = previous_actions[-6:]  # last 6 actions
        prev_text = "PREVIOUS ACTIONS (do NOT repeat these):\n" + "\n".join(
            f"  - {a}" for a in recent
        ) + "\n"

    # Incident context
    incident_text = ""
    if incident_tracker and incident_tracker.count > 0:
        incident_text = f"PREVIOUS INCIDENTS:\n{incident_tracker.render_for_prompt()}\n"

    # Loop warning
    loop_text = ""
    if len(last_coords) >= LOOP_THRESHOLD:
        unique = len(set(last_coords))
        if unique == 1:
            loop_text = "⚠️  LOOP DETECTED: same coordinate tapped 3+ times. Try a DIFFERENT approach.\n"

    user_prompt = (
        f"TASK: {instruction}\n"
        f"Step: {step}\n"
        + (f"\n{prev_text}" if prev_text else "")
        + (f"\n{loop_text}" if loop_text else "")
        + (f"\n{incident_text}" if incident_text else "")
        + f"\n{index_text}"
        + (
            "\n\n## TARGETING RULES (MANDATORY)"
            "\n1. If the target IS in the element list → output {\"action\":\"tap\",\"target\":N}"
            "\n   using the INDEX number. The system resolves to exact pixel. Do NOT guess coordinates."
            "\n2. ONLY use {\"action\":\"tap\",\"coordinate\":[x,y]} if NO element matches your target"
            "\n   (custom-drawn UI, Canvas, WebView, Flutter, or splash ads not in the list)."
            "\n3. Using coordinate when the element IS indexed WILL cause a miss — the system"
            "\n   requires target=N for precise pixel lookup from the accessibility tree."
        )
    )

    user_prompt += (
        "\n\nCOORDINATE SPACE: When using coordinate (fallback only), output in 0-1000 range."
        " They will be auto-scaled to device pixels."
    )

    system_prompt = _get_operator_system_prompt()
    raw, finish_reason, norm_w, norm_h = _call_vlm(system_prompt, user_prompt, screenshot_path)
    if not raw:
        _log("Operator VLM returned no response — retrying once after pause")
        time.sleep(2)
        raw, finish_reason, norm_w, norm_h = _call_vlm(system_prompt, user_prompt, screenshot_path)
        if not raw:
            _log("Operator VLM failed after retry — returning system_button: back")
            return ("system_button", {"button": "back"}, "VLM failure: back")

    # If truncated by token limit, the JSON likely didn't fit — retry with back
    if finish_reason == "length" and "{" not in raw:
        _log("VLM output truncated (length) — injecting back action to retry")
        return ("system_button", {"button": "back"}, "truncated: back")

    # ── Robust JSON extraction ───────────────────────────────────────────────
    # Models may wrap output in <thinking>, ``` fences, or other text.
    # Try XML <tool_calls> format FIRST on the original raw text, before any
    # stripping (the tag-stripping regex would destroy <tool_calls> / <invoke>).
    repaired = _repair_json(raw, finish_reason)
    if repaired:
        # XML format was successfully parsed — use it directly
        pass
    else:
        # Fall back to JSON extraction with XML tag stripping
        json_str = raw

        # If there's a closing </thinking> tag, take everything after it
        think_end = json_str.rfind("</")
        if think_end >= 0:
            json_str = json_str[think_end:]

        # Strip any remaining XML tags (now safe — XML was tried first above)
        json_str = re.sub(r'<[^>]+/?>', '', json_str)
        json_str = re.sub(r'```\w*\n?', '', json_str)

        # Repair truncated/malformed JSON (handles multi-JSON, missing braces)
        repaired = _repair_json(json_str, finish_reason)

    if not repaired:
        _debug(f"VLM output has no valid JSON: {raw[:300]}")
        return ("system_button", {"button": "back"}, f"no JSON in response: {raw[:80]}")

    try:
        parsed = json.loads(repaired)
    except json.JSONDecodeError:
        _debug(f"VLM JSON parse failed after repair: {json_str[:300]}")
        return ("system_button", {"button": "back"}, f"VLM parse error: {json_str[:80]}")

    action = parsed.get("action", "finish")
    target_desc = parsed.get("target_description", "")
    target_idx = parsed.get("target")  # element index (preferred over raw coordinates)
    _log(f"[vlm-decision] action={action} target={target_idx} desc={target_desc} coords={parsed.get('coordinate', parsed.get('coordinate1', ''))}")

    # Resolve element index if the VLM used target=N
    def _resolve_target(idx, elements_ref):
        """Resolve a target index to pixel center, or return None."""
        if idx is None:
            return None
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            return None
        if elements_ref and 0 <= idx < len(elements_ref):
            return elements_ref[idx].center  # [cx, cy] in device pixels
        return None

    # Normalize action_type and args
    if action == "tap":
        el_center = _resolve_target(target_idx, elements)
        if el_center is not None:
            x, y = el_center[0], el_center[1]
            desc = _desc_with_idx("TAP", target_idx, target_desc)
            return ("tap", {"coordinate": [x, y], "target": target_idx}, desc)
        # Fallback to raw coordinate
        coord = parsed.get("coordinate", [0, 0])
        x, y = int(coord[0]), int(coord[1])
        x, y = _scale_coordinate(x, y, norm_w, norm_h)
        desc = target_desc if target_desc else f"tap({x},{y})"
        return ("tap", {"coordinate": [x, y]}, desc)

    elif action == "long_press":
        el_center = _resolve_target(target_idx, elements)
        if el_center is not None:
            x, y = el_center[0], el_center[1]
            desc = _desc_with_idx("LONG_PRESS", target_idx, target_desc)
            return ("long_press", {"coordinate": [x, y], "target": target_idx}, desc)
        coord = parsed.get("coordinate", [0, 0])
        x, y = int(coord[0]), int(coord[1])
        x, y = _scale_coordinate(x, y, norm_w, norm_h)
        desc = target_desc if target_desc else f"long_press({x},{y})"
        return ("long_press", {"coordinate": [x, y]}, desc)

    elif action == "swipe":
        c1 = parsed.get("coordinate1", [0, 0])
        c2 = parsed.get("coordinate2", [0, 0])
        dur = parsed.get("duration", 300)
        x1, y1 = _scale_coordinate(int(c1[0]), int(c1[1]), norm_w, norm_h)
        x2, y2 = _scale_coordinate(int(c2[0]), int(c2[1]), norm_w, norm_h)
        desc = target_desc if target_desc else f"swipe(({x1},{y1})→({x2},{y2}))"
        return ("swipe", {"coordinate1": [x1, y1], "coordinate2": [x2, y2], "duration": dur}, desc)

    elif action == "type":
        text = parsed.get("text", "")
        desc = f"type({text[:30]})"
        return ("type", {"text": text}, desc)

    elif action == "system_button":
        btn = parsed.get("button", "back")
        desc = f"press({btn})"
        return ("system_button", {"button": btn}, desc)

    elif action == "open_app":
        app = parsed.get("app_name", "")
        # Try to resolve app display name to Android package name
        pkg = APP_PACKAGE_MAP.get(app, app)
        desc = f"open_app({app})"
        return ("open", {"text": pkg}, desc)

    elif action == "fail":
        reason = parsed.get("reason", "unknown")
        _log(f"VLM reports task cannot be completed: {reason}")
        return ("finish", {}, f"Task failed: {reason}")

    else:  # finish or unknown
        return ("finish", {}, "task complete")


def _decide_action_plan_guided(
    instruction: str,
    step: int,
    elements: list,
    incident_tracker,
    last_coords: list[str],
    screenshot_path,
    previous_actions: list[str] = None,
    plan_text: str = "",
    history=None,
    scratchpad: dict[str, str] | None = None,
    similarity_note: str = "",
    targeted_indices: dict[int, int] | None = None,
    action_loop_note: str = "",
) -> tuple[str, dict, str, dict | None, dict | None]:
    """Call the VLM with screenshot + element index + plan context to decide the next action.

    Like ``_decide_action_fallback`` but includes the current plan state,
    step history, and scratchpad notes rendered from the HistoryStore.
    Expects plan progress updates in the response.

    Returns (action_type, action_args, human_readable_description, plan_updates_dict, notes_updates).
    On VLM failure, returns ("system_button", {"button": "back"}, "VLM failure: back", None, None).
    """
    if previous_actions is None:
        previous_actions = []
    if targeted_indices is None:
        targeted_indices = {}

    # Compute normalized coordinate space early — needed for element centers
    norm_w, norm_h = _get_norm_dimensions(screenshot_path)

    if elements:
        MAX_ELEMENTS = 80
        shown = elements[:MAX_ELEMENTS]
        # Build the header with a per-element "already tried" marker
        stuck_any = any(targeted_indices.get(el.index, 0) >= 2 for el in shown)
        lines = [f"UI ELEMENTS ({len(shown)} shown of {len(elements)} indexed):"]
        if stuck_any:
            lines.append("⚠️  Elements marked ALREADY TAPPED N× were tried and the screen did NOT change. DO NOT target them.")
        for el in shown:
            marker = ""
            tcnt = targeted_indices.get(el.index, 0)
            if tcnt >= 2:
                marker = f"  ⚠️ ALREADY TAPPED {tcnt}× — DO NOT TARGET"
            elif tcnt == 1:
                marker = "  (tapped once)"
            lines.append(el.render_index_line() + marker)
        index_text = "\n".join(lines)
    else:
        index_text = "UI ELEMENTS: (unavailable — use visual analysis)"
        stuck_any = False

    # Previous actions
    prev_text = ""
    if previous_actions:
        recent = previous_actions[-6:]
        prev_text = "PREVIOUS ACTIONS (do NOT repeat these):\n" + "\n".join(
            f"  - {a}" for a in recent
        ) + "\n"

    # Incident context
    incident_text = ""
    if incident_tracker and incident_tracker.count > 0:
        incident_text = f"PREVIOUS INCIDENTS:\n{incident_tracker.render_for_prompt()}\n"

    # History context (step records from HistoryStore)
    history_block = ""
    if history and history.step_count > 0:
        history_block = history.render_for_prompt() + "\n\n"

    # Loop warning
    loop_text = ""
    if len(last_coords) >= LOOP_THRESHOLD:
        unique = len(set(last_coords))
        if unique == 1:
            loop_text = "⚠️  LOOP DETECTED: same coordinate tapped 3+ times. Try a DIFFERENT approach.\n"

    # Scratchpad notes (persistent cross-turn memory)
    scratchpad_block = ""
    if scratchpad:
        lines = ["## Scratchpad Notes"]
        for key, content in scratchpad.items():
            # Show key + first 200 chars of content
            preview = content[:200] + "…" if len(content) > 200 else content
            lines.append(f"  [{key}]: {preview}")
        scratchpad_block = "\n".join(lines) + "\n\n"

    # Plan context
    plan_block = ""
    if plan_text:
        try:
            snapshot = parse_plan(plan_text)
            plan_block = snapshot.render_for_prompt() + "\n\n"
        except Exception:
            plan_block = f"CURRENT PLAN:\n{plan_text}\n\n"

    # Assemble system prompt: model-aware base + plan tracking
    system_prompt = _get_operator_system_prompt() + "\n\n" + build_operator_plan_system_prompt()

    # TARGETING RULES — placed BEFORE the element list so the VLM reads them first.
    # When stuck, the rules explicitly forbid targeting already-tried elements.
    if stuck_any:
        targeting_rules = (
            "\n\n## TARGETING RULES — STUCK DETECTED"
            "\n1. Elements marked 'ALREADY TAPPED N×' have been tried and the screen DID NOT CHANGE."
            "\n   DO NOT output target=N for these elements."
            "\n2. Pick a DIFFERENT element, use a coordinate tap, or try type/swipe/back."
            "\n3. If no other element makes sense, output {\"action\":\"finish\"} or {\"action\":\"fail\"}."
        )
    else:
        targeting_rules = (
            "\n\n## TARGETING RULES"
            "\n1. If the target IS in the element list → output {\"action\":\"tap\",\"target\":N}"
            "\n   using the INDEX number. The system resolves to exact pixel. Do NOT guess coordinates."
            "\n2. ONLY use {\"action\":\"tap\",\"coordinate\":[x,y]} if NO element matches your target"
            "\n   (custom-drawn UI, Canvas, WebView, Flutter, or splash ads not in the list)."
        )

    user_prompt = (
        f"TASK: {instruction}\n"
        + (f"{history_block}\n" if history_block else "")
        + (f"{scratchpad_block}" if scratchpad_block else "")
        + f"\n{plan_block}"
        f"Step: {step}\n"
        + (f"\n{prev_text}" if prev_text else "")
        + (f"\n{loop_text}" if loop_text else "")
        + (f"\n{incident_text}" if incident_text else "")
        + (f"\n{similarity_note}" if similarity_note else "")
        + (f"\n{action_loop_note}" if action_loop_note else "")
        + targeting_rules
        + f"\n\n{index_text}"
    )

    user_prompt += (
        "\n\nCOORDINATE SPACE: When using coordinate (fallback only), output in 0-1000 range."
        " They will be auto-scaled to device pixels."
    )

    # Plan-guided operator needs more tokens for plan_updates + notes fields
    raw, finish_reason, norm_w, norm_h = _call_vlm(system_prompt, user_prompt, screenshot_path, max_tokens=2048)
    if not raw:
        _log("Operator VLM returned no response — retrying once after pause")
        time.sleep(2)
        raw, finish_reason, norm_w, norm_h = _call_vlm(system_prompt, user_prompt, screenshot_path, max_tokens=2048)
        if not raw:
            _log("Operator VLM failed after retry — returning system_button: back")
            return ("system_button", {"button": "back"}, "VLM failure: back", None, None)

    if finish_reason == "length" and "{" not in raw:
        _log("VLM output truncated (length) — injecting back action to retry")
        return ("system_button", {"button": "back"}, "truncated: back", None, None)

    # Robust JSON extraction with truncation repair
    # Try XML <tool_calls> format FIRST on the original raw text, before any
    # stripping (the tag-stripping regex would destroy <tool_calls> / <invoke>).
    repaired = _repair_json(raw, finish_reason)
    if repaired:
        pass
    else:
        json_str = raw
        think_end = json_str.rfind("</")
        if think_end >= 0:
            json_str = json_str[think_end:]
        json_str = re.sub(r"<[^>]+/?>", "", json_str)
        json_str = re.sub(r"```\w*\n?", "", json_str)
        repaired = _repair_json(json_str, finish_reason)
    if not repaired:
        _debug(f"VLM output has no valid JSON: {raw[:300]}")
        return ("system_button", {"button": "back"}, f"no JSON in response: {raw[:80]}", None, None)

    try:
        parsed = json.loads(repaired)
    except json.JSONDecodeError:
        _debug(f"VLM JSON parse failed after repair: {json_str[:300]}")
        return ("system_button", {"button": "back"}, f"VLM parse error: {json_str[:80]}", None, None)

    # Extract plan_updates and notes before normalizing action
    plan_updates = parsed.get("plan_updates")
    notes_updates = parsed.get("notes")

    action = parsed.get("action", "finish")
    target_desc = parsed.get("target_description", "")
    target_idx = parsed.get("target")  # element index (preferred over raw coordinates)
    _log(f"[vlm-decision] action={action} target={target_idx} desc={target_desc} coords={parsed.get('coordinate', parsed.get('coordinate1', ''))}")

    # Resolve element index if the VLM used target=N
    def _resolve_target_plan(idx, elements_ref):
        if idx is None:
            return None
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            return None
        if elements_ref and 0 <= idx < len(elements_ref):
            return elements_ref[idx].center
        return None

    # Normalize action_type and args (same logic as _decide_action_fallback)
    if action == "tap":
        el_center = _resolve_target_plan(target_idx, elements)
        if el_center is not None:
            x, y = el_center[0], el_center[1]
            desc = _desc_with_idx("TAP", target_idx, target_desc)
            return ("tap", {"coordinate": [x, y], "target": target_idx}, desc, plan_updates, notes_updates)
        coord = parsed.get("coordinate", [0, 0])
        x, y = int(coord[0]), int(coord[1])
        x, y = _scale_coordinate(x, y, norm_w, norm_h)
        desc = target_desc if target_desc else f"tap({x},{y})"
        return ("tap", {"coordinate": [x, y]}, desc, plan_updates, notes_updates)

    elif action == "long_press":
        el_center = _resolve_target_plan(target_idx, elements)
        if el_center is not None:
            x, y = el_center[0], el_center[1]
            desc = _desc_with_idx("LONG_PRESS", target_idx, target_desc)
            return ("long_press", {"coordinate": [x, y], "target": target_idx}, desc, plan_updates, notes_updates)
        coord = parsed.get("coordinate", [0, 0])
        x, y = int(coord[0]), int(coord[1])
        x, y = _scale_coordinate(x, y, norm_w, norm_h)
        desc = target_desc if target_desc else f"long_press({x},{y})"
        return ("long_press", {"coordinate": [x, y]}, desc, plan_updates, notes_updates)

    elif action == "swipe":
        c1 = parsed.get("coordinate1", [0, 0])
        c2 = parsed.get("coordinate2", [0, 0])
        dur = parsed.get("duration", 300)
        x1, y1 = _scale_coordinate(int(c1[0]), int(c1[1]), norm_w, norm_h)
        x2, y2 = _scale_coordinate(int(c2[0]), int(c2[1]), norm_w, norm_h)
        desc = target_desc if target_desc else f"swipe(({x1},{y1})→({x2},{y2}))"
        return ("swipe", {"coordinate1": [x1, y1], "coordinate2": [x2, y2], "duration": dur}, desc, plan_updates, notes_updates)

    elif action == "type":
        text = parsed.get("text", "")
        desc = f"type({text[:30]})"
        return ("type", {"text": text}, desc, plan_updates, notes_updates)

    elif action == "system_button":
        btn = parsed.get("button", "back")
        desc = f"press({btn})"
        return ("system_button", {"button": btn}, desc, plan_updates, notes_updates)

    elif action == "open_app":
        app = parsed.get("app_name", "")
        pkg = APP_PACKAGE_MAP.get(app, app)
        desc = f"open_app({app})"
        return ("open", {"text": pkg}, desc, plan_updates, notes_updates)

    elif action == "fail":
        reason = parsed.get("reason", "unknown")
        _log(f"VLM reports task cannot be completed: {reason}")
        return ("finish", {}, f"Task failed: {reason}", plan_updates, notes_updates)

    else:  # finish or unknown
        return ("finish", {}, "task complete", plan_updates, notes_updates)


def _execute_action_adb(
    adb_path: str,
    device: str,
    action_type: str,
    action_args: dict,
    elements: list | None = None,
) -> None:
    """Execute an action via ADB."""
    if action_type == "tap":
        coord = action_args.get("coordinate", [0, 0])
        _adb(["shell", "input", "tap", str(coord[0]), str(coord[1])],
             adb_path=adb_path, device=device)
    elif action_type == "swipe":
        c1 = action_args.get("coordinate1", [0, 0])
        c2 = action_args.get("coordinate2", [0, 0])
        dur = action_args.get("duration", 300)
        _adb(["shell", "input", "swipe",
              str(c1[0]), str(c1[1]), str(c2[0]), str(c2[1]), str(dur)],
             adb_path=adb_path, device=device)
    elif action_type == "type":
        text = action_args.get("text", "")
        _adb_type_text(adb_path, device, text, elements=elements)
    elif action_type == "system_button":
        btn = action_args.get("button", "").upper()
        key_map = {"HOME": "3", "BACK": "4", "ENTER": "66", "APP_SWITCH": "187"}
        code = key_map.get(btn, "3")
        _adb(["shell", "input", "keyevent", code],
             adb_path=adb_path, device=device)
    elif action_type == "open":
        app = action_args.get("text", "")
        pkg = _resolve_package(adb_path, device, app)
        if pkg:
            _log(f"open_app: resolved '{app}' → '{pkg}'")
            # Try monkey first (reliable for known packages)
            rc, _, _ = _adb(
                ["shell", "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"],
                adb_path=adb_path, device=device, timeout=10,
            )
            if rc != 0:
                # Fallback: am start with MAIN/LAUNCHER intent
                _adb(
                    ["shell", "am", "start", "-a", "android.intent.action.MAIN",
                     "-c", "android.intent.category.LAUNCHER", pkg],
                    adb_path=adb_path, device=device, timeout=10,
                )
        else:
            _log(f"open_app: could not resolve '{app}' — trying raw monkey")
            _adb(["shell", "monkey", "-p", app, "1"],
                 adb_path=adb_path, device=device)
    elif action_type == "finish":
        pass  # No ADB action needed

def _resolve_package(adb_path: str, device: str, app_name: str) -> str | None:
    """Resolve an app display name to a package name.

    Tries in order:
      1. Static APP_PACKAGE_MAP
      2. NAME_PACKAGE_DICT from packages.py (153-entry bidirectional map)
      3. Dynamic: ``pm list packages`` grep on the device
    Returns None if unresolvable.
    """
    # Tier 1: Static map
    pkg = APP_PACKAGE_MAP.get(app_name)
    if pkg:
        return pkg

    # Tier 2: packages.py bidirectional lookup
    normalized = normalize_package_name(app_name)
    candidates = NAME_PACKAGE_DICT.get(normalized)
    if candidates:
        return candidates[0]

    # Tier 3: grep installed packages on the device
    grep_term = app_name.lower().replace(" ", "")
    rc, out, _ = _adb(
        ["shell", "pm", "list", "packages", "|", "grep", "-i", grep_term],
        adb_path=adb_path, device=device, timeout=5,
    )
    if rc == 0 and out:
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("package:"):
                return line[len("package:"):]

    return None


# ── uiautomator2 helpers (optional — primary text input method) ──────────

def _u2_connect(device: str | None = None):
    """Connect to a device via uiautomator2, backward-compatibly.

    uiautomator2 v3.x added ``init_atx_agent`` to ``connect()``;
    older versions (2.x) and the latest v3.x (which removed it)
    reject it with TypeError.  This wrapper tries the v3 signature
    first and falls back gracefully.
    """
    import uiautomator2 as u2

    try:
        # v3.x intermediate path — skip atx-agent init (it's already running)
        return (
            u2.connect(device, init_atx_agent=False)
            if device
            else u2.connect(init_atx_agent=False)
        )
    except TypeError:
        # v2.x / latest v3.x path — connect() takes only serial
        return u2.connect(device) if device else u2.connect()


def _u2_disable_fast_ime(d) -> None:
    """Disable ATX FastInputIME so ACTION_SET_TEXT works without IME switching."""
    try:
        d.set_input_ime(False)
    except AttributeError:
        try:
            d.set_fastinput_ime(False)  # deprecated but still functional
        except AttributeError:
            pass


def _u2_find_editable_element(ui_xml: str) -> dict | None:
    """Locate the best editable element (EditText / SearchView) in a UI XML dump.

    Returns ``{"center": (x, y), "class": ..., "text": ...}`` or None.
    Prefers already-focused elements, then search-related hints, then any editable.
    """
    import xml.etree.ElementTree as ET

    if not ui_xml:
        return None
    try:
        root = ET.fromstring(ui_xml)
    except ET.ParseError:
        return None

    candidates: list[dict] = []

    for node in root.iter("node"):
        cls = (node.attrib.get("class", "") or "").lower()
        is_editable = any(
            kw in cls
            for kw in ("edittext", "edit", "input", "searchview", "search")
        )
        if not is_editable:
            continue
        focusable = node.attrib.get("focusable", "false") == "true"
        focused = node.attrib.get("focused", "false") == "true"
        bounds_str = node.attrib.get("bounds", "")
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
        if not m:
            continue
        x1, y1, x2, y2 = int(m[1]), int(m[2]), int(m[3]), int(m[4])
        if x2 <= x1 or y2 <= y1:
            continue

        candidates.append({
            "center": ((x1 + x2) // 2, (y1 + y2) // 2),
            "class": node.attrib.get("class", ""),
            "text": node.attrib.get("text", ""),
            "hint": node.attrib.get("hint", ""),
            "focused": focused,
            "focusable": focusable,
        })

    if not candidates:
        return None
    # Prefer already-focused
    for c in candidates:
        if c["focused"]:
            return c
    # Then prefer ones with a search hint
    for c in candidates:
        if any(kw in (c.get("hint", "") or "").lower()
               for kw in ("search", "搜索", "查找")):
            return c
    # Then return the first one
    return candidates[0]


def _u2_set_text(device: str, text: str, adb_path: str = "adb") -> bool:
    """Primary: type text via uiautomator2 AccessibilityService (NO extra APK required).

    Uses ``d(focused=True).set_text()`` which triggers ``ACTION_SET_TEXT`` on the
    focused accessibility node — no keyboard, no IME switch, no ADB Keyboard APK needed.

    When no element has focus, auto-locates an EditText/SearchView via uiautomator2's
    ``d.dump_hierarchy()`` (NOT ``adb shell uiautomator dump`` — that conflicts with
    the ATX agent's UiAutomationService hold), taps it, then retries set_text.
    """
    try:
        import uiautomator2 as u2

        d = _u2_connect(device)
        _u2_disable_fast_ime(d)

        # Try set_text on currently-focused element first
        try:
            d(focused=True).set_text(text)
            _log(f"type: u2 set_text on focused element OK ({len(text)} chars)")
            return True
        except Exception as _e1:
            _err_str = str(_e1)
            _is_focus_error = (
                "focused=True" in _err_str
                or "focused" in _err_str.lower()
            )
            if not _is_focus_error:
                raise
            _log("type: u2 — no focused element, trying auto-focus...")

        # Auto-focus: use d.dump_hierarchy() (NOT adb uiautomator dump)
        # The ADB uiautomator dump uses a separate UiAutomationService instance
        # that conflicts with u2's ATX agent.  d.dump_hierarchy() uses u2's
        # own connection — no conflict.
        xml_str = None
        for _dmp_attempt in range(3):
            try:
                xml_str = d.dump_hierarchy()
                if xml_str and xml_str.count("<node") >= 5:
                    break
                xml_str = None
            except Exception as _dmp_err:
                _dmp_msg = str(_dmp_err)
                if "already registered" in _dmp_msg or "UiAutomation" in _dmp_msg:
                    _kill_stale_uiautomation_service(adb_path, device)
                    if _dmp_attempt < 2:
                        time.sleep(0.5 * (_dmp_attempt + 1))
                        continue
                _log(f"type: u2 dump_hierarchy failed: {_dmp_msg}")

        if not xml_str:
            _log("type: u2 auto-focus — dump_hierarchy failed after retries")
            return False

        target = _u2_find_editable_element(xml_str)
        if not target:
            _log("type: u2 auto-focus — no editable element found in UI dump")
            return False

        bx, by = target["center"]
        _log(
            f"type: u2 auto-focus tapping {target['class'][:40]} "
            f"at ({bx},{by}) text={target.get('text','')[:30]}"
        )
        d.click(bx, by)
        time.sleep(0.5)

        d(focused=True).set_text(text)
        _log(f"type: u2 auto-focus + set_text OK ({len(text)} chars)")
        return True

    except ImportError:
        _log("type: uiautomator2 not installed — skipping u2 (primary method)")
        return False
    except Exception as e:
        _log(f"type: u2 set_text failed: {e}")
        return False


def _kill_stale_uiautomation_service(adb_path: str, device: str) -> None:
    """Kill stale ADB uiautomator dump processes that block uiautomator2.

    Only kills ``/system/bin/uiautomator`` (ADB dump binary) and
    ``com.wetest.uia2`` — never touches atx-agent or the uiautomator2 APK.
    """
    for pattern in ["uiautomator dump", "com.wetest.uia2"]:
        try:
            _adb(
                ["shell", "pkill", "-f", pattern],
                adb_path=adb_path, device=device, timeout=5,
            )
        except Exception:
            pass


def _adb_focus_input_via_uidump(adb_path: str, device: str) -> bool:
    """Dump UI XML via uiautomator and tap the first EditText/SearchView found.

    No extra Python libraries required — uses built-in ``adb shell uiautomator dump``.
    Returns True if an input element was found and tapped.
    """
    import xml.etree.ElementTree as ET

    dump_path = "/sdcard/_aclip_ui.xml"
    rc, _, _ = _adb(
        ["shell", "uiautomator", "dump", dump_path],
        adb_path=adb_path, device=device, timeout=10,
    )
    if rc != 0:
        return False

    # Pull the XML from device (uiautomator dump writes to device path)
    rc, xml_str, _ = _adb(
        ["shell", "cat", dump_path],
        adb_path=adb_path, device=device, timeout=5,
    )
    _adb(["shell", "rm", "-f", dump_path], adb_path=adb_path, device=device)
    if rc != 0 or not xml_str:
        return False

    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return False

    EDITABLE_CLASSES = {"EditText", "SearchView", "AutoCompleteTextView", "MultiAutoCompleteTextView"}

    def _find_editable(node, results):
        cls = (node.attrib.get("class", "") or "").split(".")[-1]
        if cls in EDITABLE_CLASSES:
            bounds = node.attrib.get("bounds", "")
            # Parse bounds="[left,top][right,bottom]"
            import re as _re
            m = _re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds)
            if m:
                left, top, right, bottom = map(int, m.groups())
                # Check if clickable/focusable/enabled
                clickable = node.attrib.get("clickable", "false") == "true"
                enabled = node.attrib.get("enabled", "true") == "true"
                if enabled:  # Tappable even if not explicitly clickable
                    cx = (left + right) // 2
                    cy = (top + bottom) // 2
                    results.append((cx, cy, cls))
        for child in node:
            _find_editable(child, results)

    candidates = []
    _find_editable(root, candidates)

    if candidates:
        cx, cy, cls = candidates[0]
        _adb(
            ["shell", "input", "tap", str(cx), str(cy)],
            adb_path=adb_path, device=device, timeout=5,
        )
        _log(f"type: auto-focused {cls} at ({cx},{cy}) [uidump]")
        time.sleep(0.3)
        return True

    return False


def _adb_type_text(
    adb_path: str,
    device: str,
    text: str,
    elements: list | None = None,
) -> bool:
    """Type text on the device via a tiered fallback chain.

    **Tier 0 (primary):** uiautomator2 ``set_text()`` — uses the ATX agent's
    AccessibilityService to directly set text on the focused node via
    ACTION_SET_TEXT.  No keyboard, no IME, no ADB Keyboard APK required.
    If no element is focused, auto-finds an EditText/SearchView via UI dump,
    taps it via u2, then retries set_text.

    **Tier 1:** Clipboard paste — base64-encode, write to temp file, decode
    on-device, ``cmd clipboard set``, PASTE keyevent (279), cleanup.
    Requires an element with IME focus (not just tap focus).

    **Tier 2 (last resort):** ``adb shell input text`` with single-quote
    escaping and literal spaces.

    Returns True if text was successfully typed, False otherwise.
    """
    # ── Tier 0: uiautomator2 set_text (primary, no IME dependency) ──────
    if _u2_set_text(device, text, adb_path=adb_path):
        return True

    # ── Step 1: Auto-focus an input field (for clipboard/input_text tiers) ─
    focused = False

    # 1a: Try element list first (fast, no extra ADB call)
    if elements:
        EDITABLE_CLASSES = ("EditText", "SearchView", "AutoCompleteTextView", "MultiAutoCompleteTextView")
        for el in elements:
            cls = (el.class_name or "").split(".")[-1]
            if cls in EDITABLE_CLASSES and el.clickable and el.enabled:
                cx, cy = el.center
                _adb(
                    ["shell", "input", "tap", str(cx), str(cy)],
                    adb_path=adb_path, device=device, timeout=5,
                )
                _log(f"type: auto-focused {cls} at ({cx},{cy}) [elements]")
                focused = True
                time.sleep(0.3)
                break

    # 1b: Fallback — dump UI XML to find any EditText/SearchView
    if not focused:
        focused = _adb_focus_input_via_uidump(adb_path, device)

    # ── Tier 1: Clipboard paste ─────────────────────────────────────────
    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    clip_b64 = "/sdcard/_aclip_b64"
    clip_txt = "/sdcard/_aclip_txt"

    # Write base64 to device temp file (base64 chars are shell-safe)
    rc, _, _ = _adb(
        ["shell", "printf", "%s", b64, ">", clip_b64],
        adb_path=adb_path, device=device, timeout=5,
    )
    if rc == 0:
        # Decode → set clipboard → PASTE → cleanup (single device-shell invocation)
        rc2, _, _ = _adb(
            ["shell",
             f"base64 -d {clip_b64} > {clip_txt} && "
             f"cmd clipboard set \"$(cat {clip_txt})\" && "
             f"input keyevent 279 && "
             f"rm -f {clip_b64} {clip_txt}"],
            adb_path=adb_path, device=device, timeout=10,
        )
        if rc2 == 0:
            _log(f"type: clipboard paste OK ({len(text)} chars)")
            return True
        # Best-effort cleanup on failure
        _adb(["shell", "rm", "-f", clip_b64, clip_txt],
             adb_path=adb_path, device=device, timeout=5)

    # ── Tier 2: adb shell input text (fallback) ─────────────────────────
    # Escape single quotes; literal spaces work with 'input text' (no %s needed)
    escaped = text.replace("'", "\\'")
    rc3, _, _ = _adb(
        ["shell", "input", "text", f"'{escaped}'"],
        adb_path=adb_path, device=device, timeout=5,
    )
    if rc3 == 0:
        _log(f"type: input text OK ({len(text)} chars)")
        return True

    _log(f"type: ALL methods failed for text ({len(text)} chars)")
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Artemis mobile agent skill — wraps Google Research's Artemis engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Exit codes:
              0  success
              1  runtime error
              2  clarification needed
              3  timeout / step limit
              4  ADB device not found
        """),
    )
    p.add_argument("--instruction", required=True, help="Natural language task")
    p.add_argument("--profile", choices=["flash", "pro"], default="flash",
                   help="Execution profile: flash (fast) or pro (full safety+verify)")
    p.add_argument("--device", default=None, help="ADB device serial")
    p.add_argument("--max_steps", type=int, default=DEFAULT_MAX_STEPS)
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p.add_argument("--verification", choices=["off", "final", "checkpoints", "strict"],
                   default="final", help="Verification level (pro profile only)")
    p.add_argument("--explorer", choices=["flash", "pro", "ultra"], default="pro",
                   help="Explorer element-locating tier")
    p.add_argument("--dry_run", action="store_true",
                   help="Only run pre-checks, skip task execution")
    p.add_argument("--debug", action="store_true",
                   help="Verbose logging")
    p.add_argument("--log-file", default="", dest="log_file",
                   help="Also write all logs to this file")
    return p.parse_args()


def main() -> int:
    global _LOG_FILE, _DEBUG
    args = parse_args()

    _DEBUG = args.debug
    cfg = load_skill_config()
    _log(f"artemis_agent.py v{VERSION} — model={cfg['model']} base_url={cfg['base_url']} profile={args.profile} instruction={args.instruction!r}")
    if args.log_file:
        try:
            _LOG_FILE = open(args.log_file, "a", encoding="utf-8")
            _log(f"=== artemis-control session start === instruction={args.instruction!r} profile={args.profile}")
        except OSError as e:
            print(f"[artemis-control] WARNING: cannot open log file {args.log_file}: {e}",
                  file=sys.stderr)

    # 1. Clarification check
    if needs_clarification(args.instruction):
        _emit({
            "type": "result",
            "status": "clarify",
            "steps": 0,
            "last_action": "",
            "message": (
                "Instruction is too vague. "
                "Please specify: which app and what action? "
                "Example: '在微信发消息给妈妈说我到家了'"
            ),
            "actions": [],
        })
        return 2

    # 2. ADB device check
    device = check_adb_device("adb", args.device)
    if device is None:
        _emit({
            "type": "result",
            "status": "error",
            "steps": 0,
            "last_action": "",
            "message": (
                "No ADB device found. "
                "Please connect your Android phone via USB and enable USB Debugging. "
                "Then run: adb devices"
            ),
            "actions": [],
        })
        return 4

    _log(f"Using device: {device}")

    # 3. Screen setup
    ensure_screen_on("adb", device)

    # 4. Profile routing
    _log(f"Profile: {args.profile} — {'full multi-agent graph' if args.profile == 'pro' else 'plan + reactive loop'}")

    if args.dry_run:
        _emit({
            "type": "result",
            "status": "success",
            "steps": 0,
            "last_action": "",
            "message": f"Dry run OK — device {device} ready. model={cfg['model']} base_url={cfg['base_url']} version={VERSION}",
            "actions": [],
        })
        return 0

    # 5. Run the agent
    result = run_artemis_agent(
        instruction=args.instruction,
        adb_path="adb",
        device=device,
        profile=args.profile,
        max_steps=args.max_steps,
        timeout=args.timeout,
        verification=args.verification,
        explorer_tier=args.explorer,
    )

    # 6. Emit result
    _emit(result)

    status_exit = {
        "success": 0,
        "timeout": 3,
        "clarify": 2,
        "error": 1,
    }
    return status_exit.get(result["status"], 1)


if __name__ == "__main__":
    sys.exit(main())