"""
Usage:
    cd Mobile-Agent-v3.5/mobile_use
    python run_gui_owl_1_5_for_mobile.py \
        --adb_path "Your ADB path" \
        --api_key "Your api key of vllm service" \
        --base_url "Your base url of vllm service" \
        --model "Your model name of vllm service" \
        --instruction "The instruction you want Mobile-Agent-v3.5 to complete" \
        --add_info "Some supplementary knowledge, can also be empty"
"""

import argparse
import json
import os
import shutil
import signal
import time
from datetime import datetime
from pathlib import Path

from PIL import Image

from packages import PACKAGES_NAME_DICT, NAME_PACKAGE_DICT
from utils import (
    AdbTools,
    annotate_screenshot,
    build_messages,
    resolve_app_name_via_llm,
    smart_resize,
    GUIOwlWrapper,
    summarise_ui_dump,
    SupervisorLLM,
)



def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Mobile-Agent-v3.5")
    parser.add_argument("--adb_path", type=str, required=True,
                        help="Path to the ADB binary.")
    parser.add_argument("--device", type=str, default=None,
                        help="ADB device serial (optional, for multi-device).")
    parser.add_argument("--api_key", type=str, required=True,
                        help="API key for the VLM service.")
    parser.add_argument("--base_url", type=str, required=True,
                        help="Base URL for the VLM service.")
    parser.add_argument("--model", type=str, required=True,
                        help="Model name for the VLM service.")
    parser.add_argument("--instruction", type=str, required=True,
                        help="Task instruction for the agent.")
    parser.add_argument("--add_info", type=str, default="",
                        help="Supplementary knowledge (can be empty).")
    parser.add_argument("--max_steps", type=int, default=50,
                        help="Maximum number of interaction steps.")
    parser.add_argument("--app_resolver_api_key", type=str, default=None,
                        help="API key for the app-resolver LLM (defaults to --api_key).")
    parser.add_argument("--app_resolver_base_url", type=str, default=None,
                        help="Base URL for the app-resolver LLM (defaults to --base_url).")
    parser.add_argument("--app_resolver_model", type=str, default="qwen-plus",
                        help="Model name for the app-resolver LLM.")
    # Supervisor LLM (optional) — a fast text model that validates each step
    # before execution. Disabled when --supervisor_model is not supplied.
    parser.add_argument("--supervisor_model", type=str, default="",
                        help="Model name for the supervisor LLM (e.g. 'MiniMax-Text-01'). "
                             "Leave empty to disable supervision.")
    parser.add_argument("--supervisor_api_key", type=str, default=None,
                        help="API key for the supervisor LLM (defaults to --api_key).")
    parser.add_argument("--supervisor_base_url", type=str, default=None,
                        help="Base URL for the supervisor LLM (defaults to --base_url).")
    return parser.parse_args()


def _repair_json(s):
    """Close unclosed arrays/objects in a truncated JSON string."""
    s = s.strip().rstrip(', \n\t')
    stack = []
    in_string = False
    escape = False
    for ch in s:
        if escape:
            escape = False
            continue
        if ch == '\\' and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in '{[':
            stack.append(ch)
        elif ch == '}' and stack and stack[-1] == '{':
            stack.pop()
        elif ch == ']' and stack and stack[-1] == '[':
            stack.pop()
    closing = {'[': ']', '{': '}'}
    for opener in reversed(stack):
        s += closing[opener]
    return s


def parse_action(output_text):
    """
    Extract the action dict from the model's output text.
    Expects a <tool_call> block containing JSON with nested 'arguments'.
    Falls back to JSON repair for truncated outputs.
    """
    if "<tool_call>" not in output_text:
        raise ValueError(f"Failed to parse action from model output: no <tool_call> block found")
    try:
        tool_call_block = output_text.split("<tool_call>\n")[1]
        json_str = tool_call_block.split("}}\n")[0] + "}}"
        return json.loads(json_str)
    except (IndexError, json.JSONDecodeError):
        pass

    # Fallback: try repairing truncated JSON
    try:
        tool_call_block = output_text.split("<tool_call>")[1].strip()
        repaired = _repair_json(tool_call_block)
        result = json.loads(repaired)
        # Validate minimum required fields
        if "arguments" in result and "action" in result.get("arguments", {}):
            return result
    except (IndexError, json.JSONDecodeError):
        pass

    raise ValueError(f"Failed to parse action from model output: {output_text!r}")


def rescale_coordinates(action_parameter, resized_width, resized_height):
    """
    Convert normalized (0-1000) coordinates to actual pixel coordinates
    based on the resized image dimensions.
    """
    for key in ("coordinate", "coordinate1", "coordinate2"):
        if key in action_parameter:
            action_parameter[key][0] = int(
                action_parameter[key][0] / 1000 * resized_width
            )
            action_parameter[key][1] = int(
                action_parameter[key][1] / 1000 * resized_height
            )
    return action_parameter


def handle_open_action(
    action_parameter,
    instruction,
    adb_tools,
    resolver_api_key,
    resolver_base_url,
    resolver_model,
):
    """
    Handle the 'open' action: resolve app name to package and launch it.

    Returns:
        True if the app was successfully opened,
        False if the app was not found (loop continues).
    """
    from packages import normalize_package_name
    app_name = action_parameter.get("text", "")
    # Normalize so '设置' matches the dict key 'settings' won't help, but
    # '设置' added to packages.py normalizes to '设置' and matches directly.
    app_name_key = normalize_package_name(app_name)

    # Include system packages (e.g. com.android.settings) not just third-party
    installed_packages = adb_tools.get_package_name(all_packages=True)
    display_name = app_name

    # First attempt: direct lookup
    package_candidates = NAME_PACKAGE_DICT.get(app_name_key, [])
    for pkg in package_candidates:
        if pkg in installed_packages:
            adb_tools.open_app(pkg)
            return True

    # Second attempt: resolve via LLM
    installed_app_names = []
    for pkg in installed_packages:
        if pkg in PACKAGES_NAME_DICT:
            installed_app_names.append(PACKAGES_NAME_DICT[pkg][0])

    resolved_name = resolve_app_name_via_llm(
        instruction,
        ", ".join(installed_app_names),
        api_key=resolver_api_key,
        base_url=resolver_base_url,
        model=resolver_model,
    )

    if resolved_name:
        display_name = resolved_name

    resolved_key = normalize_package_name(resolved_name) if resolved_name else ""
    resolved_packages = NAME_PACKAGE_DICT.get(resolved_key, [])
    for pkg in resolved_packages:
        if pkg in installed_packages:
            adb_tools.open_app(pkg)
            return True

    # App not found — do NOT block on input(), just log and let the loop continue
    print(f"[APP NOT FOUND] Could not resolve app: {display_name!r}. Continuing loop.")
    return False


def main():
    args = parse_args()

    # Initialize ADB
    adb_tools = AdbTools(adb_path=args.adb_path, device=args.device)

    # Prepare output directories — place INSIDE screenshots/ so they are
    # never left scattered in the skill's root directory.
    # Use .resolve() to guarantee an absolute path even if __file__ is relative.
    _skill_dir = Path(__file__).resolve().parent
    _screenshots_root = _skill_dir / "screenshots"
    instruction = args.instruction
    if args.add_info:
        instruction = f"{instruction} ({args.add_info})"

    _slug = instruction.replace(" ", "_")[:80]
    task_dir = str(_screenshots_root / _slug)
    anno_dir = task_dir + "_anno"

    def _cleanup():
        for _d in (task_dir, anno_dir):
            try:
                shutil.rmtree(_d, ignore_errors=True)
            except Exception:
                pass

    # Install SIGTERM handler so cleanup runs even when the parent kills us.
    def _sigterm_handler(signum, frame):
        _cleanup()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _sigterm_handler)

    for d in (task_dir, anno_dir):
        if os.path.exists(d):
            shutil.rmtree(d)
        os.makedirs(d)

    # App-resolver LLM config (falls back to main config)
    resolver_api_key = args.app_resolver_api_key or args.api_key
    resolver_base_url = args.app_resolver_base_url or args.base_url
    resolver_model = args.app_resolver_model

    # Supervisor LLM — CLI args take precedence; fall back to config.json
    # supervisor_provider section so it works even when called directly.
    _sup_model = getattr(args, "supervisor_model", "") or ""
    _sup_api_key = getattr(args, "supervisor_api_key", "") or ""
    _sup_base_url = getattr(args, "supervisor_base_url", "") or ""
    _sup_vision = False
    # Always read config.json: vision flag always comes from config;
    # model/key/url only filled in from config when not supplied via CLI.
    try:
        _cfg_path = Path(__file__).resolve().parent / "config.json"
        with _cfg_path.open(encoding="utf-8") as _f:
            _cfg = json.load(_f)
        _sp = _cfg.get("supervisor_provider", {})
        _sup_vision = bool(_sp.get("vision", False))
        if not _sup_model and _sp.get("model"):
            _sup_model = _sp["model"]
            _sup_api_key = _sup_api_key or _sp.get("api_key", "")
            _sup_base_url = _sup_base_url or _sp.get("base_url", "")
    except Exception:
        pass

    supervisor: SupervisorLLM | None = None
    if _sup_model:
        _eff_api_key = _sup_api_key or args.api_key
        _eff_base_url = _sup_base_url or args.base_url
        supervisor = SupervisorLLM(_eff_api_key, _eff_base_url, _sup_model, vision=_sup_vision)
        _vis_tag = " [vision=ON]" if _sup_vision else ""
        print(f"[SUPERVISOR] enabled — model: {_sup_model} @ {_eff_base_url}{_vis_tag}")
    else:
        print("[SUPERVISOR] disabled — set supervisor_provider.model in config.json to enable")

    history = []
    # Set to True once any physical action (click, swipe, type, etc.) is
    # executed.  Used to detect premature 'answer' refusals at step 0.
    any_real_action = False
    # Counts consecutive `wait` actions — used to detect a stuck agent.
    consecutive_waits = 0

    # Keywords in the model's action text that signal it is on the wrong screen.
    # When any of these appear the runner injects a Home-correction immediately.
    _WRONG_SCREEN_SIGNALS = [
        "调试界面", "调试工具", "debug interface", "developer",
        "PicoClaw", "picoclaw", "调试", "开发者",
    ]

    for step_id in range(args.max_steps):
        print(f"\n{'='*50}")
        print(f"STEP {step_id}")

        # ADB foreground-app check — get ground truth before screenshot + VLM.
        _fg_pkg = adb_tools.get_foreground_package()
        if _fg_pkg:
            _fg_names = PACKAGES_NAME_DICT.get(_fg_pkg, [])
            _fg_label = f"{_fg_pkg} ({', '.join(_fg_names)})" if _fg_names else _fg_pkg
            print(f"[Foreground] {_fg_label}")
        else:
            _fg_label = ""
        print(f"{'='*50}")

        # 1. Capture screenshot
        _ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:19]
        screenshot_path = os.path.join(task_dir, f"screenshot_{step_id}_{_ts}.png")
        if not adb_tools.get_screenshot(screenshot_path):
            print("[ERROR] Failed to capture screenshot. Retrying...")
            time.sleep(1)
            continue

        # 1b. UI accessibility dump — gives the VLM exact element bounds and labels.
        # Falls back gracefully (empty string) for WebView / game-engine screens.
        _ui_xml = adb_tools.get_ui_dump()
        _ui_summary = summarise_ui_dump(_ui_xml)
        if _ui_summary:
            _node_count = _ui_summary.count("\n")
            print(f"[UI dump] {_node_count} interactive elements found")
        else:
            print("[UI dump] No accessibility data (WebView or ADB error — screenshot only)")

        # 2. Build messages and call the VLM
        messages = build_messages(
            screenshot_path, instruction, history, args.model,
            foreground_pkg=_fg_label,
            ui_summary=_ui_summary,
        )

        vllm = GUIOwlWrapper(args.api_key, args.base_url, args.model)
        output_text, _, _ = vllm.predict_mm(messages)

        print(f"[MODEL OUTPUT]\n{output_text}")

        # 3a. Wrong-screen early exit: if the model text explicitly mentions a
        # debug/developer/wrong-app screen, press Home and restart rather than
        # blindly executing the suggested action.
        _action_text = output_text.split("<tool_call>")[0]
        if any(sig in _action_text for sig in _WRONG_SCREEN_SIGNALS):
            print(
                "[WARN] Model output indicates wrong/debug screen — "
                "injecting Home correction."
            )
            _correction = (
                "Action: I am on the wrong screen. Pressing Home to navigate "
                "to the correct app.\n"
                "<tool_call>\n"
                '{"name": "mobile_use", "arguments": '
                '{"action": "system_button", "button": "Home"}}\n'
                "</tool_call>"
            )
            history.append({"output": _correction, "image": screenshot_path})
            adb_tools.home()
            consecutive_waits = 0
            time.sleep(2)
            continue

        # 3. Parse the action
        try:
            action = parse_action(output_text)
        except ValueError as e:
            print(f"[WARN] Could not parse action: {e} — skipping step")
            history.append({"output": output_text, "image": screenshot_path})
            time.sleep(1)
            continue
        action_parameter = action["arguments"]

        # 3b. Rule-based guard — catches hallucination/wrong-action without any LLM call.
        # Runs unconditionally so it works even when supervisor API is down.
        _rb_override: dict | None = None
        _proposed_action = action_parameter.get("action", "")

        # (a) Intent/action mismatch: VLM says "Home/主页" but tool_call is a click
        _home_keywords = ("home", "主页", "主屏幕", "返回桌面", "按home", "按主页")
        if _proposed_action == "click" and any(kw in _action_text.lower() for kw in _home_keywords):
            _rb_override = {"action": "system_button", "button": "Home"}
            print("[RULE] Intent/action mismatch: description says Home but action is click — correcting to Home")

        # (b) Premature answer with no real action taken
        elif _proposed_action == "answer" and not any_real_action:
            _rb_override = {"action": "system_button", "button": "Home"}
            print("[RULE] Premature answer before any real action — pressing Home first")

        if _rb_override:
            _rb_note = (
                f"Action: [RULE OVERRIDE] {_rb_override}\n"
                f"<tool_call>\n{json.dumps({'name': 'mobile_use', 'arguments': _rb_override}, ensure_ascii=False)}\n</tool_call>"
            )
            history.append({"output": _rb_note, "image": screenshot_path})
            if _rb_override.get("action") == "system_button" and _rb_override.get("button") == "Home":
                adb_tools.home()
                consecutive_waits = 0
                time.sleep(2)
                continue
            action_parameter = _rb_override
            action["arguments"] = _rb_override

        # 3c. Supervisor validation — fast text LLM checks intent vs. tool_call.
        # Passes UI dump so it can verify answer claims against actual screen content.
        # Only active when supervisor is configured.
        if supervisor is not None and _rb_override is None:
            _sup_verdict = supervisor.validate(
                task=instruction,
                fg_label=_fg_label,
                action_text=_action_text,
                tool_call_dict=action,
                ui_summary=_ui_summary,
                screenshot_path=screenshot_path,
            )
            if _sup_verdict.get("verdict") == "override":
                _reason = _sup_verdict.get("reason", "")
                print(f"[SUPERVISOR] overriding action — {_reason}")
                _override_tc = _sup_verdict.get("tool_call", {})
                if _override_tc and "arguments" in _override_tc:
                    action = _override_tc
                    action_parameter = action["arguments"]
                    _sup_note = (
                        f"Action: [SUPERVISOR OVERRIDE] {_reason}\n"
                        f"<tool_call>\n{json.dumps(_override_tc, ensure_ascii=False)}\n</tool_call>"
                    )
                    history.append({"output": _sup_note, "image": screenshot_path})
                    if action_parameter.get("action") == "system_button" and action_parameter.get("button") == "Home":
                        adb_tools.home()
                        consecutive_waits = 0
                        time.sleep(2)
                        continue
                    elif action_parameter.get("action") == "wait":
                        # Supervisor wants the agent to re-examine the screen
                        print("[SUPERVISOR] forcing re-examine (wait) before answer")
                        time.sleep(2)
                        continue
                else:
                    print("[SUPERVISOR] override had no valid tool_call — approving original")
            else:
                print("[SUPERVISOR] approved")

        # 4. Rescale coordinates from 1000x1000 to actual resolution
        img = Image.open(screenshot_path)
        resized_h, resized_w = smart_resize(
            img.height, img.width,
            factor=16,
            min_pixels=3136,
            max_pixels=1003520 * 200,
        )
        action_parameter = rescale_coordinates(action_parameter, resized_w, resized_h)

        # 5. Execute the action
        action_type = action_parameter["action"]

        if action_type == "click":
            any_real_action = True
            consecutive_waits = 0
            adb_tools.click(
                action_parameter["coordinate"][0],
                action_parameter["coordinate"][1],
            )

        elif action_type == "long_press":
            any_real_action = True
            consecutive_waits = 0
            adb_tools.long_press(
                action_parameter["coordinate"][0],
                action_parameter["coordinate"][1],
            )

        elif action_type == "type":
            any_real_action = True
            consecutive_waits = 0
            adb_tools.type(action_parameter["text"])

        elif action_type in ("scroll", "swipe"):
            any_real_action = True
            consecutive_waits = 0
            adb_tools.slide(
                action_parameter["coordinate"][0],
                action_parameter["coordinate"][1],
                action_parameter["coordinate2"][0],
                action_parameter["coordinate2"][1],
            )

        elif action_type == "system_button":
            any_real_action = True
            consecutive_waits = 0
            button = action_parameter["button"]
            if button == "Back":
                adb_tools.back()
            elif button == "Home":
                adb_tools.home()

        elif action_type == "wait":
            wait_time = action_parameter.get("time", 2)
            consecutive_waits += 1
            if consecutive_waits >= 2:
                print(
                    f"[WARN] {consecutive_waits} consecutive wait actions — "
                    "agent appears stuck on wrong screen. Injecting Home correction."
                )
                _correction = (
                    "Action: I have been waiting too long on the wrong screen. "
                    "Pressing Home to navigate to the correct app.\n"
                    "<tool_call>\n"
                    '{"name": "mobile_use", "arguments": '
                    '{"action": "system_button", "button": "Home"}}\n'
                    "</tool_call>"
                )
                history.append({"output": _correction, "image": screenshot_path})
                adb_tools.home()
                consecutive_waits = 0
                time.sleep(2)
                continue
            time.sleep(wait_time)

        elif action_type == "terminate":
            status = action_parameter.get("status", "unknown")
            print(f"[TERMINATED] Status: {status}")
            break

        elif action_type == "open":
            any_real_action = True
            consecutive_waits = 0
            opened = handle_open_action(
                action_parameter,
                instruction,
                adb_tools,
                resolver_api_key,
                resolver_base_url,
                resolver_model,
            )
            if not opened:
                continue

        elif action_type == "answer":
            conclusion = output_text.split("<tool_call>")[0].strip()
            # Guard: if the model gives 'answer' before performing any real
            # actions it is refusing rather than completing the task.
            # Inject a self-correction into history, press Home, and continue.
            if not any_real_action:
                print(
                    f"[WARN] Model gave 'answer' at step {step_id} with no prior "
                    "actions — treating as premature refusal, injecting correction."
                )
                correction = (
                    "Action: I made an error — I must not give up before trying. "
                    "I will navigate to the required app from the current screen.\n"
                    "<tool_call>\n"
                    '{"name": "mobile_use", "arguments": {"action": "system_button", "button": "Home"}}\n'
                    "</tool_call>"
                )
                history.append({"output": correction, "image": screenshot_path})
                adb_tools.home()
                time.sleep(2)
                continue
            print(f"[ANSWER] {conclusion}")
            print("[TERMINATED] Task completed.")
            break

        elif action_type in ("call_user", "calluser", "interact"):
            user_prompt = action_parameter.get("text", "the required action")
            print(f"[ACTION REQUIRED] Please complete: {user_prompt}")
            print("[INFO] User action noted. Resuming...")

        else:
            print(f"[WARN] Unsupported action type: {action_type}")

        # 6. Record history and annotate screenshot
        history.append({"output": output_text, "image": screenshot_path})
        annotate_screenshot(
            screenshot_path,
            action_parameter,
            os.path.join(anno_dir, f"screenshot_anno_{step_id}.png"),
        )
        time.sleep(2)

    print("\n[DONE] Agent execution finished.")

    # Clean up screenshot directories after task ends
    _cleanup()
    print("[CLEANUP] Screenshot directories removed.")


if __name__ == "__main__":
    main()
