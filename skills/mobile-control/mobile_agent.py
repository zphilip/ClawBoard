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


DEFAULT_MAX_STEPS = 30
DEFAULT_TIMEOUT = 900  # seconds for the entire run (15 minutes)
LOOP_THRESHOLD = 3     # same coordinate N times → inject retry hint
ADB_IME = "com.android.adbkeyboard/.AdbIME"

# Patterns that imply the task completed
FINISH_PATTERNS = [
    r"\bFINISH\b",
    # NOTE: r"任务完成" removed — it appears in VLM prose descriptions like
    # "通过百度地图成功导航到南岸花城...任务完成" before the task is actually
    # done (e.g. navigation not yet started). The runner now relies on the
    # supervisor's is_task_complete() check instead.
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


def run_post_run_report(input_path: str = "", top: int = 10) -> None:
    """Run optional offline post-run memory report and log its output."""
    script = Path(__file__).parent / "memory" / "post_run_report.py"
    if not script.exists():
        _log(f"Post-run report script not found: {script}")
        return

    cmd = [sys.executable, str(script), "--top", str(top)]
    if input_path:
        cmd += ["--input", input_path]

    _log(f"Running post-run report: {' '.join(cmd)}")
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(Path(__file__).parent.resolve()),
        )
        if r.stdout.strip():
            for line in r.stdout.splitlines():
                _log(f"[post-run-report] {line}")
        if r.returncode != 0:
            err = (r.stderr or "").strip()
            _log(f"Post-run report failed (rc={r.returncode}): {err[:400]}")
    except Exception as e:
        _log(f"Post-run report error: {e}")


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


def release_uiautomation_service(adb_path: str, device: str) -> None:
    """
    Release ADB's UiAutomationService to prevent conflicts with uiautomator2.
    
    When ADB runs `uiautomator dump`, it registers a UiAutomationService that
    can block uiautomator2 from connecting. This function attempts to release
    that service by killing related processes.
    
    This should be called at task start to ensure uiautomator2 can work properly.
    """
    _log("Releasing ADB UiAutomationService...")
    
    # Method 1: Kill all uiautomator-related processes
    for proc_pattern in ["uiautomator", "atx-agent", "com.wetest.uia2"]:
        rc, out, err = _adb(
            ["shell", "pkill", "-f", proc_pattern],
            adb_path=adb_path, device=device,
        )
        if rc == 0:
            _log(f"✅ Killed process matching '{proc_pattern}'")
    
    # Method 2: Kill by PID if pkill doesn't work
    rc, out, err = _adb(
        ["shell", "ps", "-A"],
        adb_path=adb_path, device=device,
    )
    if rc == 0 and out:
        for line in out.splitlines():
            if "uiautomator" in line.lower() or "atx-agent" in line.lower():
                # Extract PID (second column)
                parts = line.split()
                if len(parts) >= 2:
                    pid = parts[1]
                    _adb(
                        ["shell", "kill", "-9", pid],
                        adb_path=adb_path, device=device,
                    )
                    _log(f"✅ Killed uiautomator process PID={pid}")
    
    # Brief delay to ensure service is fully released
    time.sleep(0.8)
    _log("✅ UiAutomationService cleanup complete")


def is_device_uia2_initialized(device: str, adb_path: str = "adb") -> bool:
    """Check whether the uiautomator2 atx-agent is running on *device*.

    Uses three detection layers, tried in order of speed / reliability:

    1. **HTTP health check** — forward port 9008 and ping the atx-agent
       HTTP server.  Fast (< 1 s), works regardless of whether the
       ``uiautomator2`` Python library is installed in the current venv.

    2. **Python library** — import ``uiautomator2``, connect, and verify
       that ``d.info`` responds and the agent reports itself alive.

    3. **ADB fallback** — check that the uiautomator APKs are installed
       AND the atx-agent process is running via ``ps -A``.

    Returns True as soon as any layer confirms the server is alive.
    """
    import subprocess as _sp

    _device_flag = ["-s", device] if device else []

    # ------------------------------------------------------------------
    # Layer 1 — HTTP health check (no Python library dependency)
    # ------------------------------------------------------------------
    try:
        # Forward device port 9008 → localhost 9008
        _fwd_cmd = [adb_path] + _device_flag + ["forward", "tcp:9008", "tcp:9008"]
        _fwd_result = _sp.run(_fwd_cmd, capture_output=True, text=True, timeout=5)
        if _fwd_result.returncode == 0:
            import socket as _sock
            _s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
            _s.settimeout(2)
            try:
                _s.connect(("127.0.0.1", 9008))
                _s.sendall(b"GET /ping HTTP/1.0\r\n\r\n")
                _resp = _s.recv(256)
                _s.close()
                # atx-agent responds to any HTTP request — a response
                # means the server is alive.
                if _resp:
                    return True
            except (_sock.timeout, ConnectionRefusedError, OSError):
                pass
            finally:
                try:
                    _s.close()
                except OSError:
                    pass
            # Clean up the forward so it doesn't linger.
            _rm_cmd = [adb_path] + _device_flag + ["forward", "--remove", "tcp:9008"]
            _sp.run(_rm_cmd, capture_output=True, text=True, timeout=5)
    except Exception:
        pass

    # ------------------------------------------------------------------
    # Layer 2 — Python uiautomator2 library
    # ------------------------------------------------------------------
    try:
        import uiautomator2 as u2

        d = u2.connect(device) if device else u2.connect()

        # Quick connectivity test: get device info
        try:
            info = d.info
            if not info or 'serial' not in info:
                return False  # connected but response malformed
        except Exception:
            return False  # server not reachable via library

        # Check if agent reports itself as alive
        try:
            if hasattr(d, 'agent_alive') and d.agent_alive:
                return True
            elif hasattr(d, 'alive') and d.alive:
                return True
            # If neither attribute exists but info worked, server is OK
            return True
        except Exception:
            return True  # info worked → assume OK

    except ImportError:
        pass  # library not installed — fall through to ADB check
    except Exception:
        pass  # connection failed — fall through to ADB check

    # ------------------------------------------------------------------
    # Layer 3 — ADB: check APKs installed AND atx-agent process running
    # ------------------------------------------------------------------
    try:
        # 3a. Are the uiautomator APKs installed?
        _pm_cmd = [adb_path] + _device_flag + [
            "shell", "pm", "list", "packages", "com.github.uiautomator",
        ]
        _pm_result = _sp.run(_pm_cmd, capture_output=True, text=True, timeout=5)
        _pkgs_ok = (
            _pm_result.returncode == 0
            and "com.github.uiautomator" in _pm_result.stdout
        )
        if not _pkgs_ok:
            return False

        # 3b. Is the atx-agent process running?  Android's toolbox
        #     supports both 'ps -A' (modern) and 'ps' (legacy).
        for _ps_flag in ("-A", ""):
            _ps_cmd = [adb_path] + _device_flag + [
                "shell", "ps", _ps_flag,
            ] if _ps_flag else [adb_path] + _device_flag + ["shell", "ps"]
            _ps_result = _sp.run(_ps_cmd, capture_output=True, text=True, timeout=5)
            if _ps_result.returncode == 0 and "atx-agent" in _ps_result.stdout:
                return True
            if _ps_result.returncode == 0 and _ps_flag == "":
                # Legacy 'ps' also worked — check for uiautomator in dumpsys
                break

        # 3c. Last resort: check dumpsys for uiautomator instrumentation
        _dump_cmd = [adb_path] + _device_flag + [
            "shell", "dumpsys", "activity", "services",
        ]
        _dump_result = _sp.run(_dump_cmd, capture_output=True, text=True, timeout=5)
        if _dump_result.returncode == 0 and "uiautomator" in _dump_result.stdout:
            return True

        return False
    except Exception:
        return False


def initialize_uiautomator2(adb_path: str, device: str) -> None:
    """
    Initialize uiautomator2 on the device by running 'python -m uiautomator2 init'.

    This installs the atx-agent server on the device (~5MB) which is required for
    uiautomator2 to work. The init process also:
    - Installs app-uiautomator-test.apk and app-uiautomator.apk
    - Starts the atx-agent daemon on the device
    - Verifies the server is running

    This should be called once at task setup to ensure uiautomator2 is ready.
    Subsequent calls are fast if already initialized.

    Uses a file-based cache so a successful init is remembered across runs
    even when the uiautomator2 Python library is not importable in the
    current venv (the init command runs via subprocess and may use a
    different Python interpreter).
    """
    _cache_file = Path(__file__).parent / ".uia2_init_cache"

    # Layer 1: file-cache — if we successfully initted recently, skip
    # the expensive connectivity test and init.  Cache TTL is 24 hours.
    _cache_ttl = 24 * 3600
    try:
        if _cache_file.exists():
            _cache_age = time.time() - _cache_file.stat().st_mtime
            if _cache_age < _cache_ttl:
                _log(f"✅ uiautomator2 init cache hit (age={_cache_age:.0f}s) — skipping init")
                return
    except OSError:
        pass

    # Layer 2: connectivity test — check if the atx-agent is responding
    if is_device_uia2_initialized(device, adb_path=adb_path):
        _log("✅ uiautomator2 server is already running and responsive")
        _touch_uia2_cache(_cache_file)
        return

    _log("Initializing uiautomator2 on device...")

    try:
        import subprocess

        # Build the init command
        device_flag = f"-s {device}" if device else ""
        init_cmd = f"python3 -m uiautomator2 {device_flag} init"

        _log(f"Running: {init_cmd}")

        # Run the init command
        result = subprocess.run(
            init_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60  # Init can take up to 60s on first run
        )

        if result.returncode == 0:
            _log("✅ uiautomator2 initialized successfully")
            _touch_uia2_cache(_cache_file)
            # Log key info from output
            for line in result.stdout.splitlines():
                if any(keyword in line.lower() for keyword in [
                    "success", "installed", "version", "atx-agent"
                ]):
                    _log(f"   {line.strip()}")
        else:
            _log(f"⚠️ uiautomator2 init failed (rc={result.returncode})")
            if result.stderr:
                _log(f"   Error: {result.stderr[:200]}")
            _log("   Will attempt to use uiautomator2 anyway (may auto-init on connect)")

    except ImportError:
        _log("⚠️ uiautomator2 not installed (pip install uiautomator2)")
    except subprocess.TimeoutExpired:
        _log("⚠️ uiautomator2 init timed out (60s)")
    except Exception as e:
        _log(f"⚠️ uiautomator2 init exception: {e}")


def _touch_uia2_cache(cache_file: Path) -> None:
    """Create or update the uiautomator2 init cache file."""
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
    except OSError:
        pass  # non-essential — failure just means the check runs next time


def send_toast(adb_path: str, device: str, message: str) -> None:
    """Show a Toast notification on the device screen."""
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


SCREENSHOTS_ROOT = Path(__file__).parent / "screenshots"


def _make_run_screenshots_dir() -> Path:
    """Create a unique per-run subdirectory under the screenshots root.

    Uses timestamp + PID so concurrent runs never share a directory,
    eliminating the stale-race between rmtree and mkdir that existed
    with the old shared flat directory.
    """
    _ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    _pid = os.getpid()
    run_dir = SCREENSHOTS_ROOT / f"run_{_ts}_{_pid}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


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
    memory_decision: str = "off",
    memory_min_score: float = 0.7,
    memory_store: str = "",
    memory_replay_mode: str = "sequential",
    plan_store: str = "",
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
    cmd += ["--memory-decision", memory_decision]
    cmd += ["--memory-min-score", str(memory_min_score)]
    cmd += ["--memory-replay-mode", memory_replay_mode]
    if memory_store:
        cmd += ["--memory-store", memory_store]
    if plan_store:
        cmd += ["--plan-store", plan_store]

    # Redact API keys in log output for security
    cmd_for_logging = cmd.copy()
    for i, arg in enumerate(cmd_for_logging):
        if arg == "--api_key" and i + 1 < len(cmd_for_logging):
            cmd_for_logging[i + 1] = "<REDACTED>"
        elif arg == "--supervisor_api_key" and i + 1 < len(cmd_for_logging):
            cmd_for_logging[i + 1] = "<REDACTED>"
    
    _log(f"Launching: {' '.join(cmd_for_logging)}")

    # Each run gets its own subdirectory — zero chance of colliding with
    # another concurrent instance.  Old run directories are cleaned by
    # clean_mobile_control_data.sh rather than at launch time, so a
    # concurrent run's screenshots are never deleted mid-flight.
    screenshots_dir = _make_run_screenshots_dir()

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
                _log(f"Hard timeout reached at {int(elapsed)}s (limit={timeout}s); terminating runner")
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
    elif status == "timeout" and rc == 0 and not end_reason.startswith("hard_timeout"):
        if runner_termination_reason:
            end_reason = f"runner_exit_without_completion rc=0; {runner_termination_reason}"
        else:
            end_reason = f"runner_exit_without_completion rc=0; last_line={last_runner_line[:120]!r}"

    # Clean up only this run's screenshots — other concurrent runs are
    # unaffected.  The runner's own _cleanup() handles the task_dir /
    # anno_dir subdirectories inside screenshots/.
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
    p.add_argument("--memory-decision", choices=["off", "shadow", "enforce"], default="off",
                   help="Memory decision mode: off (default), shadow (observe only), enforce (allow overrides).")
    p.add_argument("--memory-min-score", type=float, default=0.7,
                   help="Minimum memory score required for a usable memory hit.")
    p.add_argument("--memory-store", default="",
                   help="Optional memory store path for state->action records.")
    p.add_argument("--memory-replay-mode", choices=["sequential", "single", "plan"], default="sequential",
                   help="Memory replay mode: sequential (default, advances through cached actions "
                        "when screen state changes, using run_id+step to track provenance), "
                        "single (only one cache replay per state_key per run, safest), or "
                        "plan (replay entire task-level plans without LLM calls).")
    p.add_argument("--plan-store", default="",
                   help="Path to task plan JSONL store (defaults to memory_data/plans.jsonl).")
    p.add_argument("--dry_run", action="store_true",
                   help="Only run pre-checks, skip model inference")
    p.add_argument("--debug", action="store_true",
                   help="Print full model input/output to log")
    p.add_argument("--log-file", default="", dest="log_file",
                   help="Also write all [mobile-control] logs to this file")
    p.add_argument("--post-run-report", action="store_true",
                   help="Run memory/post_run_report.py automatically after task completion.")
    p.add_argument("--post-run-report-top", type=int, default=10,
                   help="Top N entries for auto post-run report.")
    p.add_argument("--post-run-report-input", default="",
                   help="Optional input path for post-run report (events.db or events.jsonl).")
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
    release_uiautomation_service(args.adb_path, device)  # Release ADB's UiAutomationService first
    initialize_uiautomator2(args.adb_path, device)  # Initialize uiautomator2 (installs atx-agent)
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
        memory_decision=args.memory_decision,
        memory_min_score=args.memory_min_score,
        memory_store=args.memory_store,
        memory_replay_mode=args.memory_replay_mode,
        plan_store=args.plan_store,
    )

    # 7. Emit result JSON (consumed by OpenClaw)
    _emit(result)

    # 8. Cleanup: Release UiAutomationService for next run
    release_uiautomation_service(args.adb_path, device)

    # 9. Optional offline report (stderr/log only, does not affect result JSON)
    if args.post_run_report:
        run_post_run_report(
            input_path=args.post_run_report_input,
            top=args.post_run_report_top,
        )

    status_exit = {
        "success": 0,
        "timeout": 3,
        "clarify": 2,
        "error": 1,
    }
    return status_exit.get(result["status"], 1)


if __name__ == "__main__":
    sys.exit(main())
