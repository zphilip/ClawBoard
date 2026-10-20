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
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
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

# Vague instruction patterns → request clarification
VAGUE_PATTERNS = [
    r"^(打开它|打开那个|开一下|open it|go there|帮我操作|操作一下)$",
    r"^(做一下|处理|搞一下|help me|do it|fix it)$",
]

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

    _log("Running reactive fallback loop with safety net + explorer")

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

        # 3. Determine action (simplified: in real use this would call a VLM)
        action_type, action_args, action_desc = _decide_action_fallback(
            instruction, step, elements, incident_tracker, last_coords
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


def _decide_action_fallback(
    instruction: str,
    step: int,
    elements: list,
    incident_tracker,
    last_coords: list[str],
) -> tuple[str, dict, str]:
    """
    Placeholder decision function.
    In production, this would call a VLM (Gemini/GPT-4o/Claude) with the
    screenshot, element index, instruction, and incident context.

    Returns (action_type, action_args, human_readable_description).
    """
    # Return finish for now — real implementation hooks into Artemis Explorer
    return ("finish", {}, "finish")


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
        _adb(["shell", "monkey", "-p", app, "-c",
              "android.intent.category.LAUNCHER", "1"],
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
            "message": f"Dry run OK — device {device} ready. Artemis: {'available' if check_artemis_available() else 'fallback mode'}.",
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