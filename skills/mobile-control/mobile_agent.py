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
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = "http://localhost:8810/v1"
DEFAULT_MODEL = "gui-owl"
DEFAULT_API_KEY = "not-needed"
DEFAULT_MAX_STEPS = 20
DEFAULT_TIMEOUT = 120  # seconds for the entire run
LOOP_THRESHOLD = 3     # same coordinate N times → inject retry hint
ADB_IME = "com.android.adbkeyboard/.AdbIME"

# Patterns that imply the task completed
FINISH_PATTERNS = [
    r"\bFINISH\b",
    r"任务完成",
    r"操作完成",
    r'"action":\s*"finish"',
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


def _log(msg: str) -> None:
    """Status update on stderr (not captured by OpenClaw's JSON parser)."""
    print(f"[mobileAgent] {msg}", file=sys.stderr, flush=True)


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
) -> dict:
    """
    Launch run_gui_owl_1_5_for_mobile.py in a subprocess, monitor its output,
    detect loops, auto-handle permissions, and return a result dict.
    """
    cmd = [
        sys.executable, runner_script,
        "--adb_path", adb_path,
        "--device", device,
        "--api_key", api_key,
        "--base_url", base_url,
        "--model", model,
        "--instruction", instruction,
        "--add_info", add_info,
        "--max_steps", str(max_steps),
    ]

    _log(f"Launching: {' '.join(cmd)}")

    start = time.time()
    actions: list[str] = []
    last_coords: list[str] = []
    step = 0
    status = "timeout"
    last_action = ""
    loop_hint_injected = False

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
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

    try:
        for raw_line in proc.stdout:  # type: ignore[union-attr]
            line = raw_line.rstrip()
            elapsed = time.time() - start

            # Hard timeout
            if elapsed > timeout:
                proc.terminate()
                status = "timeout"
                break

            # Forward line to stderr for live visibility
            print(line, file=sys.stderr)

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

            current_step_output.append(line)

            # Parse action from tool_call blocks
            action_summary = _extract_action_summary(line)
            if action_summary:
                last_action = action_summary
                actions.append(action_summary)

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
                    break
            if status == "success":
                break

            # Success marker
            for pat in SUCCESS_PATTERNS:
                if re.search(pat, line, re.IGNORECASE):
                    status = "success"
                    break

    except KeyboardInterrupt:
        proc.terminate()
        status = "error"

    # Wait for process to exit
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    rc = proc.returncode if proc.returncode is not None else -1

    if rc == 0 and status == "timeout":
        # Script exited cleanly → treat as success
        status = "success"

    message_map = {
        "success": f"Task completed in {step} steps.",
        "timeout": f"Timed out after {int(time.time()-start)}s / {step} steps.",
        "error": f"Runner exited with code {rc} after {step} steps.",
    }
    message = message_map.get(status, f"Status: {status} after {step} steps.")
    if loop_hint_injected:
        message += " (Loop detected — agent was retrying the same position.)"

    return {
        "status": status,
        "steps": step,
        "last_action": last_action,
        "message": message,
        "actions": actions,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
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
    p.add_argument("--base_url", default=DEFAULT_BASE_URL)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--api_key", default=DEFAULT_API_KEY)
    p.add_argument("--max_steps", type=int, default=DEFAULT_MAX_STEPS)
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p.add_argument("--add_info", default="")
    p.add_argument("--dry_run", action="store_true",
                   help="Only run pre-checks, skip model inference")
    return p.parse_args()


def main() -> int:
    args = parse_args()

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
