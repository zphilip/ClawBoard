"""
Utility functions for Mobile-Agent-v3.5:
  - ADB device interaction
  - Screenshot annotation
  - Image resizing
  - Message construction for the VLM
  - App name resolution via LLM
"""

import json
import math
import os
import re
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Optional
import abc
import base64
import numpy as np
from io import BytesIO
from openai import OpenAI
from typing import Any, Optional

from PIL import Image, ImageDraw


# ---------------------------------------------------------------------------
# ADB Tools
# ---------------------------------------------------------------------------
# Setup instructions (for reference):
#
# 1. Download ADB (Android Debug Bridge) for your OS from:
#    https://developer.android.com/tools/releases/platform-tools
#
# 2. Enable "USB Debugging" (or "ADB Debugging") on your mobile device.
#    a. Developer Options is usually in System Settings.
#    b. If Developer Options is not visible, go to "About Phone" and
#       tap the build number 7 times.
#    c. On Xiaomi HyperOS, also enable "USB Debugging (Security Settings)".
#
# 3. Connect the device to your computer via USB; select "File Transfer" mode.
#
# 4. Install the ADB Keyboard APK on the device:
#    https://github.com/senzhk/ADBKeyBoard/blob/master/ADBKeyboard.apk
#
# 5. Set the default input method to "ADB Keyboard" in system settings.
#    Verify by tapping an input field — you should see "ADB Keyboard {No}"
#    at the bottom of the screen.
#
# 6. Test the connection from a terminal:
#      /path/to/adb devices
#    The device list should not be empty.
#    On Windows the binary is adb.exe; on macOS/Linux it is just adb.
#
# 7. On macOS / Linux, grant execute permission first:
#      sudo chmod +x /path/to/adb
#
# 8. Quick sanity check — open any app, then run:
#      /path/to/adb shell am start -a android.intent.action.MAIN \
#          -c android.intent.category.HOME
#    The device should return to the home screen.
#
# 9. Pass the adb_path when instantiating AdbTools. If multiple devices
#    are connected, obtain the device ID via `adb devices` and pass it
#    as the `device` argument.
# ---------------------------------------------------------------------------


class AdbTools:
    """Wrapper around ADB commands for device interaction."""

    def __init__(self, adb_path, device=None):
        self.adb_path = adb_path
        self.device = device
        self._device_flag = f" -s {device} " if device is not None else " "
        self.image_info = None

    def get_foreground_package(self) -> str:
        """
        Return the package name of the app currently in the foreground
        (e.g. 'com.baidu.BaiduMap').  Returns '' on failure.

        Uses `dumpsys activity activities` which works across Android 8–14.
        Falls back to `dumpsys window windows` for devices where the first
        command returns nothing useful.
        """
        device_flag = f" -s {self.device}" if self.device else ""
        for probe_cmd in (
            f"{self.adb_path}{device_flag} shell dumpsys activity activities",
            f"{self.adb_path}{device_flag} shell dumpsys window windows",
        ):
            try:
                result = subprocess.run(
                    probe_cmd, capture_output=True, text=True,
                    shell=True, timeout=6,
                )
                for line in result.stdout.splitlines():
                    if any(k in line for k in (
                        "mResumedActivity", "topResumedActivity",
                        "mCurrentFocus", "mFocusedApp",
                    )):
                        m = re.search(r'\s+([\w.]+)/[.\w]+', line)
                        if m:
                            return m.group(1)
            except Exception:
                pass
        return ""

    def get_ui_dump(self) -> str:
        """
        Dump the current UI accessibility hierarchy and return the raw XML
        string.  Tries ``adb shell uiautomator dump`` first; if that returns
        empty or very sparse XML (< 5 ``<node`` elements), falls back to the
        ``uiautomator2`` Python library (optional — install with
        ``pip install uiautomator2``).  Returns '' when both methods fail
        (WebView, game engines, ADB error, etc.).
        """
        xml = self._get_ui_dump_adb()
        if not xml or xml.count("<node") < 5:
            xml_u2 = self._get_ui_dump_u2()
            if xml_u2 and xml_u2.count("<node") > xml.count("<node"):
                return xml_u2
        return xml

    def _get_ui_dump_adb(self) -> str:
        """
        Dump UI hierarchy via ``adb shell uiautomator dump``.
        Returns the raw XML string or '' on failure.
        """
        device_flag = f" -s {self.device}" if self.device else ""
        remote = "/sdcard/window_dump.xml"
        try:
            # Write XML to device storage
            r = subprocess.run(
                f"{self.adb_path}{device_flag} shell uiautomator dump {remote}",
                capture_output=True, text=True, shell=True, timeout=8,
            )
            if "ERROR" in r.stdout or "ERROR" in r.stderr:
                return ""
            # Pull to a temp file
            with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tf:
                local = tf.name
            subprocess.run(
                f"{self.adb_path}{device_flag} pull {remote} {local}",
                capture_output=True, text=True, shell=True, timeout=8,
            )
            with open(local, encoding="utf-8", errors="replace") as f:
                xml = f.read()
            os.unlink(local)
            return xml
        except Exception:
            return ""

    def _get_ui_dump_u2(self) -> str:
        """
        Fallback UI dump via the ``uiautomator2`` Python library.
        Requires ``pip install uiautomator2`` and the atx-agent running on
        the device (installed automatically on first ``u2.connect()``).
        Returns '' if the library is not installed or the connection fails.
        """
        try:
            import uiautomator2 as u2  # optional dependency
            d = u2.connect(self.device) if self.device else u2.connect()
            xml = d.dump_hierarchy()
            return xml or ""
        except ImportError:
            return ""
        except Exception as _e:
            print(f"[UI DUMP] uiautomator2 fallback failed: {_e}")
            return ""

    # -- helpers ----------------------------------------------------------

    def _run(self, args):
        """Run an ADB command string."""
        cmd = self.adb_path + self._device_flag + args
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                shell=True,
                timeout=10,
            )
            if res.returncode != 0:
                err = (res.stderr or res.stdout or "").strip()[:180]
                print(f"[ADB] command failed (rc={res.returncode}): {args} | {err}")
                return False
            return True
        except subprocess.TimeoutExpired:
            print(f"[ADB] command timeout (>10s): {args}")
            return False
        except Exception as _e:
            print(f"[ADB] command error: {args} | {_e}")
            return False

    def _load_image_info(self, path):
        """Cache the width and height of the screenshot."""
        width, height = Image.open(path).size
        self.image_info = (width, height)

    # -- screenshot -------------------------------------------------------

    def get_screenshot(self, image_path, retry_times=3, max_age_seconds=30):
        """
        Capture a screenshot from the device and save it to *image_path*.
        Removes any stale file at *image_path* before capturing so that a
        leftover screenshot from a previous step or run cannot be returned.
        After capture, validates that the file's mtime is within
        *max_age_seconds* to guard against OS buffering anomalies.
        Returns True on success, False after exhausting retries.
        """
        # Remove stale file so os.path.exists() cannot give a false positive.
        try:
            os.remove(image_path)
        except FileNotFoundError:
            pass

        device_flag = f" -s {self.device}" if self.device else ""
        cmd = f"{self.adb_path}{device_flag} exec-out screencap -p > {image_path}"

        for _ in range(retry_times):
            subprocess.run(cmd, capture_output=True, text=True, shell=True)
            if os.path.exists(image_path):
                age = time.time() - os.path.getmtime(image_path)
                if age > max_age_seconds:
                    print(
                        f"[WARN] Screenshot at {image_path!r} is {age:.1f}s old "
                        f"(expected < {max_age_seconds}s); discarding and retrying"
                    )
                    try:
                        os.remove(image_path)
                    except OSError:
                        pass
                    time.sleep(0.5)
                    continue
                # Validate that the file is a readable image before accepting.
                # ADB failures can produce empty or non-PNG files.
                file_size = os.path.getsize(image_path)
                if file_size < 512:
                    print(
                        f"[WARN] Screenshot at {image_path!r} is suspiciously small "
                        f"({file_size} bytes); discarding and retrying"
                    )
                    try:
                        os.remove(image_path)
                    except OSError:
                        pass
                    time.sleep(0.5)
                    continue
                try:
                    self._load_image_info(image_path)
                except Exception as _img_err:
                    print(
                        f"[WARN] Screenshot at {image_path!r} is not a valid image "
                        f"({_img_err}); discarding and retrying"
                    )
                    try:
                        os.remove(image_path)
                    except OSError:
                        pass
                    time.sleep(0.5)
                    continue
                return True
            time.sleep(0.1)
        return False

    # -- input actions ----------------------------------------------------

    def click(self, x, y):
        """Tap at screen coordinate (x, y)."""
        return self._run(f"shell input tap {x} {y}")

    def long_press(self, x, y, duration=800):
        """Long-press at (x, y) for *duration* milliseconds."""
        return self._run(f"shell input swipe {x} {y} {x} {y} {duration}")

    def slide(self, x1, y1, x2, y2, slide_time=800):
        """Swipe from (x1, y1) to (x2, y2) over *slide_time* milliseconds."""
        return self._run(f"shell input swipe {x1} {y1} {x2} {y2} {slide_time}")

    def back(self):
        """Press the Back button."""
        return self._run("shell input keyevent 4")

    def home(self):
        """Press the Home button to return to the home screen."""
        return self._run(
            "shell am start -a android.intent.action.MAIN "
            "-c android.intent.category.HOME"
        )

    def type(self, text):
        """
        Type text via ADB Keyboard (supports CJK and Latin characters).
        Requires ADB Keyboard to be installed on the device.
        """
        escaped_text = text.replace('"', '\\"').replace("'", "\\'")
        command_sequence = [
            "shell ime enable com.android.adbkeyboard/.AdbIME",
            "shell ime set com.android.adbkeyboard/.AdbIME",
            0.1,  # short delay for IME switch
            f'shell am broadcast -a ADB_INPUT_TEXT --es msg "{escaped_text}"',
            0.1,
            "shell ime disable com.android.adbkeyboard/.AdbIME",
        ]

        for item in command_sequence:
            if isinstance(item, (int, float)):
                time.sleep(item)
            else:
                if not self._run(item.strip()):
                    return False
        return True

    @staticmethod
    def _normalize_for_match(s: str) -> str:
        """Normalize text for robust UI matching."""
        return re.sub(r"\s+", "", s or "").strip()

    def _ui_contains_text(self, ui_xml: str, expected_text: str) -> bool:
        """Check whether expected text appears in raw XML or node attributes."""
        if not ui_xml or not expected_text:
            return False

        if expected_text in ui_xml:
            return True

        expected_norm = self._normalize_for_match(expected_text)
        if not expected_norm:
            return False

        try:
            root = ET.fromstring(ui_xml)
        except Exception:
            return False

        for node in root.iter("node"):
            merged = " ".join([
                node.attrib.get("text", ""),
                node.attrib.get("content-desc", ""),
                node.attrib.get("hint", ""),
            ])
            merged_norm = self._normalize_for_match(merged)
            if expected_norm and expected_norm in merged_norm:
                return True
        return False

    def type_with_verification(
        self,
        text: str,
        retries: int = 2,
        verify_wait_seconds: float = 2.0,
        verify_interval_seconds: float = 0.4,
    ) -> bool:
        """
        Type text and verify it appears in the UI dump.

        Returns True only when text is observed on-screen after typing.
        """
        retries = max(1, int(retries))
        for attempt in range(1, retries + 1):
            print(f"[ADB TYPE] attempt {attempt}/{retries}: sending text {text!r}")
            if not self.type(text):
                print(f"[ADB TYPE] attempt {attempt} command sequence failed")
                continue

            deadline = time.time() + max(0.2, verify_wait_seconds)
            while time.time() < deadline:
                ui_xml = self.get_ui_dump()
                if self._ui_contains_text(ui_xml, text):
                    print(f"[ADB TYPE] text verified in UI after attempt {attempt}")
                    return True
                time.sleep(max(0.1, verify_interval_seconds))

            print(f"[ADB TYPE] text not visible after attempt {attempt}")

        return False

    # -- package management -----------------------------------------------

    def get_package_name(self, all_packages=False):
        """
        Return a sorted list of installed package names.
        If *all_packages* is False, only third-party packages are listed.
        """
        try:
            flag = "" if all_packages else " -3"
            cmd = f"{self.adb_path}{self._device_flag}shell pm list packages{flag}"
            res = subprocess.run(cmd, capture_output=True, text=True, shell=True)
            pkgs = []
            for line in res.stdout.splitlines():
                s = line.strip()
                if not s:
                    continue
                # Strip the "package:" prefix
                if s.startswith("package:"):
                    s = s[len("package:"):]
                # If the line contains "=", the right side is the package name
                if "=" in s:
                    _, s = s.split("=", 1)
                if s:
                    pkgs.append(s)
            return sorted(set(pkgs))
        except Exception as e:
            print(f"[ERROR] Failed to list packages: {e}")
            return []

    def open_app(self, package_name):
        """Launch an app by its package name."""
        self._run(
            f"shell monkey -p {package_name} "
            "-c android.intent.category.LAUNCHER 1"
        )


# ---------------------------------------------------------------------------
# Screenshot annotation
# ---------------------------------------------------------------------------

def annotate_screenshot(image_path, action_parameter, save_path="screenshot_anno.png"):
    """
    Draw action annotations (click dot / swipe arrow) on a screenshot
    and save the result to *save_path*.

    Returns the save path on success, or None if the action type is
    not visualizable.
    """
    image = Image.open(image_path)
    draw = ImageDraw.Draw(image)

    action_type = action_parameter.get("action", "")

    if action_type == "click":
        radius = 15
        cx, cy = action_parameter["coordinate"]
        draw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius),
            fill="red",
            outline="red",
        )
    elif action_type in ("scroll", "swipe"):
        x1, y1 = action_parameter["coordinate"]
        x2, y2 = action_parameter["coordinate2"]
        color = "red"
        arrow_size = 10

        # Draw the line
        draw.line((x1, y1, x2, y2), fill=color, width=2)

        # Compute arrowhead
        angle = math.atan2(y2 - y1, x2 - x1)
        ax1 = x2 - arrow_size * math.cos(angle - math.pi / 6)
        ay1 = y2 - arrow_size * math.sin(angle - math.pi / 6)
        ax2 = x2 - arrow_size * math.cos(angle + math.pi / 6)
        ay2 = y2 - arrow_size * math.sin(angle + math.pi / 6)
        draw.polygon([(x2, y2), (ax1, ay1), (ax2, ay2)], fill=color)
    else:
        return None

    image.save(save_path)
    return save_path


# ---------------------------------------------------------------------------
# Smart image resize (Qwen-VL style)
# ---------------------------------------------------------------------------

def smart_resize(height, width, factor=16, min_pixels=None, max_pixels=None):
    """
    Rescale dimensions so that:
      1. Both are divisible by *factor*.
      2. Total pixels is within [min_pixels, max_pixels].
      3. Aspect ratio is preserved as closely as possible.
    """
    IMAGE_MIN_TOKEN_NUM = 4
    IMAGE_MAX_TOKEN_NUM = 16384
    MAX_RATIO = 200

    max_pixels = max_pixels if max_pixels is not None else (IMAGE_MAX_TOKEN_NUM * factor ** 2)
    min_pixels = min_pixels if min_pixels is not None else (IMAGE_MIN_TOKEN_NUM * factor ** 2)
    assert max_pixels >= min_pixels, "max_pixels must be >= min_pixels."

    if max(height, width) / min(height, width) > MAX_RATIO:
        raise ValueError(
            f"Aspect ratio must be < {MAX_RATIO}, "
            f"got {max(height, width) / min(height, width)}"
        )

    def _round(n):
        return round(n / factor) * factor

    def _floor(n):
        return math.floor(n / factor) * factor

    def _ceil(n):
        return math.ceil(n / factor) * factor

    h_bar = max(factor, _round(height))
    w_bar = max(factor, _round(width))

    if h_bar * w_bar > max_pixels:
        beta = math.sqrt((height * width) / max_pixels)
        h_bar = _floor(height / beta)
        w_bar = _floor(width / beta)
    elif h_bar * w_bar < min_pixels:
        beta = math.sqrt(min_pixels / (height * width))
        h_bar = _ceil(height * beta)
        w_bar = _ceil(width * beta)

    return h_bar, w_bar


# ---------------------------------------------------------------------------
# VLM message construction
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = '''# Tools

You may call one or more functions to assist with the user query.

You are provided with function signatures within <tools></tools> XML tags:
<tools>
{"type": "function", "function": {"name_for_human": "mobile_use", "name": "mobile_use", "description": "Use a touchscreen to interact with a mobile device, and take screenshots.
* This is an interface to a mobile device with touchscreen. You can perform actions like clicking, typing, swiping, etc.
* Some applications may take time to start or process actions, so you may need to wait and take successive screenshots to see the results of your actions.
* The screen's resolution is 1000x1000.
* Make sure to click any buttons, links, icons, etc with the cursor tip in the center of the element. Don't click boxes on their edges unless asked.", "parameters": {"properties": {"action": {"description": "The action to perform. The available actions are:
* `key`: Perform a key event on the mobile device.
    - This supports adb's `keyevent` syntax.
    - Examples: \\"volume_up\\", \\"volume_down\\", \\"power\\", \\"camera\\", \\"clear\\".
* `click`: Click the point on the screen with coordinate (x, y).
* `long_press`: Press the point on the screen with coordinate (x, y) for specified seconds.
* `swipe`: Swipe from the starting point with coordinate (x, y) to the end point with coordinates2 (x2, y2).
* `type`: Input the specified text into the activated input box.
* `system_button`: Press the system button.
* `open`: Open an app on the device.
* `wait`: Wait specified seconds for the change to happen.
* `answer`: Terminate the current task and output the answer.
* `interact`: Resolve the blocking window by interacting with the user.
* `terminate`: Terminate the current task and report its completion status.", "enum": ["key", "click", "long_press", "swipe", "type", "system_button", "open", "wait", "answer", "interact", "terminate"], "type": "string"}, "coordinate": {"description": "(x, y): The x (pixels from the left edge) and y (pixels from the top edge) coordinates to move the mouse to. Required only by `action=click`, `action=long_press`, and `action=swipe`.", "type": "array"}, "coordinate2": {"description": "(x, y): The x (pixels from the left edge) and y (pixels from the top edge) coordinates to move the mouse to. Required only by `action=swipe`.", "type": "array"}, "text": {"description": "Required only by `action=key`, `action=type`, `action=open`, `action=answer`,and `action=interact`.", "type": "string"}, "time": {"description": "The seconds to wait. Required only by `action=long_press` and `action=wait`.", "type": "number"}, "button": {"description": "Back means returning to the previous interface, Home means returning to the desktop, Menu means opening the application background menu, and Enter means pressing the enter. Required only by `action=system_button`", "enum": ["Back", "Home", "Menu", "Enter"], "type": "string"}, "status": {"description": "The status of the task. Required only by `action=terminate`.", "type": "string", "enum": ["success", "failure"]}}, "required": ["action"], "type": "object"}, "args_format": "Format the arguments as a JSON object."}}
</tools>

For each function call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:
<tool_call>
{"name": <function-name>, "arguments": <args-json-object>}
</tool_call>

# Response format

Response format for every step:
1) Action: a short imperative describing what to do in the UI.
2) A single <tool_call>...</tool_call> block containing only the JSON: {"name": <function-name>, "arguments": <args-json-object>}.

Rules:
- Output exactly in the order: Action, <tool_call>.
- Be brief: one for Action.
- Do not output anything else outside those two parts.
- If finishing, use action=terminate in the tool call.

# Critical execution rules — MUST follow every step


## Always execute, never refuse
- You MUST always try to execute the task, even if the current screen is not the right app or state.
- If you are on the wrong screen or wrong app, navigate there yourself: press Home to go to the home screen, then open the required app. Never give up or tell the user to do it themselves.
- NEVER use `answer` to describe what the user should do manually. `answer` is only for reporting a completed task result.
- Do NOT use `answer` or `terminate` until you have actually performed the required actions and can see in the screenshot that the task is complete.

## Verify every step before proceeding
- After every action, look at the new screenshot and VERIFY the action had the intended effect before deciding the next step.
- If the action did not produce the expected result (wrong app, wrong screen, wrong text entered, wrong search result), take immediate corrective action — do NOT proceed as if it succeeded.
- When selecting a search result or map location, carefully read the text to confirm it matches the target. If it does not match, go back and try again.
- Never declare success ("navigate completed", "task done") unless the screenshot clearly shows the task outcome.

## App context verification (run at every step)
- Before choosing any action, identify which app is currently on screen by reading the status bar, app title bar, or distinctive UI elements.
- If the task requires a specific app (e.g., Baidu Maps / 百度地图) and the current screenshot does NOT show that app, your ONLY valid next action is: press Home (system_button=Home), then open the correct app.
- NEVER execute task-specific actions (type text, tap search results, tap navigation buttons) while the wrong app is in the foreground.
- "Wait for the debug interface to stabilise" is NOT a valid step. Seeing a debug or developer screen means you are in the wrong app — press Home immediately and open the correct app.
- If you see PicoClaw, a terminal, a settings screen, or any non-target interface, treat it as a wrong-app situation and navigate away before doing anything else.

## Prioritise direct UI shortcuts
- Before planning a multi-step flow, ALWAYS scan the UI elements list for a button or link that directly performs the task goal.
- Examples: a "导航" / "开始导航" / "Navigate" button on a location card, a pre-populated search field, a 一键导航 (one-tap navigation) shortcut.
- If such a direct shortcut appears in the UI elements list or is visible in the screenshot, click it IMMEDIATELY — do NOT start a longer manual flow.
- Do not scroll past or dismiss a result card that already offers the required action.

## Recovery when stuck or looping
- If you have executed the same action (same tap coordinate, same button) 3 or more times without any change in screen state, your approach is not working.
- Recovery steps (in order):
  1. Press Back once (返回上一界面) to dismiss overlays or exit a dead-end screen.
  2. If still stuck after Back, press Home, then reopen the required app with the `open` action.
  3. If the app appears frozen or in an unrecoverable state, press Home and use `open` to force-relaunch it — a fresh start is always better than endless retries.
- Do NOT keep tapping the same coordinate hoping the result will change. Recognise the loop and break out of it.

## Honesty rules for the answer action
- ONLY use the answer action when the task outcome is LITERALLY VISIBLE in the current screenshot.
- Do NOT fabricate or assume any information that is not shown on screen: distances, travel times, congestion indices, prices, ratings, status messages, or any other numbers/text.
- If you cannot see clear confirmation that the task completed (e.g. navigation actively running, booking confirmed screen), do NOT answer — take the next required action instead.
- Your answer text must describe only what is visible. Never extrapolate from partial information. If the screen shows a destination pin but navigation has not started, say so — do not invent route details.'''

# Compact system prompt for small-context models (≤2048 tokens).
# Contains only the tool schema + response format; drops behavioural rules so
# the prompt itself fits within the model's context window.
# Derived dynamically so it stays in sync when SYSTEM_PROMPT is updated.
SYSTEM_PROMPT_COMPACT = SYSTEM_PROMPT[:SYSTEM_PROMPT.index('\n# Critical execution rules')]


# ---------------------------------------------------------------------------
# UI hierarchy helpers
# ---------------------------------------------------------------------------

def summarise_ui_dump(xml: str, max_nodes: int = 60) -> str:
    """
    Parse the raw uiautomator XML dump and return a compact, human-readable
    summary of the interactive UI elements on screen.

    Only nodes that are either clickable, long-clickable, or carry visible text
    are included.  The output is intentionally terse so it fits comfortably
    inside the VLM prompt without crowding the screenshot context.

    Returns '' if the XML cannot be parsed or contains no useful nodes.
    """
    if not xml:
        return ""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return ""

    lines: list[str] = []
    for node in root.iter("node"):
        attrib = node.attrib
        clickable = attrib.get("clickable") == "true"
        long_clickable = attrib.get("long-clickable") == "true"
        text = (attrib.get("text") or "").strip()
        content_desc = (attrib.get("content-desc") or "").strip()
        cls = attrib.get("class", "").rsplit(".", 1)[-1]   # e.g. "Button"
        bounds = attrib.get("bounds", "")
        resource_id = attrib.get("resource-id", "")
        # Derive a short id (strip package prefix)
        short_id = resource_id.split("/")[-1] if "/" in resource_id else resource_id
        enabled = attrib.get("enabled") == "true"

        # Skip nodes with nothing useful to show
        label = text or content_desc
        if not label and not clickable and not long_clickable:
            continue
        if not enabled:
            continue

        parts: list[str] = [f"[{cls}]"]
        if label:
            parts.append(f'"{label}"')
        if short_id:
            parts.append(f"id={short_id}")
        if bounds:
            parts.append(f"bounds={bounds}")
        flags: list[str] = []
        if clickable:
            flags.append("clickable")
        if long_clickable:
            flags.append("long-clickable")
        if flags:
            parts.append(" ".join(flags))
        lines.append("  " + "  ".join(parts))

        if len(lines) >= max_nodes:
            lines.append(f"  ... (truncated at {max_nodes} nodes)")
            break

    if not lines:
        return ""
    return "UI elements on screen:\n" + "\n".join(lines)


def build_messages(image_path, instruction, history_output, model_name,
                   history_n=4, foreground_pkg: str = "", ui_summary: str = "",
                   installed_apps_hint: str = "", target_app_hint: str = "",
                   compact: bool = False):
    """
    Construct the multi-turn message list for the VLM.

    Args:
        image_path:      Path to the current screenshot.
        instruction:     The user's task instruction.
        history_output:  List of dicts with keys 'output' and 'image'.
        model_name:      Model identifier (affects history summarization).
        history_n:       Number of recent history turns to include as images.

    Returns:
        A list of message dicts suitable for the DashScope API.
    """
    current_step = len(history_output)
    history_start_idx = max(0, current_step - history_n)

    # Summarize early actions (before the image-history window)
    # In compact mode keep only a tiny tail to stay inside 2k-token contexts.
    _max_prev_steps = 3 if compact else 30
    _max_prev_action_chars = 90 if compact else 240
    _max_prev_total_chars = 420 if compact else 4000
    previous_actions = []
    _prev_start = max(0, history_start_idx - _max_prev_steps)
    _omitted_prev = max(0, _prev_start)
    for i in range(_prev_start, history_start_idx):
        if i < len(history_output):
            text = history_output[i]["output"]
            if model_name.endswith(".mem"):
                if "<tool_call>" in text:
                    text = text.split("<tool_call>")[0].strip()
            else:
                if "Action:" in text and "<tool_call>" in text:
                    text = text.split("Action:")[1].split("<tool_call>")[0].strip()
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > _max_prev_action_chars:
                text = text[:_max_prev_action_chars].rstrip() + "..."
            previous_actions.append(f"Step {i + 1}: {text}")

    if _omitted_prev > 0:
        previous_actions.insert(0, f"... ({_omitted_prev} earlier steps omitted)")

    previous_actions_str = "\n".join(previous_actions) if previous_actions else "None"
    if len(previous_actions_str) > _max_prev_total_chars:
        previous_actions_str = previous_actions_str[:_max_prev_total_chars].rstrip() + "..."

    # Build date context
    today = datetime.today()
    weekday_names = [
        "Monday", "Tuesday", "Wednesday", "Thursday",
        "Friday", "Saturday", "Sunday",
    ]
    formatted_date = today.strftime("%Y-%m-%d") + " " + weekday_names[today.weekday()]
    date_info = f"Today's date is: {formatted_date}."

    instruction_prompt = (
        f"Please generate the next move according to the UI screenshot, "
        f"instruction and previous actions.\n\n"
        f"Instruction: {date_info}{instruction}\n\n"
        f"Previous actions:\n{previous_actions_str}"
    )
    if foreground_pkg:
        instruction_prompt += (
            f"\n\nCurrent foreground app (from ADB): {foreground_pkg}\n"
            f"Verify this matches the app required by the task before acting."
        )
    if ui_summary:
        instruction_prompt += f"\n\n{ui_summary}\nUse the bounds and resource IDs above to choose exact tap coordinates instead of guessing from the screenshot alone."

    if installed_apps_hint:
        _apps_hint = installed_apps_hint
        if compact:
            _apps = [x.strip() for x in installed_apps_hint.split(",") if x.strip()]
            _apps = _apps[:12]
            _apps_hint = ", ".join(_apps)
            if len(_apps_hint) > 180:
                _apps_hint = _apps_hint[:180].rstrip() + "..."
        instruction_prompt += (
            f"\n\nInstalled apps on device (use only exact matches from this list): {_apps_hint}"
        )
    if target_app_hint:
        instruction_prompt += (
            f"\nTarget app candidate for this task: {target_app_hint}"
            f"\nIf this exact app is installed, prefer action=open with that name instead of tapping a similarly named app icon."
        )

    if compact:
        # Compact prompt omits behavioural rules — add a brief reminder so the
        # model doesn't give up when the required app is not yet in foreground.
        instruction_prompt += (
            "\n\nIf the required app is not on screen, use action=open to "
            "launch it. Never say an app is unavailable — navigate to it."
        )

    # Assemble messages
    _system_text = SYSTEM_PROMPT_COMPACT if compact else SYSTEM_PROMPT
    messages = [
        {
            "role": "system",
            "content": [{"text": _system_text}],
        }
    ]

    history_len = min(history_n, len(history_output))
    if history_len > 0:
        for idx, item in enumerate(history_output[-history_n:]):
            if idx == 0:
                messages.append({
                    "role": "user",
                    "content": [
                        {"text": instruction_prompt},
                        {"image": "file://" + item["image"]},
                    ],
                })
            else:
                messages.append({
                    "role": "user",
                    "content": [{"image": "file://" + item["image"]}],
                })
            messages.append({
                "role": "assistant",
                "content": [{"text": item["output"]}],
            })
        messages.append({
            "role": "user",
            "content": [{"image": "file://" + image_path}],
        })
    else:
        messages.append({
            "role": "user",
            "content": [
                {"text": instruction_prompt},
                {"image": "file://" + image_path},
            ],
        })

    return messages


ERROR_CALLING_LLM = 'Error calling LLM'

def pil_to_base64(image):
    buffer = BytesIO()
    image.save(buffer, format="PNG") 
    return base64.b64encode(buffer.getvalue()).decode("utf-8")

def image_to_base64(image_path, max_pixels=None):
    if isinstance(image_path, str) and image_path.startswith("file://"):
        image_path = image_path[7:]
    dummy_image = Image.open(image_path)
    if max_pixels is not None:
        MIN_PIXELS = 3136
        resized_height, resized_width = smart_resize(
            dummy_image.height, dummy_image.width,
            factor=28,
            min_pixels=MIN_PIXELS,
            max_pixels=max_pixels,
        )
        dummy_image = dummy_image.resize((resized_width, resized_height))
    return f"data:image/png;base64,{pil_to_base64(dummy_image)}"

class LlmWrapper(abc.ABC):
    """Abstract interface for (text only) LLM."""
    @abc.abstractmethod
    def predict(
        self,
        text_prompt: str,
    ) -> tuple[str, Optional[bool], Any]:
        """Calling multimodal LLM with a prompt and a list of images.

        Args:
        text_prompt: Text prompt.

        Returns:
        Text output, is_safe, and raw output.
        """

class MultimodalLlmWrapper(abc.ABC):
    """Abstract interface for Multimodal LLM."""
    @abc.abstractmethod
    def predict_mm(
        self, text_prompt: str, images: list[np.ndarray]
    ) -> tuple[str, Optional[bool], Any]:
        """Calling multimodal LLM with a prompt and a list of images.

        Args:
        text_prompt: Text prompt.
        images: List of images as numpy ndarray.

        Returns:
        Text output and raw output.
        """

class GUIOwlWrapper(LlmWrapper, MultimodalLlmWrapper):

    RETRY_WAITING_SECONDS = 20

    def __init__(
            self,
            api_key: str,
            base_url: str,
            model_name: str,
            max_retry: int = 10,
            temperature: float = 0.0,
            max_context_size: Optional[int] = None,
    ):
        if max_retry <= 0:
            max_retry = 10
            print('Max_retry must be positive. Reset it to 3')
        self.max_retry = min(max_retry, 10)
        self.temperature = temperature
        self.model = model_name
        # When set, used to calculate initial image budget and detect when text
        # alone exceeds the context so we trim history instead of the image.
        self.max_context_size: Optional[int] = max_context_size
        self.bot = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=30
        )

    def convert_messages_format_to_openaiurl(self, messages, max_pixels=None):
      converted_messages = []
      for message in messages:
          new_content = []
          for item in message['content']:
              if list(item.keys())[0] == 'text':
                  new_content.append({'type': 'text', 'text': item['text']})
              elif list(item.keys())[0] == 'image':
                new_content.append({'type': 'image_url', 'image_url': {'url': image_to_base64(item['image'], max_pixels=max_pixels)}})
          converted_messages.append({'role': message['role'], 'content': new_content})

      return converted_messages

    @staticmethod
    def _trim_payload_history(original_payload: list, keep_n: int) -> list:
        """
        Return a copy of *original_payload* keeping only the last *keep_n*
        history messages (everything between the system message and the final
        user message).  Always keeps [0] (system) and [-1] (current user).
        """
        if len(original_payload) <= 2:
            return original_payload
        history = original_payload[1:-1]
        trimmed = history[-keep_n:] if keep_n > 0 else []
        return [original_payload[0]] + trimmed + [original_payload[-1]]

    def predict(
            self,
            text_prompt: str,
    ) -> tuple[str, Optional[bool], Any]:
        return self.predict_mm(text_prompt, [])

    def predict_mm(
            self, messages = None
    ) -> tuple[str, Optional[bool], Any]:

        # Use declared context size if available; otherwise assume large model.
        _n_ctx = self.max_context_size or 32768
        # Initial max_pixels: budget 60% of context for input, 700 tokens for
        # text, remainder split across images.  Clamped to [3136, 401408].
        n_images = sum(
            1
            for msg in messages
            for item in msg.get('content', [])
            if 'image' in item
        )
        if self.max_context_size:
            _text_reserve = 700
            _available = int(_n_ctx * 0.60) - _text_reserve
            _tokens_per_image = max(_available // max(n_images, 1), 16)
            max_pixels = max(min(_tokens_per_image * 28 * 28, 401408), 3136)
        else:
            max_pixels = None  # no resize — provider receives image at original resolution

        # Pixels-per-token ratio for the vision encoder (28×28 = 784 px/tok).
        _PX_PER_TOK = 28 * 28

        payload = self.convert_messages_format_to_openaiurl(messages, max_pixels=max_pixels)
        # Snapshot the base payload for history trimming (re-using pre-encoded images).
        _base_payload = list(payload)
        _keep_n_history = len(_base_payload) - 2  # number of history slots
        # Flag: set True once history is already at minimum so next text-overflow
        # gives up rather than retrying with the same payload indefinitely.
        _history_trim_exhausted = False

        counter = self.max_retry
        wait_seconds = self.RETRY_WAITING_SECONDS
        while counter > 0:
            try:
              # For small-context models the server default max_tokens can be
              # very small (a few dozen tokens), causing truncated JSON output.
              # Request enough for a complete tool_call; the server will cap at
              # n_ctx − n_prompt_tokens regardless.
              _gen_kwargs: dict = {}
              if self.max_context_size:
                  _gen_kwargs['max_tokens'] = max(self.max_context_size // 4, 256)
              chat_completion_from_url = self.bot.chat.completions.create(model=self.model, messages=payload, **_gen_kwargs)
              return (chat_completion_from_url.choices[0].message.content, payload, chat_completion_from_url)
            except Exception as e:
                error_str = str(e)
                if 'exceed_context_size_error' in error_str:
                    np_m = re.search(r"'n_prompt_tokens':\s*(\d+)", error_str)
                    nc_m = re.search(r"'n_ctx':\s*(\d+)", error_str)
                    if np_m and nc_m:
                        n_prompt = int(np_m.group(1))
                        n_ctx   = int(nc_m.group(1))
                        target  = int(n_ctx * 0.60)
                        # Separate image tokens from text tokens so we only
                        # scale the image portion, not the whole prompt.
                        # When no resize was applied (max_pixels is None), estimate
                        # image tokens as n_prompt minus a text-token allowance.
                        img_tok_est  = (n_prompt - 700) if max_pixels is None else max_pixels / _PX_PER_TOK
                        text_tok_est = n_prompt - img_tok_est
                        target_img_tok = target - text_tok_est
                        if target_img_tok <= 0 or (max_pixels is not None and max_pixels <= 3136):
                            # Text alone exceeds budget — shrinking image won't help.
                            # Give up only if we already tried at minimum history.
                            if _history_trim_exhausted:
                                print(
                                    f'Cannot fit content in {n_ctx}-token context '
                                    f'even after trimming history — giving up'
                                )
                                return ERROR_CALLING_LLM, None, None
                            # Trim history from the base payload progressively.
                            _prev_keep = _keep_n_history
                            _keep_n_history = max(0, _keep_n_history // 2)
                            payload = self._trim_payload_history(_base_payload, _keep_n_history)
                            if _keep_n_history == _prev_keep:
                                # Reached minimum — mark so we give up next time.
                                _history_trim_exhausted = True
                            print(
                                f'Context exceeded by text; trimming history to '
                                f'last {_keep_n_history} message(s) and retrying...'
                            )
                        else:
                            new_max_pixels = max(int(target_img_tok * _PX_PER_TOK), 3136)
                            max_pixels = new_max_pixels
                            payload = self.convert_messages_format_to_openaiurl(messages, max_pixels=max_pixels)
                            print(f'Image too large for context, resizing to max_pixels={max_pixels} and retrying...')
                    else:
                        # Fallback: halve current max_pixels (or start from 401408
                        # if image was sent at full resolution).
                        max_pixels = max(int((max_pixels or 401408) * 0.5), 3136)
                        payload = self.convert_messages_format_to_openaiurl(messages, max_pixels=max_pixels)
                        print(f'Image too large for context, resizing to max_pixels={max_pixels} and retrying...')
                else:
                    # Fatal errors (quota exhausted, auth failure) will not
                    # recover with retries — return immediately so the caller
                    # can switch to a fallback provider right away.
                    _status = getattr(e, 'status_code', None)
                    if _status in (401, 403):
                        print(f'Fatal provider error ({_status}) — not retrying; try fallback provider')
                        return ERROR_CALLING_LLM, None, None
                    time.sleep(wait_seconds)
                counter -= 1
                print('Error calling LLM, will retry soon...')
                print(e)
        return ERROR_CALLING_LLM, None, None


# ---------------------------------------------------------------------------
# App name resolution via LLM
# ---------------------------------------------------------------------------

def resolve_app_name_via_llm(instruction, app_name_list_str, api_key, base_url, model="qwen-plus"):
    """
    Use an LLM to determine which installed app should be opened
    based on the user instruction.

    Args:
        instruction:        The user's natural-language instruction.
        app_name_list_str:  Comma-separated string of installed app names.
        api_key:            API key for the LLM service.
        base_url:           Base URL for the LLM service.
        model:              Model name to use.

    Returns:
        The resolved app name (str), or empty string if unresolvable.
    """
    from openai import OpenAI

    prompt = f'''Role and Task:
You are an app resolver. Given a natural language instruction and a list of
installed app names on a device, determine which app needs to be opened and
output the corresponding name.

Input:
User instruction: "{instruction}"
Installed apps: "{app_name_list_str}"

Rules:
- Only select from the given app name list; never fabricate names.
- If the instruction explicitly names an app (e.g., "open WeChat"):
  - If that app is in the list, return its name.
  - If not in the list, return an empty string.
  - Do NOT substitute with a similar app.
- If the instruction is generic (e.g., "open a browser / map / camera"):
  - Pick any matching candidate from the list.
  - If no candidate exists, return an empty string.

Output format (important):
Only output JSON, no extra text.
JSON fields:
{{
  "reason": "<brief decision reason (1-2 sentences)>",
  "app": "<string, empty string if unable to determine>"
}}

Examples (for style reference only):

User instruction: "open WeChat"
Installed apps: "WeChat, Taobao"
Output:
{{
  "reason": "WeChat is explicitly named and exists in the list.",
  "app": "WeChat"
}}

User instruction: "open a browser"
Installed apps: "Google Chrome, Firefox"
Output:
{{
  "reason": "No specific browser named; multiple exist; returning Google Chrome.",
  "app": "Google Chrome"
}}

User instruction: "open iQIYI"
Installed apps: "Tencent Video, Bilibili"
Output:
{{
  "reason": "iQIYI is explicitly named but not installed; returning empty.",
  "app": ""
}}

User instruction: "open a map"
Installed apps: "Taobao, WeChat"
Output:
{{
  "reason": "A map app is needed but none is installed; returning empty.",
  "app": ""
}}
'''

    client = OpenAI(api_key=api_key, base_url=base_url)

    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": prompt},
        ],
        stream=False,
    )

    res_text = completion.choices[0].message.content
    print(f"[APP RESOLVER] LLM response: {res_text}")

    parsed = _try_parse_json(res_text)
    if parsed and "app" in parsed:
        return parsed["app"]
    return ""


def _try_parse_json(text):
    """Extract and parse the first JSON object from text.

    Strategy (tried in order):
    1. Strip <think>...</think> blocks, then parse the remainder.
    2. If the remainder is empty or invalid, regex-extract the first
       balanced {...} from the stripped text.
    3. Last resort: extract the first {...} from the FULL original text
       (catches the case where the entire response is inside <think>).
    Also handles ```json / ``` markdown fences.
    """
    if not text:
        return None

    def _extract_first_obj(s: str):
        """Return the first balanced {...} substring, or None."""
        start = s.find("{")
        if start == -1:
            return None
        depth = 0
        for i, c in enumerate(s[start:], start):
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return s[start : i + 1]
        return None

    # Pass 1 — strip think blocks, handle fences, try direct parse
    stripped = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    target = stripped if stripped else text
    if "```json" in target:
        target = target.split("```json")[1].split("```")[0].strip()
    elif target.startswith("```"):
        target = target.strip("`").strip()
    if target:
        try:
            return json.loads(target)
        except json.JSONDecodeError:
            pass

    # Pass 2 — regex-extract first {...} from stripped/post-fence text
    candidate = _extract_first_obj(target)
    if candidate:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # Pass 3 — search the full original text (think block may contain the JSON)
    candidate = _extract_first_obj(text)
    if candidate:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    print("[WARN] JSON parse failed: no valid JSON object found in response")
    return None


# ---------------------------------------------------------------------------
# Supervisor LLM — verifies each step before execution
# ---------------------------------------------------------------------------

_SUPERVISOR_SYSTEM = """\
You are a mobile automation supervisor. Your only job is to verify that the \
action the VLM agent is about to take is safe, correct, and actually moves \
the task forward.

You will be given:
- The user's task
- The foreground app currently on screen (from ADB ground truth)
- The actual UI elements on screen (from uiautomator dump — ground truth)
- A screenshot of the current screen (when provided — treat it as PRIMARY \
evidence; it shows everything including WebView/canvas content that the \
UI dump may miss)
- The VLM's plain-text reasoning (what it says it is doing)
- The exact tool_call it proposes to execute (JSON)

You must check for these failure modes and override when found:

0. WRONG OPEN TARGET — The action is `open` and the `text` field names an app \
that is WRONG or ambiguous for the task.
   Common confusions that must be caught:
   • '百度' (Baidu Search/Browser, com.baidu.searchbox) is NOT '百度地图' \
(Baidu Maps, com.baidu.BaiduMap).
   • '高德' is NOT '高德地图' (Amap/AutoNavi Maps).
   • Any partial alias that resolves to a different category of app than what \
the task requires (search engine vs. map, browser vs. travel app, etc.).
   When "Installed apps on device" is provided, check whether the exact open \
target exists in that list. If the target is absent or is a known alias for \
the wrong app, override with open using the precise app name from the task \
instruction. Exact match is required — do not let shorter aliases slip through.

1. WRONG APP — The foreground app is NOT the app required by the task.
   EXCEPTION — System UI overlays are NOT wrong-app situations; they are \
mandatory dialogs that must be handled in place:
   • com.android.permissioncontroller — permission dialog; agent should tap \
the appropriate Allow / Deny / Grant button.
   • com.android.systemui — system notification or status-bar overlay.
   • Any launcher (net.oneplus.launcher, com.android.launcher*, \
com.miui.home, com.huawei.android.launcher, etc.) — home screen reached \
normally; agent should open the correct app.
   For all other wrong-app foreground packages, the only valid override is \
system_button=Home or open=<correct app>.

2. INTENT/ACTION MISMATCH — The VLM says "press Home" / "返回主屏幕" / \
"按主页" in its text but the tool_call is a `click` at a coordinate.
   Override with system_button=Home.

3. LOOPING IN WRONG APP — The VLM is clearly trying to accomplish the task \
inside the wrong app (e.g. searching in Google Maps instead of Baidu Maps).
   Override with system_button=Home.

4. HALLUCINATED ANSWER — The action is "answer" and the answer text describes \
specific information (distances, times, prices, status indicators, numbers) \
that are NOT visible on screen. When a screenshot is provided, look at it \
directly — if the claimed numbers/text are not visible on screen, override \
with a `wait` action. When only UI elements are available, cross-check those.

5. PREMATURE ANSWER — The action is "answer" but the task goal is clearly \
not yet achieved (e.g. task requires navigation running but only a pin is \
shown; no confirmation screen visible). Override with a `wait` action.

6. APPROVE — If none of the above apply, approve.

If you need to reason before deciding, wrap your thinking in <think>...</think> \
tags first, then output the JSON. The JSON must be the LAST thing you output.

Output ONLY a JSON object (after any <think> block), no markdown, no extra text:
- Approve:  {"verdict": "approve"}
- Override: {"verdict": "override", "tool_call": {"name": "mobile_use", \
"arguments": {"action": "...", ...}}, "reason": "one sentence"}
"""

_SUPERVISOR_USER_TMPL = """\
Task: {task}
Foreground app (ADB): {fg_label}
UI elements on screen (ground truth): {ui_summary}
VLM reasoning text: {action_text}
Proposed tool_call: {tool_call_json}
"""

_TASK_COMPLETE_SYSTEM = """\
You are a mobile automation auditor. Your only job is to decide whether a \
task has been fully and successfully completed based on the execution history \
and the current screen.

You will be given:
- The original task instruction
- A summary of the last steps taken (action history)
- The foreground app currently on screen (ADB ground truth)
- The UI elements visible on screen
- A screenshot of the current screen (when provided — treat as PRIMARY evidence)
- The VLM agent's conclusion text

Decide: is the task ACTUALLY complete?

Be strict. The task is complete only if the required outcome is verifiably \
achieved. Key rules:
- Navigation tasks: turn-by-turn navigation must be RUNNING, not just a pin \
or destination set on the map.
- Search tasks: relevant results must be visible on screen.
- Media tasks: the content must be playing.
- Purchase/form tasks: submission confirmation must be visible.
- If the agent's conclusion text is consistent with what the screen shows, \
lean toward complete. If the conclusion claims something not visible, \
mark not complete.

Output ONLY a JSON object (after any <think> block):
- Complete:     {"complete": true,  "reason": "one sentence"}
- Not complete: {"complete": false, "reason": "what is still missing"}
"""

_TASK_COMPLETE_USER_TMPL = """\
Task: {task}
Foreground app: {fg_label}
UI elements on screen: {ui_summary}
Steps taken ({step_count} steps, showing last 10):
{history_summary}
Agent's conclusion: {conclusion}
"""


class SupervisorLLM:
    """
    Supervisor LLM that validates each VLM step before execution.
    Set vision=True when the supervisor model supports image input.
    Set reasoning_split=True for models that support the MiniMax/reasoning_split
    parameter (MiniMax-M3, etc.) — this separates chain-of-thought into
    reasoning_details so that content contains ONLY the JSON verdict.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        vision: bool = False,
        reasoning_split: bool = False,
    ):
        self.model = model
        self.vision = vision
        self.reasoning_split = reasoning_split
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=60)

    def validate(
        self,
        task: str,
        fg_label: str,
        action_text: str,
        tool_call_dict: dict,
        ui_summary: str = "",
        screenshot_path: str = "",
        installed_apps_hint: str = "",
    ) -> dict:
        """
        Returns one of:
          {"verdict": "approve"}
          {"verdict": "override", "tool_call": {...}, "reason": "..."}
        Returns {"verdict": "approve"} on any error so as not to block execution.

        When self.vision is True and screenshot_path is provided, the screenshot
        is sent alongside the text context so the supervisor can directly verify
        what is visible on screen (including WebView/canvas content).

        installed_apps_hint: comma-separated display names of installed apps on
        the device.  Passed when the proposed action is ``open`` so the
        supervisor can verify the open target against actual installed apps.
        """
        user_text = _SUPERVISOR_USER_TMPL.format(
            task=task,
            fg_label=fg_label,
            ui_summary=(ui_summary or "(not available)"),
            action_text=(action_text or "").strip()[:800],
            tool_call_json=json.dumps(tool_call_dict, ensure_ascii=False),
        )
        if installed_apps_hint:
            user_text += f"\nInstalled apps on device: {installed_apps_hint}"
        # Build user message: multimodal (image + text) or plain text
        if self.vision and screenshot_path and os.path.exists(screenshot_path):
            try:
                with open(screenshot_path, "rb") as _img_f:
                    _b64 = base64.b64encode(_img_f.read()).decode()
                user_content: Any = [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_b64}"}},
                    {"type": "text", "text": user_text},
                ]
                print("[SUPERVISOR] sending screenshot for visual verification")
            except Exception as _enc_err:
                print(f"[SUPERVISOR] could not encode screenshot ({_enc_err}) — falling back to text-only")
                user_content = user_text
        else:
            user_content = user_text
        _extra_body: dict = {}
        if self.reasoning_split:
            _extra_body["reasoning_split"] = True
        _sup_max_attempts = 3
        for _sup_try in range(1, _sup_max_attempts + 1):
            try:
                print(f"[SUPERVISOR] validate attempt {_sup_try}/{_sup_max_attempts}")
                resp = self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": _SUPERVISOR_SYSTEM},
                        {"role": "user", "content": user_content},
                    ],
                    temperature=0,
                    max_tokens=512,
                    **(dict(extra_body=_extra_body) if _extra_body else {}),
                )
                # With reasoning_split=True, content has only the JSON verdict;
                # the chain-of-thought is in reasoning_details (we don't need it).
                raw = (resp.choices[0].message.content or "").strip()
                if not raw:
                    print("[SUPERVISOR] empty response from API — approving by default")
                    return {"verdict": "approve"}
                parsed = _try_parse_json(raw)
                if parsed:
                    v = str(parsed.get("verdict", "")).lower()
                    # Normalise past-tense forms ("approved" → "approve", etc.)
                    if v in ("approve", "approved"):
                        return {"verdict": "approve"}
                    if v in ("override", "overridden"):
                        return dict(parsed, verdict="override")
                # Last-resort keyword scan — model output chain-of-thought prose
                # instead of bare JSON (happens even with reasoning_split=True on
                # some model versions).  Scan the final 300 chars for a verdict.
                _tail = raw[-300:].lower()
                if re.search(r'\b(approve|approved)\b', _tail) and "override" not in _tail:
                    return {"verdict": "approve"}
                if "override" in _tail:
                    print(f"[SUPERVISOR] prose override — cannot parse action JSON; defaulting to Home: {raw[:80]!r}")
                    return {
                        "verdict": "override",
                        "tool_call": {"name": "mobile_use", "arguments": {"action": "system_button", "button": "Home"}},
                        "reason": "prose override (unparsed): supervisor flagged wrong action — pressing Home",
                    }
                else:
                    print(f"[SUPERVISOR] unexpected response format — approving: {raw[:120]!r}")
                return {"verdict": "approve"}
            except Exception as _e:
                _is_timeout = "timeout" in str(_e).lower() or "timed out" in str(_e).lower()
                _is_auth = getattr(_e, "status_code", None) in (401, 403)
                if _is_auth:
                    print(f"[SUPERVISOR] auth error — approving by default: {_e}")
                    return {"verdict": "approve"}
                if _sup_try < _sup_max_attempts and _is_timeout:
                    print(f"[SUPERVISOR] timeout on attempt {_sup_try} — retrying in 3s")
                    time.sleep(3)
                    continue
                print(f"[SUPERVISOR] error — approving by default: {_e}")
                return {"verdict": "approve"}
        return {"verdict": "approve"}

    def is_task_complete(
        self,
        task: str,
        fg_label: str,
        ui_summary: str,
        history: list,
        conclusion: str,
        screenshot_path: str = "",
    ) -> dict:
        """
        Ask whether the overall task has been fully achieved.
        Returns {"complete": True/False, "reason": "..."}
        Returns {"complete": True, "reason": "error"} on failure so as not to
        block execution when the supervisor API is unavailable.
        """
        history_lines = []
        for i, h in enumerate(history[-10:], 1):
            out = h.get("output", "")
            first_line = out.split("\n")[0][:120] if out else "(no output)"
            history_lines.append(f"  {i}. {first_line}")
        history_summary = "\n".join(history_lines) if history_lines else "  (no history)"

        user_text = _TASK_COMPLETE_USER_TMPL.format(
            task=task,
            fg_label=fg_label,
            ui_summary=(ui_summary or "(not available)"),
            step_count=len(history),
            history_summary=history_summary,
            conclusion=(conclusion or "").strip()[:500],
        )

        if self.vision and screenshot_path and os.path.exists(screenshot_path):
            try:
                with open(screenshot_path, "rb") as _img_f:
                    _b64 = base64.b64encode(_img_f.read()).decode()
                user_content: Any = [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_b64}"}},
                    {"type": "text", "text": user_text},
                ]
                print("[SUPERVISOR] checking task completion with screenshot")
            except Exception as _enc_err:
                print(f"[SUPERVISOR] could not encode screenshot ({_enc_err}) — text-only")
                user_content = user_text
        else:
            user_content = user_text

        _extra_body: dict = {}
        if self.reasoning_split:
            _extra_body["reasoning_split"] = True
        _tc_max_attempts = 3
        for _tc_try in range(1, _tc_max_attempts + 1):
            try:
                print(f"[SUPERVISOR] task-complete attempt {_tc_try}/{_tc_max_attempts}")
                resp = self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": _TASK_COMPLETE_SYSTEM},
                        {"role": "user", "content": user_content},
                    ],
                    temperature=0,
                    max_tokens=256,
                    **(dict(extra_body=_extra_body) if _extra_body else {}),
                )
                raw = (resp.choices[0].message.content or "").strip()
                if not raw:
                    print("[SUPERVISOR] empty task-complete response — assuming complete")
                    return {"complete": True, "reason": "empty response"}
                parsed = _try_parse_json(raw)
                if parsed is not None:
                    complete_val = parsed.get("complete")
                    reason = parsed.get("reason", "")
                    if isinstance(complete_val, bool):
                        return {"complete": complete_val, "reason": reason}
                    if str(complete_val).lower() in ("true", "yes", "1"):
                        return {"complete": True, "reason": reason}
                    if str(complete_val).lower() in ("false", "no", "0"):
                        return {"complete": False, "reason": reason}
                # Prose fallback
                _tail = raw[-300:].lower()
                if '"complete": false' in _tail or '"complete":false' in _tail:
                    return {"complete": False, "reason": raw[:200]}
                print(f"[SUPERVISOR] task-complete parse failed — assuming complete: {raw[:120]!r}")
                return {"complete": True, "reason": "parse-fallback"}
            except Exception as _e:
                _is_timeout = "timeout" in str(_e).lower() or "timed out" in str(_e).lower()
                _is_auth = getattr(_e, "status_code", None) in (401, 403)
                if _is_auth:
                    print(f"[SUPERVISOR] task-complete auth error — assuming complete: {_e}")
                    return {"complete": True, "reason": "error/auth"}
                if _tc_try < _tc_max_attempts and _is_timeout:
                    print(f"[SUPERVISOR] task-complete timeout on attempt {_tc_try} — retrying in 3s")
                    time.sleep(3)
                    continue
                print(f"[SUPERVISOR] task-complete error — assuming complete: {_e}")
                return {"complete": True, "reason": "error/parse-fallback"}
        return {"complete": True, "reason": "error/parse-fallback"}


