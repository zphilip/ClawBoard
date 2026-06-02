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

from packages import PACKAGES_NAME_DICT, NAME_PACKAGE_DICT, normalize_package_name
from utils import (
    AdbTools,
    annotate_screenshot,
    build_messages,
    ERROR_CALLING_LLM,
    resolve_app_name_via_llm,
    smart_resize,
    GUIOwlWrapper,
    summarise_ui_dump,
    SupervisorLLM,
)


PRIMARY_RECOVERY_COOLDOWN_SECONDS = 600  # 10 minutes


def _ts() -> str:
    """Human-friendly timestamp for log lines."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log_t(msg: str) -> None:
    """Print a timestamped log line."""
    print(f"[{_ts()}] {msg}")



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
    parser.add_argument("--max-context-size", type=int, default=None,
                        help="Override VLM context window size (tokens). "
                             "Activates compact mode when ≤2048.")
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
    _raw_coords: dict[str, list[int]] = {}
    for key in ("coordinate", "coordinate1", "coordinate2"):
        if key in action_parameter:
            _raw_coords[key] = list(action_parameter[key])
            action_parameter[key][0] = int(
                action_parameter[key][0] / 1000 * resized_width
            )
            action_parameter[key][1] = int(
                action_parameter[key][1] / 1000 * resized_height
            )
    if _raw_coords:
        print(
            f"[COORD DEBUG] raw={_raw_coords} -> scaled={{{', '.join(f'{k}: {action_parameter[k]}' for k in _raw_coords)}}} "
            f"(resized={resized_width}x{resized_height})"
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
        print("[TERMINATION REASON] sigterm_from_parent")
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
    _sup_reasoning_split = False
    # max_context_size for the VLM: CLI arg takes priority, then config.json.
    _vlm_max_ctx = getattr(args, 'max_context_size', None)
    # Fallback provider credentials (populated from config.json when present).
    _fb_base_url: str = ""
    _fb_api_key: str = ""
    _fb_model: str = ""
    _fb_max_ctx: int | None = None

    # Always read config.json: vision flag always comes from config;
    # model/key/url only filled in from config when not supplied via CLI.
    try:
        _cfg_path = Path(__file__).resolve().parent / "config.json"
        with _cfg_path.open(encoding="utf-8") as _f:
            _cfg = json.load(_f)
        _sp = _cfg.get("supervisor_provider", {})
        _sup_vision = bool(_sp.get("vision", False))
        _sup_reasoning_split = bool(_sp.get("reasoning_split", False))
        if not _sup_model and _sp.get("model"):
            _sup_model = _sp["model"]
            _sup_api_key = _sup_api_key or _sp.get("api_key", "")
            _sup_base_url = _sup_base_url or _sp.get("base_url", "")
        # Determine which provider the runner was launched with and pull its
        # max_context_size so GUIOwlWrapper can trim content proactively.
        # Only override if CLI did not supply --max-context-size.
        if _vlm_max_ctx is None:
            for _pkey in ("provider", "fallback_provider"):
                _prov = _cfg.get(_pkey, {})
                if _prov.get("base_url") == args.base_url or _prov.get("model") == args.model:
                    _vlm_max_ctx = _prov.get("max_context_size") or None
                    break
        # Read fallback provider for use when the primary provider is unavailable.
        _fp = _cfg.get("fallback_provider", {})
        _fb_base_url = _fp.get("base_url", "")
        _fb_api_key = _fp.get("api_key", "")
        _fb_model = _fp.get("model", "")
        _fb_max_ctx = _fp.get("max_context_size") or None
        if _fb_model:
            print(f"[VLM] Fallback provider configured: {_fb_model} @ {_fb_base_url}")
    except Exception:
        pass

    supervisor: SupervisorLLM | None = None
    if _sup_model:
        _eff_api_key = _sup_api_key or args.api_key
        _eff_base_url = _sup_base_url or args.base_url
        supervisor = SupervisorLLM(
            _eff_api_key, _eff_base_url, _sup_model,
            vision=_sup_vision, reasoning_split=_sup_reasoning_split,
        )
        _vis_tag = " [vision=ON]" if _sup_vision else ""
        _rs_tag = " [reasoning_split=ON]" if _sup_reasoning_split else ""
        print(f"[SUPERVISOR] enabled — model: {_sup_model} @ {_eff_base_url}{_vis_tag}{_rs_tag}")
    else:
        print("[SUPERVISOR] disabled — set supervisor_provider.model in config.json to enable")

    # Compact mode: use stripped-down system prompt + fewer UI nodes when the
    # VLM's context window is very small (≤2048 tokens).
    _compact_mode = bool(_vlm_max_ctx and _vlm_max_ctx <= 2048)
    _ui_max_nodes = 20 if _compact_mode else 60
    if _compact_mode:
        print(f"[VLM] Compact mode ON (max_context_size={_vlm_max_ctx}): using compact system prompt, UI dump limited to {_ui_max_nodes} nodes")

    history = []
    # Set to True once any physical action (click, swipe, type, etc.) is
    # executed.  Used to detect premature 'answer' refusals at step 0.
    any_real_action = False
    # Counts consecutive `wait` actions — used to detect a stuck agent.
    consecutive_waits = 0
    # If the primary provider fails, suppress primary attempts for this
    # cooldown window and use fallback directly for faster recovery.
    _primary_cooldown_until = 0.0
    _primary_cooldown_reason = ""
    # Rolling window of the last 5 click coordinates.
    # Used to detect a stuck loop (same coordinate tapped 3+ times in a row).
    _recent_click_coords: list[tuple] = []

    # Keywords in the model's action text that signal it is on the wrong screen.
    # When any of these appear the runner injects a Home-correction immediately.
    _WRONG_SCREEN_SIGNALS = [
        "调试界面", "调试工具", "debug interface", "developer",
        "PicoClaw", "picoclaw", "调试", "开发者",
    ]

    # Cache installed app display-names once, then reuse them for both the
    # supervisor and the VLM prompt constraints.
    _cached_sup_app_names: list[str] = []
    _cached_inst_app_names: list[str] = []
    _target_app_hint: str = ""
    _target_pkg_hint: str = ""
    _installed_pkg_set: set[str] = set()
    _launcher_pkgs = {
        "net.oneplus.launcher",
        "com.android.launcher",
        "com.android.launcher3",
        "com.miui.home",
        "com.huawei.android.launcher",
        "com.oppo.launcher",
        "com.vivo.launcher",
        "com.samsung.android.launcher",
    }
    try:
        _inst_pkgs = adb_tools.get_package_name(all_packages=True)
        _installed_pkg_set = set(_inst_pkgs)
        _cached_inst_app_names = [
            PACKAGES_NAME_DICT[p][0] for p in _inst_pkgs if p in PACKAGES_NAME_DICT
        ]
        _seen_names: set[str] = set()
        _deduped_inst_names: list[str] = []
        for _name in _cached_inst_app_names:
            if _name in _seen_names:
                continue
            _seen_names.add(_name)
            _deduped_inst_names.append(_name)
        _cached_inst_app_names = _deduped_inst_names
        _cached_sup_app_names = list(_cached_inst_app_names)
        _norm_instruction = instruction.lower().replace(" ", "").replace("-", "")
        _candidate_names = sorted(
            _cached_inst_app_names,
            key=lambda _n: len(_n),
            reverse=True,
        )
        for _name in _candidate_names:
            _norm_name = _name.lower().replace(" ", "").replace("-", "")
            if _norm_name and _norm_name in _norm_instruction:
                _target_app_hint = _name
                break
        if _target_app_hint:
            _cand_pkgs = NAME_PACKAGE_DICT.get(normalize_package_name(_target_app_hint), [])
            for _pkg in _cand_pkgs:
                if _pkg in _installed_pkg_set:
                    _target_pkg_hint = _pkg
                    break
            if _target_pkg_hint:
                print(f"[TARGET APP] instruction target={_target_app_hint} package={_target_pkg_hint}")
            else:
                print(f"[TARGET APP] instruction target={_target_app_hint} (package unresolved)")
    except Exception:
        pass

    termination_reason = "unknown"
    for step_id in range(args.max_steps):
        _step_t0 = time.time()
        _step_metrics: dict[str, float] = {}

        def _emit_step_summary(_outcome: str) -> None:
            _parts = [f"step={step_id}", f"outcome={_outcome}", f"total={time.time() - _step_t0:.2f}s"]
            for _k in (
                "screenshot", "ui_dump", "vlm_primary", "vlm_fallback",
                "supervisor", "action",
            ):
                if _k in _step_metrics:
                    _parts.append(f"{_k}={_step_metrics[_k]:.2f}s")
            _log_t("[STEP SUMMARY] " + " | ".join(_parts))

        print(f"\n{'='*50}")
        print(f"STEP {step_id}")
        print(f"[STEP DEBUG] history_len={len(history)} any_real_action={any_real_action} max_steps={args.max_steps}")
        _log_t(f"[STEP START] step={step_id}")

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
        _t_screenshot = time.time()
        _ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:19]
        screenshot_path = os.path.join(task_dir, f"screenshot_{step_id}_{_ts}.png")
        if not adb_tools.get_screenshot(screenshot_path):
            print("[ERROR] Failed to capture screenshot. Retrying...")
            _emit_step_summary("screenshot_failed")
            time.sleep(1)
            continue
        _step_metrics["screenshot"] = time.time() - _t_screenshot
        _log_t(f"[TIMING] screenshot_capture={_step_metrics['screenshot']:.2f}s")

        # 1b. UI accessibility dump — gives the VLM exact element bounds and labels.
        # Falls back gracefully (empty string) for WebView / game-engine screens.
        _t_ui_dump = time.time()
        _ui_xml = adb_tools.get_ui_dump()
        _ui_summary = summarise_ui_dump(_ui_xml, max_nodes=_ui_max_nodes)
        if _ui_summary:
            _node_count = _ui_summary.count("\n")
            print(f"[UI dump] {_node_count} interactive elements found")
        else:
            print("[UI dump] No accessibility data (WebView or ADB error — screenshot only)")
        _step_metrics["ui_dump"] = time.time() - _t_ui_dump
        _log_t(f"[TIMING] ui_dump={_step_metrics['ui_dump']:.2f}s")

        # 2. Build messages and call the VLM
        messages = build_messages(
            screenshot_path, instruction, history, args.model,
            foreground_pkg=_fg_label,
            ui_summary=_ui_summary,
            installed_apps_hint=", ".join(_cached_inst_app_names[:80]),
            target_app_hint=_target_app_hint,
            compact=_compact_mode,
        )

        vllm = GUIOwlWrapper(
            args.api_key,
            args.base_url,
            args.model,
            max_retry=1,
            max_context_size=_vlm_max_ctx,
        )
        _primary_attempts = 2
        output_text = ERROR_CALLING_LLM
        _t_primary = time.time()
        _now = time.time()
        _primary_attempted = False
        _primary_failed_after_attempt = False
        if _now < _primary_cooldown_until:
            _remaining = int(_primary_cooldown_until - _now)
            print(
                f"[VLM] primary provider in cooldown ({_remaining}s left, reason={_primary_cooldown_reason}) — skipping primary"
            )
        else:
            _primary_attempted = True
            for _p_try in range(1, _primary_attempts + 1):
                print(f"[VLM] primary attempt {_p_try}/{_primary_attempts}")
                output_text, _, _ = vllm.predict_mm(messages)
                if output_text != ERROR_CALLING_LLM:
                    break
                if _p_try < _primary_attempts:
                    print("[VLM] primary attempt failed — retrying in 2s")
                    time.sleep(2)
            _primary_failed_after_attempt = (output_text == ERROR_CALLING_LLM)
        _step_metrics["vlm_primary"] = time.time() - _t_primary
        _log_t(f"[TIMING] vlm_primary={_step_metrics['vlm_primary']:.2f}s")
        _provider_used = f"primary:{args.model} @ {args.base_url}"

        # If primary provider failed, try the fallback (e.g. local gui-owl).
        if output_text == ERROR_CALLING_LLM and _fb_model:
            if _primary_attempted and _primary_failed_after_attempt:
                _primary_cooldown_until = time.time() + PRIMARY_RECOVERY_COOLDOWN_SECONDS
                _primary_cooldown_reason = "primary_error"
                _log_t(
                    f"[VLM] entering primary cooldown for {PRIMARY_RECOVERY_COOLDOWN_SECONDS}s"
                )
            print(f"[VLM] Primary provider failed — switching to fallback: {_fb_model}")
            _fb_compact = bool(_fb_max_ctx and _fb_max_ctx <= 2048)
            if _fb_compact and not _compact_mode:
                # Rebuild with compact prompt.  Drop ui_summary (saves ~150+ tokens)
                # and limit history to 1 step — the 2048-token model can barely fit
                # one history image alongside the system prompt + current screenshot.
                _fb_messages = build_messages(
                    screenshot_path, instruction, history, _fb_model,
                    foreground_pkg=_fg_label,
                    ui_summary="",   # omit — saves ~150 tokens for the small model
                    installed_apps_hint=", ".join(_cached_inst_app_names[:80]),
                    target_app_hint=_target_app_hint,
                    compact=True,
                    history_n=1,    # at most one history image in 2048-token context
                )
            else:
                _fb_messages = messages
            _fb_vllm = GUIOwlWrapper(
                _fb_api_key,
                _fb_base_url,
                _fb_model,
                max_retry=1,
                max_context_size=_fb_max_ctx,
            )
            _t_fallback = time.time()
            _fb_attempts = 3
            for _fb_try in range(1, _fb_attempts + 1):
                print(f"[VLM] fallback attempt {_fb_try}/{_fb_attempts}")
                output_text, _, _ = _fb_vllm.predict_mm(_fb_messages)
                if output_text != ERROR_CALLING_LLM:
                    break
                if _fb_try < _fb_attempts:
                    print("[VLM] fallback attempt failed — retrying in 2s")
                    time.sleep(2)
            if output_text == ERROR_CALLING_LLM:
                print("[VLM] fallback exhausted all retries")
            _step_metrics["vlm_fallback"] = time.time() - _t_fallback
            _log_t(f"[TIMING] vlm_fallback={_step_metrics['vlm_fallback']:.2f}s")
            _provider_used = f"fallback:{_fb_model} @ {_fb_base_url}"
        elif output_text == ERROR_CALLING_LLM:
            print("[VLM] primary failed and no fallback provider is configured")

        if output_text == ERROR_CALLING_LLM:
            print(f"[VLM] provider used: {_provider_used} (ERROR_CALLING_LLM)")
        else:
            print(f"[VLM] provider used: {_provider_used}")

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
            _emit_step_summary("wrong_screen_home_recovery")
            time.sleep(2)
            continue

        # 3. Parse the action
        try:
            action = parse_action(output_text)
        except ValueError as e:
            print(f"[WARN] Could not parse action: {e} — skipping step")
            history.append({"output": output_text, "image": screenshot_path})
            _emit_step_summary("parse_failed")
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

        # (c) App disambiguation/recovery guard:
        # If the task has a clear target app package (e.g., QQ音乐), do not rely
        # on ambiguous launcher icon taps (e.g., QQ vs QQ音乐). Force an exact
        # open target action when on launcher or when stuck in a wrong foreground app.
        elif _target_pkg_hint and _target_app_hint:
            _fg_pkg_clean = (_fg_pkg or "").strip()
            _on_launcher = _fg_pkg_clean in _launcher_pkgs
            _in_wrong_app = bool(_fg_pkg_clean and not _on_launcher and _fg_pkg_clean != _target_pkg_hint)

            if _on_launcher and _proposed_action == "click":
                _rb_override = {"action": "open", "text": _target_app_hint}
                print(
                    f"[RULE] On launcher with target app {_target_app_hint} — "
                    "replacing ambiguous click with exact open"
                )
            elif _in_wrong_app and _proposed_action in (
                "wait", "click", "type", "scroll", "swipe", "long_press"
            ):
                _rb_override = {"action": "open", "text": _target_app_hint}
                print(
                    f"[RULE] Wrong foreground app {_fg_pkg_clean}; "
                    f"forcing open target {_target_app_hint} ({_target_pkg_hint})"
                )

        if _rb_override:
            if supervisor is not None:
                print("[SUPERVISOR] skipped (rule override already applied)")
            _rb_note = (
                f"Action: [RULE OVERRIDE] {_rb_override}\n"
                f"<tool_call>\n{json.dumps({'name': 'mobile_use', 'arguments': _rb_override}, ensure_ascii=False)}\n</tool_call>"
            )
            history.append({"output": _rb_note, "image": screenshot_path})
            if _rb_override.get("action") == "system_button" and _rb_override.get("button") == "Home":
                print("[ACTION EXEC] RULE override -> Home (start)")
                if not adb_tools.home():
                    print("[ACTION EXEC] RULE override -> Home failed")
                else:
                    print("[ACTION EXEC] RULE override -> Home done")
                consecutive_waits = 0
                _emit_step_summary("rule_override_home")
                time.sleep(2)
                continue
            action_parameter = _rb_override
            action["arguments"] = _rb_override

        # 3c. Supervisor validation — fast text LLM checks intent vs. tool_call.
        # Passes UI dump so it can verify answer claims against actual screen content.
        # Only active when supervisor is configured.
        if supervisor is not None and _rb_override is None:
            _sup_apps_hint = ""
            if _proposed_action == "open" and _cached_sup_app_names:
                _sup_apps_hint = ", ".join(_cached_sup_app_names[:60])
            _t_supervisor = time.time()
            try:
                _sup_verdict = supervisor.validate(
                    task=instruction,
                    fg_label=_fg_label,
                    action_text=_action_text,
                    tool_call_dict=action,
                    ui_summary=_ui_summary,
                    screenshot_path=screenshot_path,
                    installed_apps_hint=_sup_apps_hint,
                )
            except Exception as _sup_err:
                print(f"[SUPERVISOR] error during validation ({_sup_err!r}) — approving by default")
                _sup_verdict = {"verdict": "approve"}
            _step_metrics["supervisor"] = time.time() - _t_supervisor
            _log_t(f"[TIMING] supervisor_validate={_step_metrics['supervisor']:.2f}s")
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
                        print("[ACTION EXEC] SUPERVISOR override -> Home (start)")
                        if not adb_tools.home():
                            print("[ACTION EXEC] SUPERVISOR override -> Home failed")
                        else:
                            print("[ACTION EXEC] SUPERVISOR override -> Home done")
                        consecutive_waits = 0
                        _emit_step_summary("supervisor_override_home")
                        time.sleep(2)
                        continue
                    elif action_parameter.get("action") == "wait":
                        # Supervisor wants the agent to re-examine the screen
                        print("[SUPERVISOR] forcing re-examine (wait) before answer")
                        _emit_step_summary("supervisor_forced_wait")
                        time.sleep(2)
                        continue
                else:
                    print("[SUPERVISOR] override had no valid tool_call — injecting Home correction")
                    print("[ACTION EXEC] SUPERVISOR fallback -> Home (start)")
                    if not adb_tools.home():
                        print("[ACTION EXEC] SUPERVISOR fallback -> Home failed")
                    else:
                        print("[ACTION EXEC] SUPERVISOR fallback -> Home done")
                    _sup_note = (
                        "Action: [SUPERVISOR OVERRIDE] no valid tool_call — pressing Home\n"
                        "<tool_call>\n"
                        + json.dumps({"name": "mobile_use", "arguments": {"action": "system_button", "button": "Home"}}, ensure_ascii=False)
                        + "\n</tool_call>"
                    )
                    history.append({"output": _sup_note, "image": screenshot_path})
                    consecutive_waits = 0
                    _emit_step_summary("supervisor_fallback_home")
                    time.sleep(2)
                    continue
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
            _t_action = time.time()
            any_real_action = True
            consecutive_waits = 0
            _coord = (
                action_parameter["coordinate"][0],
                action_parameter["coordinate"][1],
            )
            _recent_click_coords.append(_coord)
            if len(_recent_click_coords) > 5:
                _recent_click_coords.pop(0)
            # Detect 3+ consecutive taps on the exact same coordinate → stuck loop
            if len(_recent_click_coords) >= 3 and len(set(_recent_click_coords[-3:])) == 1:
                print(
                    f"[STUCK] coordinate {_coord} tapped 3x in a row — "
                    "pressing Back to escape stuck state"
                )
                _stuck_note = (
                    "Action: [RECOVERY] I have tapped the same coordinate 3 times "
                    "with no change. Pressing Back to escape the stuck state.\n"
                    "<tool_call>\n"
                    '{"name": "mobile_use", "arguments": {"action": "system_button", "button": "Back"}}\n'
                    "</tool_call>"
                )
                history.append({"output": _stuck_note, "image": screenshot_path})
                adb_tools.back()
                _recent_click_coords.clear()
                time.sleep(1.5)
                continue
            print(f"[ACTION EXEC] click {_coord} (start)")
            if not adb_tools.click(_coord[0], _coord[1]):
                print(f"[ACTION EXEC] click {_coord} failed")
            else:
                print(f"[ACTION EXEC] click {_coord} done")
            _step_metrics["action"] = time.time() - _t_action
            _log_t(f"[TIMING] action_click={_step_metrics['action']:.2f}s")

        elif action_type == "long_press":
            _t_action = time.time()
            any_real_action = True
            consecutive_waits = 0
            print("[ACTION EXEC] long_press (start)")
            _ok = adb_tools.long_press(
                action_parameter["coordinate"][0],
                action_parameter["coordinate"][1],
            )
            print("[ACTION EXEC] long_press done" if _ok else "[ACTION EXEC] long_press failed")
            _step_metrics["action"] = time.time() - _t_action
            _log_t(f"[TIMING] action_long_press={_step_metrics['action']:.2f}s")

        elif action_type == "type":
            _t_action = time.time()
            any_real_action = True
            consecutive_waits = 0
            _text = str(action_parameter.get("text", ""))
            print(f"[ACTION EXEC] type {_text!r} (start)")
            _ok = adb_tools.type_with_verification(_text, retries=2)
            if _ok:
                print("[ACTION EXEC] type done (verified)")
            else:
                print("[ACTION EXEC] type failed_or_unverified")
                print("[WARN] Input command may have succeeded but text was not observed in UI")
            _step_metrics["action"] = time.time() - _t_action
            _log_t(f"[TIMING] action_type={_step_metrics['action']:.2f}s")

        elif action_type in ("scroll", "swipe"):
            _t_action = time.time()
            any_real_action = True
            consecutive_waits = 0
            print("[ACTION EXEC] swipe/scroll (start)")
            _ok = adb_tools.slide(
                action_parameter["coordinate"][0],
                action_parameter["coordinate"][1],
                action_parameter["coordinate2"][0],
                action_parameter["coordinate2"][1],
            )
            print("[ACTION EXEC] swipe/scroll done" if _ok else "[ACTION EXEC] swipe/scroll failed")
            _step_metrics["action"] = time.time() - _t_action
            _log_t(f"[TIMING] action_swipe={_step_metrics['action']:.2f}s")

        elif action_type == "system_button":
            _t_action = time.time()
            any_real_action = True
            consecutive_waits = 0
            _recent_click_coords.clear()  # navigation resets the click-loop window
            button = action_parameter["button"]
            if button == "Back":
                print("[ACTION EXEC] system_button Back (start)")
                if not adb_tools.back():
                    print("[ACTION EXEC] system_button Back failed")
                else:
                    print("[ACTION EXEC] system_button Back done")
            elif button == "Home":
                print("[ACTION EXEC] system_button Home (start)")
                if not adb_tools.home():
                    print("[ACTION EXEC] system_button Home failed")
                else:
                    print("[ACTION EXEC] system_button Home done")
            _step_metrics["action"] = time.time() - _t_action
            _log_t(f"[TIMING] action_system_button={_step_metrics['action']:.2f}s")

        elif action_type == "wait":
            _t_action = time.time()
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
                print("[ACTION EXEC] wait-recovery -> Home (start)")
                if not adb_tools.home():
                    print("[ACTION EXEC] wait-recovery -> Home failed")
                else:
                    print("[ACTION EXEC] wait-recovery -> Home done")
                consecutive_waits = 0
                _step_metrics["action"] = time.time() - _t_action
                _emit_step_summary("wait_recovery_home")
                time.sleep(2)
                continue
            time.sleep(wait_time)
            _step_metrics["action"] = time.time() - _t_action
            _log_t(f"[TIMING] action_wait={_step_metrics['action']:.2f}s")

        elif action_type == "terminate":
            status = action_parameter.get("status", "unknown")
            print(f"[TERMINATED] Status: {status}")
            termination_reason = f"terminate_action_status={status}"
            _emit_step_summary("terminate_action")
            break

        elif action_type == "open":
            _t_action = time.time()
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
            _step_metrics["action"] = time.time() - _t_action
            _log_t(f"[TIMING] action_open={_step_metrics['action']:.2f}s")
            if not opened:
                _emit_step_summary("open_not_found")
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
                print("[ACTION EXEC] premature-answer recovery -> Home (start)")
                if not adb_tools.home():
                    print("[ACTION EXEC] premature-answer recovery -> Home failed")
                else:
                    print("[ACTION EXEC] premature-answer recovery -> Home done")
                _emit_step_summary("premature_answer_home")
                time.sleep(2)
                continue
            print(f"[ANSWER] {conclusion}")
            # Ask the supervisor whether the task is actually done before
            # accepting the agent's self-reported completion.
            if supervisor is not None:
                try:
                    _completion = supervisor.is_task_complete(
                        task=instruction,
                        fg_label=_fg_label,
                        ui_summary=_ui_summary,
                        history=history,
                        conclusion=conclusion,
                        screenshot_path=str(screenshot_path),
                    )
                except Exception as _comp_err:
                    print(f"[SUPERVISOR] task-complete check error ({_comp_err!r}) — accepting completion")
                    _completion = {"complete": True, "reason": "error"}
                if not _completion.get("complete", True):
                    _missing = _completion.get("reason", "task not yet complete")
                    print(f"[SUPERVISOR] task NOT complete — {_missing}")
                    correction = (
                        f"Action: I made an error — the task is not finished yet. "
                        f"{_missing} I will continue from the current screen.\n"
                        "<tool_call>\n"
                        '{"name": "mobile_use", "arguments": {"action": "wait", "time": 1}}\n'
                        "</tool_call>"
                    )
                    history.append({"output": correction, "image": screenshot_path})
                    _emit_step_summary("answer_rejected_by_supervisor")
                    continue
                print("[SUPERVISOR] task confirmed complete")
            else:
                print("[SUPERVISOR] completion check skipped (supervisor disabled)")
            print("[TERMINATED] Task completed.")
            termination_reason = "answer_confirmed_complete"
            _emit_step_summary("answer_confirmed_complete")
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
        _emit_step_summary("completed")
        _log_t(f"[STEP END] step={step_id} total={time.time() - _step_t0:.2f}s")
        time.sleep(2)
    else:
        termination_reason = f"max_steps_reached ({args.max_steps})"
        print(f"[TERMINATED] Reached max_steps={args.max_steps} without explicit completion.")

    print(f"[TERMINATION REASON] {termination_reason}")
    print("\n[DONE] Agent execution finished.")

    # Clean up screenshot directories after task ends
    _cleanup()
    print("[CLEANUP] Screenshot directories removed.")


if __name__ == "__main__":
    main()
