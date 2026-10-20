#!/usr/bin/env python3
"""
artemis_agent.py — OpenClaw skill wrapper for Artemis (Google Research)

Wraps the Artemis multi-agent engine with:
  - ADB pre-checks (device connectivity, screen state)
  - Profile selection (flash / pro)
  - Safety net integration (XML + pixel precondition validation)
  - Execution incident tracking with self-healing
  - Explorer element indexing
  - Structured JSONL output for OpenClaw to consume

Key architectural imports from Artemis core:
  - artemis.sdk.agent.Agent          — main execution engine
  - artemis.agents.validator.*       — precondition_xml, precondition_pixel
  - artemis.agents.explorer.*        — screen_index, tiers, exploration
  - artemis.agents.validator.incidents — ExecutionIncident tracking

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
from datetime import datetime
from pathlib import Path
from typing import Optional

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

# VLM system prompt for action decisions
SYSTEM_PROMPT_FALLBACK = """You are a phone automation agent controlling an Android device via ADB.
Your job: look at the screenshot, read the element index, and decide the NEXT SINGLE ACTION to take toward the user's goal.

AVAILABLE ACTIONS (return as JSON):
- {"action": "tap", "coordinate": [x, y]}           — tap at pixel (x, y). Use element index to find coords.
- {"action": "long_press", "coordinate": [x, y]}     — long-press at (x, y)
- {"action": "swipe", "coordinate1": [x1, y1], "coordinate2": [x2, y2], "duration": 300}
- {"action": "type", "text": "hello"}                — type text via ADB Keyboard
- {"action": "system_button", "button": "back"}      — press: back, home, enter, recent_apps
- {"action": "open_app", "app_name": "微信"}          — open an app by name
- {"action": "finish"}                                — task is complete
- {"action": "fail", "reason": "why"}                 — task cannot be completed

RULES:
1. Return EXACTLY ONE action as a JSON object, nothing else.
2. Use the element index for precise coordinates. Elements have format:
   [N] class="..." text="..." content_desc="..." bounds="[x1,y1][x2,y2]" center=(cx,cy)
3. If the current screen doesn't show what you need, try swipe to scroll or press back.
4. If the task is done, return {"action": "finish"}.
5. Think step by step, but only output the final JSON."""

def load_skill_config() -> dict:
    """Load config.json, falling back to hard-coded defaults."""
    global _CONFIG
    if _CONFIG is not None:
        return _CONFIG

    defaults = {
        "api_key": "not-needed",
        "model": "gui-owl",
        "base_url": "http://apicn.aiworm.cn:8809/v1",
    }

    try:
        if _CONFIG_PATH.exists():
            raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            provider = raw.get("provider", {})
            if provider and provider.get("api_key", "").strip():
                defaults["api_key"] = provider["api_key"].strip()
                defaults["model"] = provider.get("model", defaults["model"])
                defaults["base_url"] = provider.get("base_url", defaults["base_url"])
            else:
                fb = raw.get("fallback_provider", {})
                if fb:
                    defaults["api_key"] = fb.get("api_key", defaults["api_key"])
                    defaults["model"] = fb.get("model", defaults["model"])
                    defaults["base_url"] = fb.get("base_url", defaults["base_url"])
    except Exception as e:
        _trace(f"[config] Failed to load config.json: {e}")

    _CONFIG = defaults
    _trace(f"[config] model={defaults['model']} base_url={defaults['base_url']} api_key={'<set>' if defaults['api_key'] else '<empty>'}")
    return defaults

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

def _emit(obj: dict) -> None:
    """Print a JSON object to stdout — consumed by OpenClaw."""
    print(json.dumps(obj, ensure_ascii=False), flush=True)

def _emit_progress(step: int, action: str, message: str,
                   screenshot: Optional[str] = None,
                   verified: Optional[bool] = None,
                   safety_check: Optional[str] = None) -> None:
    """Emit a JSONL progress line."""
    obj: dict = {"type": "progress", "step": step, "action": action, "message": message}
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

def check_artemis_available() -> bool:
    """Check if the Artemis Python package is importable."""
    try:
        import artemis  # noqa: F401
        return True
    except ImportError:
        return False

def _find_artemis_python() -> Optional[str]:
    """Find the Python interpreter with Artemis installed."""
    candidates = [
        "/workspace/artemis/.venv/bin/python",
        "/workspace/artemis/.venv/bin/python3",
    ]
    for p in candidates:
        if Path(p).exists():
            return p
    # Fall back to system python
    try:
        subprocess.run(
            [sys.executable, "-c", "import artemis"],
            capture_output=True, timeout=5
        )
        return sys.executable
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Core runner: wraps Artemis SDK Agent
# ---------------------------------------------------------------------------

SCREENSHOTS_ROOT_ARTEMIS: Optional[Path] = None

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
    """
    Run the Artemis multi-agent engine and emit progress events.
    Falls back to a pure-ADB reactive loop if Artemis is not importable.
    """
    if not check_artemis_available():
        _log("Artemis SDK not importable — falling back to reactive ADB loop")
        return _run_reactive_fallback(instruction, adb_path, device, max_steps, timeout)

    _log(f"Launching Artemis engine: profile={profile}, explorer={explorer_tier}, verification={verification}")

    screenshots_dir = _make_run_screenshots_dir()
    start = time.time()
    actions: list[str] = []
    step = 0
    status = "timeout"
    last_action = ""
    end_reason = "runner_started"
    verification_result = {"passed": 0, "failed": 0, "inconclusive": 0}

    _trace(f"[run] profile={profile} instruction={instruction!r} device={device}")

    try:
        # Attempt to use Artemis SDK
        import asyncio as _asyncio

        from artemis.sdk.agent import Agent
        from artemis.sdk.types.agent import AgentConfig
        from artemis.sdk.builders import Builders

        # Build config
        builder = Builders.AgentConfig
        builder.for_device("android", device)

        if profile == "flash":
            builder.with_profile("flash")
        else:
            builder.with_profile("pro")

        if verification == "off":
            builder.disable_checker()
        elif verification == "strict":
            builder.with_verification_level("strict")
        elif verification == "checkpoints":
            builder.with_verification_level("checkpoints")
        else:
            builder.with_verification_level("final")

        if explorer_tier:
            builder.with_explorer(explorer_tier)

        config = builder.build()
        agent = Agent(config=config, device_serial=device)

        # Run the task
        async def _run():
            result = await agent.run(instruction)
            return result

        loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(_run())
        finally:
            loop.close()

        if result and result.succeeded:
            status = "success"
            end_reason = "artemis_completed"
            # Extract steps from result
            if hasattr(result, 'steps'):
                step = result.steps
            if hasattr(result, 'actions'):
                actions = result.actions
            if hasattr(result, 'verification'):
                verification_result = result.verification

            _emit_progress(step, "finish",
                           f"Step {step}: Task finished ✓ (Artemis {profile})",
                           screenshot=None)
        else:
            error_msg = getattr(result, 'error', 'Unknown error') if result else "No result"
            status = "error"
            end_reason = f"artemis_failed: {error_msg}"
            _log(f"Artemis task failed: {error_msg}")

    except ImportError as e:
        _log(f"Artemis SDK missing: {e} — using fallback loop")
        return _run_reactive_fallback(instruction, adb_path, device, max_steps, timeout)

    except Exception as e:
        _log(f"Artemis execution error: {e}")
        _trace(f"[error] {e}")
        status = "error"
        end_reason = f"artemis_exception: {e}"

    # Clean up screenshots
    try:
        if screenshots_dir.exists():
            shutil.rmtree(screenshots_dir, ignore_errors=True)
    except Exception:
        pass

    message_map = {
        "success": f"Task completed in {step} steps. Verification: {verification_result['passed']}/{verification_result['passed'] + verification_result['failed'] + verification_result['inconclusive']} checkpoints passed.",
        "timeout": f"Timed out after {int(time.time()-start)}s / {step} steps.",
        "error": f"Execution error after {step} steps.",
    }
    message = message_map.get(status, f"Status: {status} after {step} steps.")
    message += f" [reason: {end_reason}]"

    return {
        "type": "result",
        "status": status,
        "steps": step,
        "last_action": last_action,
        "message": message,
        "actions": actions,
        "verification": verification_result,
        "debug": {
            "end_reason": end_reason,
            "profile": profile,
            "explorer_tier": explorer_tier,
        },
    }


def _run_reactive_fallback(
    instruction: str,
    adb_path: str,
    device: str,
    max_steps: int,
    timeout: int,
) -> dict:
    """
    Fallback reactive observe→think→act loop when Artemis SDK is not available.
    Uses the safety net and explorer modules from this skill directly.
    """
    from runner.safety_net import SafetyNet, SafetyNetConfig
    from runner.incidents import IncidentTracker
    from runner.explorer import ElementIndexer

    _log(f"Running reactive fallback loop v{VERSION} — model={load_skill_config()['model']} — safety net + explorer")

    screenshots_dir = _make_run_screenshots_dir()
    start = time.time()
    actions: list[str] = []
    step = 0
    status = "timeout"
    end_reason = "fallback_started"
    last_coords: list[str] = []
    incident_tracker = IncidentTracker()
    safety_net = SafetyNet(SafetyNetConfig(
        adb_path=adb_path,
        device=device,
        xml_enabled=True,
        pixel_fallback=True,
        retry_attempts=3,
        retry_delay=0.4,
    ))
    indexer = ElementIndexer(adb_path=adb_path, device=device)

    _emit_progress(0, "init", f"Starting reactive loop for: {instruction[:60]}...")

    while step < max_steps and (time.time() - start) < timeout:
        step += 1
        _debug(f"Step {step}/{max_steps}")

        # 1. Take screenshot
        ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:19]
        shot_path = screenshots_dir / f"step_{step:03d}_{ts}_pre.png"
        if not _take_screenshot(adb_path, device, shot_path):
            _log("Screenshot capture failed")
            continue

        # 2. Index UI elements
        try:
            elements = indexer.build_index(shot_path)
            _debug(f"Indexed {len(elements)} UI elements")
        except Exception as e:
            _debug(f"Element indexing skipped: {e}")
            elements = []

        # 3. Determine action via VLM
        action_type, action_args, action_desc = _decide_action_fallback(
            instruction, step, elements, incident_tracker, last_coords, shot_path
        )

        # 4. Safety net check
        if action_type in ("tap", "long_press", "input_text"):
            passed, category, reason = safety_net.validate(
                action_type, action_args, shot_path
            )
            if not passed:
                incident_tracker.add_incident(
                    category=category,
                    reason=reason,
                    action=action_type,
                    step=step,
                    action_args=action_args,
                )
                _emit_incident(step, category, reason, action_type,
                               incident_tracker.consecutive_failures)
                _log(f"Safety net blocked: {category} — {reason}")
                continue
            else:
                incident_tracker.clear_on_success()
            safety_check = "xml_passed"
        else:
            safety_check = None

        # 5. Execute action
        _execute_action_adb(adb_path, device, action_type, action_args)
        actions.append(action_desc)
        time.sleep(1.2)

        # 6. Verification screenshot
        vshot_path = screenshots_dir / f"step_{step:03d}_{ts}_post.png"
        _take_screenshot(adb_path, device, vshot_path)

        _emit_progress(
            step, action_desc,
            f"Step {step}: {action_desc}",
            screenshot=str(vshot_path) if vshot_path.exists() else None,
            verified=(safety_check is not None),
            safety_check=safety_check,
        )

        # 7. Loop detection
        if action_type == "tap" and "coordinate" in action_args:
            coord_str = f"{action_args['coordinate'][0]},{action_args['coordinate'][1]}"
            last_coords.append(coord_str)
            if len(last_coords) > LOOP_THRESHOLD:
                last_coords = last_coords[-LOOP_THRESHOLD:]
            if len(last_coords) == LOOP_THRESHOLD and len(set(last_coords)) == 1:
                _log(f"Loop detected at {coord_str} — injecting recovery hint")
                incident_tracker.add_recovery_hint(
                    "Same coordinate tapped repeatedly. Try a different approach."
                )
        else:
            last_coords = []

        # 8. Check task completion
        if action_type == "finish":
            status = "success"
            end_reason = "fallback_completed"
            break

    # Cleanup
    try:
        if screenshots_dir.exists():
            shutil.rmtree(screenshots_dir, ignore_errors=True)
    except Exception:
        pass

    message = f"Fallback loop: {status} after {step} steps. [reason: {end_reason}]"
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
            "profile": "fallback",
            "incidents": incident_tracker.count,
        },
    }


def _encode_image(image_path: str) -> str:
    """Read an image file and return a base64 data URI."""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def _call_vlm(system_prompt: str, user_prompt: str, image_path: str,
              max_tokens: int = 256) -> Optional[str]:
    """Send a prompt + screenshot to the configured VLM and return its response text."""
    cfg = load_skill_config()
    base_url = cfg["base_url"].rstrip("/")
    api_key = cfg["api_key"]
    model = cfg["model"]

    data_uri = f"data:image/png;base64,{_encode_image(image_path)}"

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

    url = f"{base_url}/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result["choices"][0]["message"]["content"]
    except Exception as e:
        _log(f"VLM call failed ({url}): {e}")
        _trace(f"[vlm] call failed: {e}")
        return None


def _decide_action_fallback(
    instruction: str,
    step: int,
    elements: list,
    incident_tracker,
    last_coords: list[str],
    screenshot_path,
) -> tuple[str, dict, str]:
    """
    Call the VLM with screenshot + element index + instruction to decide the next action.

    Returns (action_type, action_args, human_readable_description).
    On VLM failure, returns ("finish", {}, "finish") to stop gracefully.
    """
    # Build element index text
    if elements:
        MAX_ELEMENTS = 80
        shown = elements[:MAX_ELEMENTS]
        lines = [f"UI ELEMENTS ({len(shown)} shown of {len(elements)} indexed):"]
        for el in shown:
            label = el.display_text
            rid = el.resource_id or ""
            rid_short = rid.split("/")[-1] if "/" in rid else rid
            cx, cy = el.center
            bounds_str = f"bounds=({el.bounds[0]},{el.bounds[1]},{el.bounds[2]},{el.bounds[3]})"
            cls_short = (el.class_name or "").split(".")[-1]
            lines.append(
                f'[{el.index}] "{label}" class={cls_short} {bounds_str} center=({cx},{cy})'
                + (f" rid={rid_short}" if rid_short else "")
                + (" clickable" if el.clickable and el.enabled else "")
                + (" disabled" if not el.enabled else "")
            )
        index_text = "\n".join(lines)
    else:
        index_text = "UI ELEMENTS: (unavailable — use visual analysis)"

    # Incident context
    incident_text = ""
    if incident_tracker and incident_tracker.count > 0:
        incident_text = f"\nPREVIOUS INCIDENTS:\n{incident_tracker.render_for_prompt()}\n"

    # Loop warning
    loop_text = ""
    if len(last_coords) >= LOOP_THRESHOLD:
        unique = len(set(last_coords))
        if unique == 1:
            loop_text = "\n⚠️  LOOP DETECTED: same coordinate tapped 3+ times. Try a DIFFERENT approach.\n"

    user_prompt = (
        f"TASK: {instruction}\n"
        f"Step: {step}\n"
        f"{loop_text}"
        f"{incident_text}"
        f"{index_text}"
    )

    raw = _call_vlm(SYSTEM_PROMPT_FALLBACK, user_prompt, screenshot_path)
    if not raw:
        _log("VLM returned no response — assuming task is stuck, returning finish")
        return ("finish", {}, "VLM call failed — stopping")

    # Extract JSON from response.
    # Models like MiniMax-M3 may wrap output in &lt;thinking&gt; blocks.
    # Others use ```json fences.  We strip all wrappers then find JSON.
    json_str = raw.strip()

    # 1) Strip XML/HTML tag blocks (e.g. &lt;thinking&gt;...&lt;/thinking&gt;)
    json_str = re.sub(r'<[^>]+>\s*', '', json_str)
    json_str = re.sub(r'\s*</[^>]+>', '', json_str)
    json_str = re.sub(r'<[^>]+/>', '', json_str)

    # 2) Strip markdown code fences
    for fence in ["```json", "```"]:
        if fence in json_str:
            idx = json_str.index(fence) + len(fence)
            json_str = json_str[idx:]
            end = json_str.rfind("```")
            if end > 0:
                json_str = json_str[:end]
            break

    # 3) Try to find a JSON object by locating outermost { ... }
    brace_start = json_str.find("{")
    brace_end = json_str.rfind("}")
    if brace_start >= 0 and brace_end > brace_start:
        json_str = json_str[brace_start:brace_end + 1]

    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError:
        _debug(f"VLM returned non-JSON: {raw[:300]}")
        return ("finish", {}, f"VLM parse error: {raw[:80]}")

    action = parsed.get("action", "finish")

    # Normalize action_type and args
    if action == "tap":
        coord = parsed.get("coordinate", [0, 0])
        x, y = int(coord[0]), int(coord[1])
        desc = f"tap({x},{y})"
        return ("tap", {"coordinate": [x, y]}, desc)

    elif action == "long_press":
        coord = parsed.get("coordinate", [0, 0])
        x, y = int(coord[0]), int(coord[1])
        desc = f"long_press({x},{y})"
        return ("long_press", {"coordinate": [x, y]}, desc)

    elif action == "swipe":
        c1 = parsed.get("coordinate1", [0, 0])
        c2 = parsed.get("coordinate2", [0, 0])
        dur = parsed.get("duration", 300)
        desc = f"swipe({c1}→{c2})"
        return ("swipe", {"coordinate1": c1, "coordinate2": c2, "duration": dur}, desc)

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


def _execute_action_adb(
    adb_path: str,
    device: str,
    action_type: str,
    action_args: dict,
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
        # Use ADB Keyboard for reliable input
        _adb(["shell", "am", "broadcast", "-a", "ADB_INPUT_TEXT", "--es", "msg", text],
             adb_path=adb_path, device=device)
    elif action_type == "system_button":
        btn = action_args.get("button", "").upper()
        key_map = {"HOME": "3", "BACK": "4", "ENTER": "66", "APP_SWITCH": "187"}
        code = key_map.get(btn, "3")
        _adb(["shell", "input", "keyevent", code],
             adb_path=adb_path, device=device)
    elif action_type == "open":
        app = action_args.get("text", "")
        # Use am start with resolved main activity (more reliable than monkey)
        rc, out, _ = _adb(
            ["shell", "cmd", "package", "resolve-activity", "--brief", app, "|", "tail", "-n", "1"],
            adb_path=adb_path, device=device, timeout=5
        )
        # Fall back to monkey if resolve-activity fails
        _adb(["shell", "monkey", "-p", app, "1"],
             adb_path=adb_path, device=device)
    elif action_type == "finish":
        pass  # No ADB action needed

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

    # 4. Check Artemis availability
    if check_artemis_available():
        _log("Artemis SDK is available — using full engine")
        _trace(f"[config] artemis SDK importable, profile={args.profile}")
    else:
        _log("Artemis SDK not found — using fallback reactive loop with safety net")
        _log("Tip: cd /workspace/artemis && uv sync to enable full Artemis engine")
        _trace("[config] artemis SDK NOT importable — fallback mode")

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