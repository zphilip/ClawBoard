#!/usr/bin/env python3
"""
mobile_agent.py — OpenClaw skill wrapper for mobileAgent

Wraps run_gui_owl_1_5_for_mobile.py with:
  - ADB pre-checks (device connectivity, screen state)
  - ADB Keyboard setup
  - Toast notification to the device
  - Subprocess monitoring with timeout & loop detection
  - Permission-dialog auto-handler
  - Structured JSON output for OpenClaw to consume

Usage:
    python3 mobile_agent.py --instruction "打开百度地图,导航回家"
    python3 mobile_agent.py --instruction "Open WeChat" --device emulator-5554
    python3 mobile_agent.py --instruction "dummy" --dry_run

Exit codes:
    0  Task completed (FINISH detected in model output)
    1  Runtime error (ADB lost, model crash, unexpected exception)
    2  Clarification needed (instruction too vague)
    3  Timeout or max-steps reached
    4  ADB device not found
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Global log file handle (set in main)
# ---------------------------------------------------------------------------
_LOG_FILE = None
_DEBUG = False

# Persistent trace log — always written, not gated by --debug.
# Captures provider selection and key decision points for offline diagnosis.
_TRACE_LOG = Path(__file__).parent / "skill_trace.log"


def _trace(msg: str) -> None:
    """Append a timestamped line to skill_trace.log in the skill directory."""
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        with _TRACE_LOG.open("a", encoding="utf-8") as _f:
            _f.write(line + "\n")
    except Exception:
        pass  # never crash due to logging

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = "http://apicn.aiworm.cn:8809/v1"
DEFAULT_MODEL = "gui-owl"
DEFAULT_API_KEY = "not-needed"
DEFAULT_MAX_CONTEXT_SIZE = 2048  # gui-owl fallback model has a 2048-token context

# ---------------------------------------------------------------------------
# Skill config loader
# ---------------------------------------------------------------------------

_CONFIG_FILE = Path(__file__).parent / "config.json"


def load_skill_config() -> dict:
    """
    Load provider settings from config.json next to this script.
    Returns a dict with keys: base_url, model, api_key, max_context_size.

    Resolution order:
      1. If 'provider' exists and has a non-empty api_key → use provider.
      2. Otherwise → use 'fallback_provider' (defaults to DEFAULT_* constants).

    CLI arguments always take precedence over these values.
    """
    fallback = {
        "base_url": DEFAULT_BASE_URL,
        "model": DEFAULT_MODEL,
        "api_key": DEFAULT_API_KEY,
        "max_context_size": DEFAULT_MAX_CONTEXT_SIZE,
    }
    if not _CONFIG_FILE.exists():
        _trace(f"[config] config.json not found at {_CONFIG_FILE} — using hard-coded fallback")
        return fallback
    try:
        with _CONFIG_FILE.open(encoding="utf-8") as f:
            data = json.load(f)

        # Load fallback_provider if present (overrides hard-coded defaults)
        fp = data.get("fallback_provider", {})
        if fp.get("base_url"):
            fallback["base_url"] = fp["base_url"]
        if fp.get("model"):
            fallback["model"] = fp["model"]
        if "api_key" in fp:
            fallback["api_key"] = fp["api_key"]
        if fp.get("max_context_size"):
            fallback["max_context_size"] = int(fp["max_context_size"])
        _trace(f"[config] fallback_provider loaded: base_url={fallback['base_url']} model={fallback['model']} max_context_size={fallback['max_context_size']}")

        # Use primary provider only if it has a non-empty api_key
        provider = data.get("provider", {})
        _trace(
            f"[config] primary provider: base_url={provider.get('base_url','<none>')} "
            f"model={provider.get('model','<none>')} "
            f"api_key={'<set>' if provider.get('api_key') else '<EMPTY — will use fallback>'}"
        )
        if provider and provider.get("api_key"):
            cfg = dict(fallback)
            if provider.get("base_url"):
                cfg["base_url"] = provider["base_url"]
            if provider.get("model"):
                cfg["model"] = provider["model"]
            cfg["api_key"] = provider["api_key"]
            # Primary provider is typically a large-context model—don't cap it.
            cfg["max_context_size"] = provider.get("max_context_size") or None
            _trace(f"[config] SELECTED primary provider: base_url={cfg['base_url']} model={cfg['model']} max_context_size={cfg['max_context_size']}")
            return cfg

        # No valid primary provider — use fallback
        _trace(f"[config] SELECTED fallback provider: base_url={fallback['base_url']} model={fallback['model']} (reason: primary api_key is empty)")
        return fallback
    except Exception as e:
        _trace(f"[config] ERROR loading config.json: {e} — using hard-coded fallback")
        print(f"[mobile-control] WARNING: failed to load config.json: {e}", file=sys.stderr)
    return fallback


def load_supervisor_config() -> dict:
    """
    Load supervisor_provider settings from config.json.
    Returns a dict with keys base_url, model, api_key, or an empty dict
    when the section is absent or has no model set (supervisor disabled).
    """
    if not _CONFIG_FILE.exists():
        return {}
    try:
        with _CONFIG_FILE.open(encoding="utf-8") as f:
            data = json.load(f)
        sp = data.get("supervisor_provider", {})
        if sp.get("model"):
            cfg = {
                "model": sp["model"],
                "base_url": sp.get("base_url", ""),
                "api_key": sp.get("api_key", ""),
                "vision": bool(sp.get("vision", False)),
                "reasoning_split": bool(sp.get("reasoning_split", False)),
            }
            _trace(f"[config] supervisor_provider loaded: base_url={cfg['base_url']} model={cfg['model']} vision={cfg['vision']} reasoning_split={cfg['reasoning_split']}")
            return cfg
    except Exception as e:
        _trace(f"[config] ERROR loading supervisor_provider: {e}")
    return {}


DEFAULT_MAX_STEPS = 20
DEFAULT_TIMEOUT = 600  # seconds for the entire run (10 minutes)
LOOP_THRESHOLD = 3     # same coordinate N times → inject retry hint
ADB_IME = "com.android.adbkeyboard/.AdbIME"

# Patterns that imply the task completed
FINISH_PATTERNS = [
    r"\bFINISH\b",
    r"任务完成",
    # NOTE: r"操作完成" was removed — it means "operation complete" (a single-step
    # description) and caused the runner to be killed prematurely after phrases like
    # "QQ Music opened, 操作完成" even though the full task was not yet done.
    r'"action":\s*"finish"',
    # NOTE: r'"action":\s*"answer"' removed — the runner now intercepts the
    # 'answer' action internally and calls supervisor.is_task_complete() before
    # accepting completion. Killing the runner here would bypass that check.
]

# Patterns that indicate a step-level success note
SUCCESS_PATTERNS = [
    r"Action:\s*SUCCESS",
    r"Action:\s*成功",
]

# Vague instruction patterns → request clarification
VAGUE_PATTERNS = [
    r"^(打开它|打开那个|开一下|open it|go there|帮我操作|操作一下)$",
    r"^(做一下|处理|搞一下|help me|do it|fix it)$",
]

# Common permission-dialog button labels (regex match against uiautomator XML)
PERMISSION_ALLOW_LABELS = [
    "Allow", "允许", "ALLOW", "Grant", "授权",
    "OK", "确定", "Continue", "继续",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    """Run a command, return (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError as e:
        return -1, "", str(e)


def _adb(args: list[str], adb_path: str = "adb", device: Optional[str] = None,
         timeout: int = 10) -> tuple[int, str, str]:
    """Run an adb command, optionally targeting a specific device."""
    cmd = [adb_path]
    if device:
        cmd += ["-s", device]
    cmd += args
    return _run(cmd, timeout=timeout)


def _emit(obj: dict) -> None:
    """Print a JSON object to stdout — consumed by OpenClaw."""
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _emit_progress(step: int, action: str, message: str, screenshot: Optional[str] = None) -> None:
    """Emit a JSONL progress line so the agent can narrate live updates to the user."""
    obj: dict = {"type": "progress", "step": step, "action": action, "message": message}
    if screenshot:
        obj["screenshot"] = screenshot
    _emit(obj)


def _log(msg: str) -> None:
    """Status update on stderr (not captured by OpenClaw's JSON parser) and optional log file."""
    _now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{_now}] [mobile-control] {msg}"
    print(line, file=sys.stderr, flush=True)
    if _LOG_FILE:
        try:
            _LOG_FILE.write(line + "\n")
            _LOG_FILE.flush()
        except Exception:
            pass


def _debug(msg: str) -> None:
    """Only emitted when --debug is set."""
    if _DEBUG:
        _log(f"[DEBUG] {msg}")


# ---------------------------------------------------------------------------
# Pre-checks
# ---------------------------------------------------------------------------

def check_adb_device(adb_path: str, device: Optional[str]) -> Optional[str]:
    """
    Return the device serial to use, or None if no device found.
    Picks the first online device when --device is not specified.
    """
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
    """Wake the screen if it is off; attempt swipe-unlock."""
    rc, out, _ = _adb(
        ["shell", "dumpsys", "window", "policy"], adb_path=adb_path, device=device
    )
    if "isInteractive=false" in out or "mInteractive=false" in out:
        _log("Screen off — waking...")
        _adb(["shell", "input", "keyevent", "26"], adb_path=adb_path, device=device)
        time.sleep(1)
        # Swipe up to unlock (works for most stock launchers)
        _adb(
            ["shell", "input", "swipe", "540", "1600", "540", "800", "300"],
            adb_path=adb_path, device=device,
        )
        time.sleep(1)
    else:
        _log("Screen is on.")


def setup_adb_keyboard(adb_path: str, device: str) -> None:
    """Force-set ADB Keyboard as the active IME (required for reliable text input)."""
    rc, out, _ = _adb(["shell", "ime", "list", "-a"], adb_path=adb_path, device=device)
    if "adbkeyboard" in out.lower():
        _adb(["shell", "ime", "set", ADB_IME], adb_path=adb_path, device=device)
        _log("ADB Keyboard activated.")
    else:
        _log(
            "WARNING: ADB Keyboard not installed. "
            "Text input may fail. "
            "Install from https://github.com/senzhk/ADBKeyBoard"
        )


def send_toast(adb_path: str, device: str, message: str) -> None:
    """Show a Toast notification on the device screen."""
    escaped = message.replace("'", "\\'")
    script = (
        f"am broadcast -a ADB_INPUT_TEXT --es msg '{escaped}' "
        "com.android.adbkeyboard 2>/dev/null; "
        f"echo 'AI正在接管操作... {escaped}' | "
        "am broadcast -a ADB_INPUT_TEXT --es msg 2>/dev/null; true"
    )
    # Simpler fallback: use a toast via cmd notification (Android 11+)
    _adb(
        ["shell", "cmd", "notification", "post", "-S", "bigtext", "-t",
         "OpenClaw AI", "MobileAgent", message],
        adb_path=adb_path, device=device,
    )
    _log(f"Toast sent: {message}")


def handle_permission_dialog(adb_path: str, device: str) -> bool:
    """
    Check for common permission dialogs and auto-tap 'Allow'.
    Returns True if a dialog was handled.
    """
    rc, _, _ = _adb(
        ["shell", "uiautomator", "dump", "/sdcard/_oc_ui.xml"],
        adb_path=adb_path, device=device, timeout=8,
    )
    if rc != 0:
        return False

    rc2, out, _ = _adb(
        ["shell", "cat", "/sdcard/_oc_ui.xml"],
        adb_path=adb_path, device=device, timeout=5,
    )
    if rc2 != 0 or not out:
        return False

    for label in PERMISSION_ALLOW_LABELS:
        pattern = re.compile(
            rf'text="{re.escape(label)}"[^/]*/>'
            r'|'
            rf'content-desc="{re.escape(label)}"',
            re.IGNORECASE,
        )
        m = re.search(
            rf'<node[^>]+text="{re.escape(label)}"[^>]+bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            out, re.IGNORECASE,
        )
        if m:
            x1, y1, x2, y2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            _adb(
                ["shell", "input", "tap", str(cx), str(cy)],
                adb_path=adb_path, device=device,
            )
            _log(f"Auto-tapped permission dialog: '{label}' at ({cx},{cy})")
            return True
    return False


# ---------------------------------------------------------------------------
# Instruction validation
# ---------------------------------------------------------------------------

def needs_clarification(instruction: str) -> bool:
    """Return True if the instruction is too vague to act on."""
    s = instruction.strip()
    if len(s) < 4:
        return True
    for pattern in VAGUE_PATTERNS:
        if re.fullmatch(pattern, s, re.IGNORECASE):
            return True
    return False


# ---------------------------------------------------------------------------
# Subprocess monitoring
# ---------------------------------------------------------------------------

def _find_runner_script() -> Optional[str]:
    """
    Locate run_gui_owl_1_5_for_mobile.py relative to this skill or on PATH.
    Search order:
      1. Same directory as this file (ClawBoard/skills/mobile-control/)
      2. ~/MobileAgent/Mobile-Agent-v3.5/mobile_use/
    """
    candidates = [
        Path(__file__).parent / "run_gui_owl_1_5_for_mobile.py",
        Path.home() / "MobileAgent" / "Mobile-Agent-v3.5" / "mobile_use" / "run_gui_owl_1_5_for_mobile.py",
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    return None


SCREENSHOTS_DIR = Path(__file__).parent / "screenshots"


def _take_screenshot(adb_path: str, device: Optional[str], dest: Path,
                     max_age_seconds: int = 30) -> bool:
    """
    Capture a screenshot from the device via ADB and save to dest.
    Removes any existing file first to prevent stale data being returned.
    Validates the captured file's mtime is within max_age_seconds.
    Returns True on success.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Remove stale file so existence check cannot give a false positive
    if dest.exists():
        dest.unlink()
    # Must run in binary mode — PNG output is not valid UTF-8
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
                _log(f"Screenshot {dest.name} is {age:.1f}s old (max {max_age_seconds}s); discarding")
                dest.unlink(missing_ok=True)
                return False
            return True
    except Exception as e:
        _log(f"Screenshot failed: {e}")
    return False


def _extract_action_summary(line: str) -> Optional[str]:
    """
    Parse a [MODEL OUTPUT] action line into a short human-readable summary.
    e.g. '{"name":"mobile_use","arguments":{"action":"click","coordinate":[160,376]}}'
      → 'click [160, 376]'
    """
    if '"action"' not in line:
        return None
    try:
        # Strip leading/trailing noise
        m = re.search(r'\{.*\}', line)
        if not m:
            return None
        data = json.loads(m.group(0))
        args = data.get("arguments", {})
        action = args.get("action", "?")
        if "coordinate" in args:
            return f"{action} {args['coordinate']}"
        if "text" in args:
            return f"{action} \"{args['text']}\""
        return action
    except (json.JSONDecodeError, KeyError):
        return None


def run_agent(
    instruction: str,
    adb_path: str,
    device: str,
    base_url: str,
    model: str,
    api_key: str,
    max_steps: int,
    timeout: int,
    add_info: str,
    runner_script: str,
    supervisor_model: str = "",
    supervisor_api_key: str = "",
    supervisor_base_url: str = "",
    max_context_size: Optional[int] = None,
) -> dict:
    """
    Launch run_gui_owl_1_5_for_mobile.py in a subprocess, monitor its output,
    detect loops, auto-handle permissions, and return a result dict.
    """
    cmd = [
        sys.executable, "-u", runner_script,
        "--adb_path", adb_path,
        "--device", device,
        "--api_key", api_key,
        "--base_url", base_url,
        "--model", model,
        "--instruction", instruction,
        "--add_info", add_info,
        "--max_steps", str(max_steps),
    ]
    if supervisor_model:
        cmd += [
            "--supervisor_model", supervisor_model,
            "--supervisor_api_key", supervisor_api_key or api_key,
            "--supervisor_base_url", supervisor_base_url or base_url,
        ]
    if max_context_size is not None:
        cmd += ["--max-context-size", str(max_context_size)]

    _log(f"Launching: {' '.join(cmd)}")

    # Clean up any leftover screenshots/task-dirs from previous runs before
    # starting fresh.  This runs at the START so stale data cannot be reused
    # even when the previous run was killed before its own end-of-task cleanup.
    screenshots_dir = SCREENSHOTS_DIR
    if screenshots_dir.exists():
        shutil.rmtree(screenshots_dir, ignore_errors=True)
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    start = time.time()
    actions: list[str] = []
    last_coords: list[str] = []
    step = 0
    status = "timeout"
    last_action = ""
    loop_hint_injected = False
    end_reason = "runner_started"
    finish_pattern_hit = ""
    last_runner_line = ""
    runner_termination_reason = ""

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            # Pin CWD to the skill directory so any relative-path artefacts
            # from the runner land here, not in picoclaw's workspace root.
            cwd=str(Path(__file__).parent.resolve()),
        )
    except FileNotFoundError as e:
        return {
            "status": "error",
            "steps": 0,
            "last_action": "",
            "message": f"Failed to start runner: {e}",
            "actions": [],
        }

    current_step_output: list[str] = []
    current_fg_app: str = ""

    try:
        for raw_line in proc.stdout:  # type: ignore[union-attr]
            line = raw_line.rstrip()
            elapsed = time.time() - start
            if line:
                last_runner_line = line

            # Runner-provided end reason (if available).
            if line.startswith("[TERMINATION REASON]"):
                runner_termination_reason = line.split("]", 1)[-1].strip()

            # Explicit task completion signal from runner.
            if line.strip() == "[TERMINATED] Task completed.":
                status = "success"
                end_reason = "runner_terminated_completed"
                break

            # Hard timeout
            if elapsed > timeout:
                proc.terminate()
                status = "timeout"
                end_reason = f"hard_timeout_elapsed={int(elapsed)}s"
                break

            # Forward line to stderr for live visibility
            print(line, file=sys.stderr)
            if _LOG_FILE:
                try:
                    _LOG_FILE.write(line + "\n")
                    _LOG_FILE.flush()
                except Exception:
                    pass

            # Step counter
            if re.match(r"^={10,}$", line):
                if current_step_output:
                    # Check for permission dialogs between steps
                    handle_permission_dialog(adb_path, device)
                current_step_output = []
            if re.match(r"^STEP\s+\d+", line):
                m = re.search(r"\d+", line)
                step = int(m.group(0)) if m else step
                current_step_output = []
                current_fg_app = ""  # reset per step; will be filled by [Foreground] line

            current_step_output.append(line)

            # Track foreground app reported by the runner (from ADB).
            _fg_match = re.match(r"\[Foreground\]\s+(.+)", line)
            if _fg_match:
                current_fg_app = _fg_match.group(1).strip()

            # Parse action from tool_call blocks
            action_summary = _extract_action_summary(line)
            if action_summary:
                last_action = action_summary
                actions.append(action_summary)

                # Capture verification screenshot after the action settles
                time.sleep(1.2)
                _ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:19]
                action_slug = re.sub(r'[^\w]+', '_', action_summary)[:40]
                shot_path = screenshots_dir / f"step_{step:03d}_{_ts}_{action_slug}.png"
                shot_ok = _take_screenshot(adb_path, device, shot_path)
                shot_rel = str(shot_path) if shot_ok else None

                # Emit live progress so the agent can narrate to the user
                _msg = f"Step {step}: {action_summary}"
                if current_fg_app:
                    _msg += f"  [Foreground: {current_fg_app}]"
                _emit_progress(
                    step, action_summary,
                    _msg,
                    screenshot=shot_rel,
                )
                _debug(f"action parsed: {action_summary}")

                # Loop detection
                coord_m = re.search(r"\[(\d+),\s*(\d+)\]", action_summary)
                if coord_m:
                    coord_str = f"{coord_m.group(1)},{coord_m.group(2)}"
                    last_coords.append(coord_str)
                    if len(last_coords) > LOOP_THRESHOLD:
                        last_coords = last_coords[-LOOP_THRESHOLD:]
                    if len(last_coords) == LOOP_THRESHOLD and len(set(last_coords)) == 1:
                        _log(
                            f"Loop detected: same coordinate {coord_str} "
                            f"tapped {LOOP_THRESHOLD} times. "
                            "Injecting retry hint..."
                        )
                        # We can't easily inject into an already-running process,
                        # but we log the warning; the retry hint will be in add_info
                        # on the next invocation if the user retries.
                        loop_hint_injected = True
                else:
                    last_coords = []

            # Finish detection
            for pat in FINISH_PATTERNS:
                if re.search(pat, line, re.IGNORECASE):
                    proc.terminate()
                    status = "success"
                    finish_pattern_hit = pat
                    end_reason = f"finish_pattern_matched:{pat}"
                    _ts_fin = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:19]
                    shot_path = screenshots_dir / f"step_{step:03d}_{_ts_fin}_finish.png"
                    shot_ok = _take_screenshot(adb_path, device, shot_path)
                    _emit_progress(
                        step, "finish",
                        f"Step {step}: Task finished ✓",
                        screenshot=str(shot_path) if shot_ok else None,
                    )
                    break
            if status == "success":
                break

            # Success marker
            for pat in SUCCESS_PATTERNS:
                if re.search(pat, line, re.IGNORECASE):
                    status = "success"
                    end_reason = f"success_pattern_matched:{pat}"
                    break

    except KeyboardInterrupt:
        proc.terminate()
        status = "error"
        end_reason = "keyboard_interrupt"

    # Wait for process to exit
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    rc = proc.returncode if proc.returncode is not None else -1

    # IMPORTANT: clean runner exit without explicit completion marker is NOT success.
    # This avoids false positives like "opened app at step 1" then silent exit.
    if status == "timeout" and rc != 0 and not end_reason.startswith("hard_timeout"):
        status = "error"
        end_reason = f"runner_nonzero_exit rc={rc}"
    elif status == "timeout" and rc == 0:
        if runner_termination_reason:
            end_reason = f"runner_exit_without_completion rc=0; {runner_termination_reason}"
        else:
            end_reason = f"runner_exit_without_completion rc=0; last_line={last_runner_line[:120]!r}"

    # Clean up all screenshots after the task ends (success or failure).
    # Use rmtree to also remove subdirectories created by the runner
    # (task_dir / anno_dir live inside screenshots/ now).
    try:
        if screenshots_dir.exists():
            shutil.rmtree(screenshots_dir, ignore_errors=True)
        _log("Screenshot directory cleaned up.")
    except Exception as _e:
        _log(f"Screenshot cleanup warning: {_e}")

    message_map = {
        "success": f"Task completed in {step} steps.",
        "timeout": f"Timed out after {int(time.time()-start)}s / {step} steps.",
        "error": f"Runner exited with code {rc} after {step} steps.",
    }
    message = message_map.get(status, f"Status: {status} after {step} steps.")
    if loop_hint_injected:
        message += " (Loop detected — agent was retrying the same position.)"
    message += f" [reason: {end_reason}]"

    _log(
        "END DEBUG | "
        f"status={status} rc={rc} step={step} reason={end_reason} "
        f"finish_pattern={finish_pattern_hit or 'none'} "
        f"runner_reason={runner_termination_reason or 'none'} "
        f"last_action={last_action or 'none'} "
        f"last_line={last_runner_line[:200] if last_runner_line else 'none'}"
    )

    return {
        "type": "result",
        "status": status,
        "steps": step,
        "last_action": last_action,
        "message": message,
        "actions": actions,
        "debug": {
            "end_reason": end_reason,
            "rc": rc,
            "finish_pattern": finish_pattern_hit,
            "runner_termination_reason": runner_termination_reason,
            "last_runner_line": last_runner_line,
        },
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    skill_cfg = load_skill_config()
    p = argparse.ArgumentParser(
        description="OpenClaw mobileAgent skill — wraps GUI-Owl mobile control",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Exit codes:
              0  success (FINISH detected)
              1  runtime error
              2  clarification needed
              3  timeout / step limit
              4  ADB device not found
        """),
    )
    p.add_argument("--instruction", required=True, help="Natural language task")
    p.add_argument("--adb_path", default="adb", help="Path to ADB binary")
    p.add_argument("--device", default=None, help="ADB device serial")
    p.add_argument("--base_url", default=skill_cfg["base_url"],
                   help="Override base URL from config.json")
    p.add_argument("--model", default=skill_cfg["model"],
                   help="Override model from config.json")
    p.add_argument("--api_key", default=skill_cfg["api_key"],
                   help="Override API key from config.json")
    p.add_argument("--max_steps", type=int, default=DEFAULT_MAX_STEPS)
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p.add_argument("--add_info", default="")
    p.add_argument("--supervisor_model", default="",
                   help="Text LLM that validates each step before execution. "
                        "E.g. 'MiniMax-Text-01'. Leave empty to disable.")
    p.add_argument("--supervisor_api_key", default="",
                   help="API key for supervisor LLM (defaults to --api_key).")
    p.add_argument("--supervisor_base_url", default="",
                   help="Base URL for supervisor LLM (defaults to --base_url).")
    p.add_argument("--max-context-size", type=int,
                   default=skill_cfg.get("max_context_size"),
                   dest="max_context_size",
                   help="VLM context window size (tokens). Activates compact mode when ≤2048. "
                        "Defaults to config.json value or 2048 for the built-in fallback model.")
    p.add_argument("--dry_run", action="store_true",
                   help="Only run pre-checks, skip model inference")
    p.add_argument("--debug", action="store_true",
                   help="Print full model input/output to log")
    p.add_argument("--log-file", default="", dest="log_file",
                   help="Also write all [mobile-control] logs to this file")
    return p.parse_args()


def main() -> int:
    global _LOG_FILE, _DEBUG
    args = parse_args()

    _DEBUG = args.debug
    if args.log_file:
        try:
            _LOG_FILE = open(args.log_file, "a", encoding="utf-8")
            _log(f"=== mobile-control session start ===  instruction={args.instruction!r}")
        except OSError as e:
            print(f"[mobile-control] WARNING: cannot open log file {args.log_file}: {e}",
                  file=sys.stderr)

    # 1. Clarification check
    if needs_clarification(args.instruction):
        _emit({
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
    device = check_adb_device(args.adb_path, args.device)
    if device is None:
        _emit({
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

    # 3. Screen + keyboard setup
    ensure_screen_on(args.adb_path, device)
    setup_adb_keyboard(args.adb_path, device)

    # 4. Toast notification
    send_toast(args.adb_path, device, f"AI正在接管操作: {args.instruction[:30]}")

    if args.dry_run:
        _emit({
            "status": "success",
            "steps": 0,
            "last_action": "",
            "message": f"Dry run OK — device {device} ready.",
            "actions": [],
        })
        return 0

    # 5. Find runner script
    runner = _find_runner_script()
    if runner is None:
        _emit({
            "status": "error",
            "steps": 0,
            "last_action": "",
            "message": (
                "run_gui_owl_1_5_for_mobile.py not found. "
                "Expected locations:\n"
                "  • ClawBoard/skills/mobile-control/run_gui_owl_1_5_for_mobile.py\n"
                "  • ~/MobileAgent/Mobile-Agent-v3.5/mobile_use/"
            ),
            "actions": [],
        })
        return 1

    _log(f"Runner: {runner}")

    # Supervisor config: CLI flags take precedence; fall back to config.json section
    sup_cfg = load_supervisor_config()
    sup_model = getattr(args, "supervisor_model", "") or sup_cfg.get("model", "")
    sup_api_key = getattr(args, "supervisor_api_key", "") or sup_cfg.get("api_key", "")
    sup_base_url = getattr(args, "supervisor_base_url", "") or sup_cfg.get("base_url", "")
    if sup_model:
        _log(f"Supervisor LLM: {sup_model} @ {sup_base_url or '(same as main)'}")

    # 6. Run the agent
    result = run_agent(
        instruction=args.instruction,
        adb_path=args.adb_path,
        device=device,
        base_url=args.base_url,
        model=args.model,
        api_key=args.api_key,
        max_steps=args.max_steps,
        timeout=args.timeout,
        add_info=args.add_info,
        runner_script=runner,
        supervisor_model=sup_model,
        supervisor_api_key=sup_api_key,
        supervisor_base_url=sup_base_url,
        max_context_size=args.max_context_size or None,
    )

    # 7. Emit result JSON (consumed by OpenClaw)
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
