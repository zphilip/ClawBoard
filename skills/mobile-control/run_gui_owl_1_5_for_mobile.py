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
import copy
import json
import os
import shutil
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image

from packages import PACKAGES_NAME_DICT, NAME_PACKAGE_DICT, normalize_package_name
from memory.logger import MemoryEventLogger
from memory.models import ActionCandidate, DecisionInput, MemoryRecord, StateSignature
from memory.plan_executor import PlanExecutor
from memory.plan_store import PlanStore
from memory.policy import MemoryPolicy, NON_CACHEABLE_ACTIONS
from memory.signature import (
    build_canonical_intent_key,
    build_intent_signature,
    build_state_key,
    build_ui_fingerprint,
)
from memory.store import JsonlMemoryStore
from utils import (
    AdbTools,
    annotate_screenshot,
    build_messages,
    ERROR_CALLING_LLM,
    find_element_at_coordinates,
    find_matching_element,
    parse_action,
    repair_json,
    resolve_app_name_via_llm,
    smart_resize,
    GUIOwlWrapper,
    summarise_ui_dump,
    SupervisorLLM,
)

from runner.actions import action_display_name
from runner.coords import (
    action_has_out_of_range_coords,
    bucketed_action_sig,
    detect_relaxed_cycle,
    rescale_coordinates,
    validate_coordinate_drift,
)
from runner.guards import decide_rule_override, launcher_pkgs
from runner.providers import ProviderConfig, VLMProvider, VLMResult


MEMORY_CLICK_OVERRIDE_MAX_DRIFT = 120.0  # normalized 0-1000 coordinate distance


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
    parser.add_argument("--max_steps", type=int, default=30,
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
    parser.add_argument("--memory-decision", choices=["off", "shadow", "enforce"], default="off",
                        help="Memory decision mode: off, shadow (observe only), or enforce.")
    parser.add_argument("--memory-min-score", type=float, default=0.7,
                        help="Minimum score for memory candidates.")
    parser.add_argument("--memory-store", type=str, default="",
                        help="Optional memory store JSONL path for state->action records.")
    parser.add_argument("--memory-replay-mode", choices=["sequential", "single", "plan"], default="sequential",
                        help="Memory replay mode: sequential (default, advances through cached "
                             "actions when screen state changes, uses run_id+step provenance), "
                             "single (one cache replay per state_key per run, safest), or "
                             "plan (replay entire task-level plans without LLM calls).")
    parser.add_argument("--plan-store", type=str, default="",
                        help="Path to task plan JSONL store (defaults to memory_data/plans.jsonl).")
    return parser.parse_args()




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



# Required argument keys for each action type.  Used to validate
# supervisor override tool_calls before accepting them.
_REQUIRED_ACTION_FIELDS: dict[str, tuple[str, ...]] = {
    "click": ("coordinate",),
    "long_press": ("coordinate",),
    "swipe": ("coordinate", "coordinate2"),
    "scroll": ("coordinate", "coordinate2"),
    "type": ("text",),
    "system_button": ("button",),
    "open": ("text",),
}


def _missing_required_fields(action_type: str, args: dict) -> list[str]:
    """Return a list of required field names missing from *args*."""
    required = _REQUIRED_ACTION_FIELDS.get(action_type, ())
    return [k for k in required if k not in args]


def _goal_conflict_in_feedback(feedback: str) -> bool:
    """Return True if the supervisor feedback indicates the VLM is working
    against the task goal (e.g. trying to exit when task is to navigate)."""
    _text = feedback.lower()
    _conflict_signals = [
        "task requires", "not exiting", "not exit",
        "should not", "going against", "而不是", "而非",
        "应先", "必须先",
    ]
    return any(s in _text for s in _conflict_signals)


def _action_text_has_auth(action_text: str) -> bool:
    """Return True if the VLM's reasoning proposes clicking an authorization
    or consent button that must be blocked regardless of context."""
    _text = action_text.lower()
    _auth_keywords = [
        "同意授权", "同意", "授权", "agree", "authorize", "allow",
        "grant", "accept", "确认授权", "允许",
    ]
    return any(kw in _text for kw in _auth_keywords)


def _vlm_wants_to_exit(action_text: str) -> bool:
    """Return True if the VLM's reasoning indicates it wants to exit/close/cancel.

    Used to prevent the transient-dialog fast-path from skipping supervisor
    validation when the VLM is actively trying to exit navigation (counter to
    the task goal).
    """
    _text_lc = action_text.lower()
    _exit_keywords = [
        "退出导航", "退出", "exit navigation", "exit", "关闭", "close",
        "取消", "cancel", "结束", "end", "停止", "stop",
    ]
    return any(kw in _text_lc for kw in _exit_keywords)


def main():
    args = parse_args()

    # Initialize ADB
    adb_tools = AdbTools(adb_path=args.adb_path, device=args.device)

    # Prepare output directories — place INSIDE screenshots/ so they are
    # never left scattered in the skill's root directory.
    # Use .resolve() to guarantee an absolute path even if __file__ is relative.
    _skill_dir = Path(__file__).resolve().parent
    _screenshots_root = _skill_dir / "screenshots"
    _memory_root = _skill_dir / "memory_data"
    _run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
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

    # VLM provider with primary + optional fallback chain.
    _vlm_provider = VLMProvider(
        primary=ProviderConfig(
            api_key=args.api_key,
            base_url=args.base_url,
            model=args.model,
            max_context_size=_vlm_max_ctx,
        ),
        fallback=(
            ProviderConfig(
                api_key=_fb_api_key,
                base_url=_fb_base_url,
                model=_fb_model,
                max_context_size=_fb_max_ctx,
            )
            if _fb_model
            else None
        ),
        compact_mode=_compact_mode,
    )
    if _compact_mode:
        print(f"[VLM] Compact mode ON (max_context_size={_vlm_max_ctx}): using compact system prompt, UI dump limited to {_ui_max_nodes} nodes")

    history = []
    _intent_sig = build_intent_signature(instruction)
    _memory_logger = MemoryEventLogger(_memory_root / "events.jsonl")
    _memory_policy: MemoryPolicy | None = None
    _memory_store: JsonlMemoryStore | None = None
    _MEMORY_GOOD_OUTCOMES = {"completed", "answer_confirmed_complete", "terminate_action"}
    _MEMORY_BAD_OUTCOMES = {
        "parse_failed",
        "screenshot_failed",
        "open_not_found",
        "wait_recovery_home",
        "wrong_screen_home_recovery",
    }
    # These actions are either passive, terminal, or user-dependent. Treating
    # them as positive cache hits causes stale loops such as wait -> wait ->
    # recovery. They can still be logged as failures/forbidden records.
    # Shared with memory.policy.NON_CACHEABLE_ACTIONS to keep read/write
    # filters in sync.
    _MEMORY_NON_CACHEABLE_ACTIONS = NON_CACHEABLE_ACTIONS
    if args.memory_decision != "off":
        _store_path = Path(args.memory_store) if args.memory_store else (_memory_root / "records.jsonl")
        try:
            _memory_store = JsonlMemoryStore(_store_path)
            _memory_policy = MemoryPolicy(_memory_store, min_score=float(args.memory_min_score))
            # Purge stale records for non-cacheable action types (e.g. wait,
            # answer) that may have been written before the write-time filter
            # existed.  This is a one-time cleanup per run and prevents the
            # fastpath from replaying passive/terminal actions.
            _purged = _memory_store.purge_actions(NON_CACHEABLE_ACTIONS)
            _log_t(
                f"[MEMORY] mode={args.memory_decision} store={_store_path} "
                f"min_score={float(args.memory_min_score):.2f}"
                + (f" purged={_purged} stale records" if _purged else "")
            )
        except Exception as _mem_init_err:
            _memory_policy = None
            _log_t(f"[MEMORY] init failed ({_mem_init_err!r}) — disabling memory decision")
    # ------------------------------------------------------------------
    # Task-level plan executor (replay entire recorded plans without LLM)
    # Plan recording is ALWAYS active (builds plans from every successful run).
    # Plan replay only activates when --memory-replay-mode=plan.
    # ------------------------------------------------------------------
    _canonical_intent_key = build_canonical_intent_key(instruction)
    _plan_store_path = (
        Path(args.plan_store) if getattr(args, 'plan_store', '')
        else (_memory_root / "plans.jsonl")
    )
    _plan_executor: PlanExecutor | None = None
    _plan_replay_active = False
    _plan_intent_key: str = ""
    try:
        _plan_store = PlanStore(_plan_store_path)
        _plan_screenshot_dir = str(_memory_root / "plan_screenshots")
        _plan_executor = PlanExecutor(
            _plan_store, adb_tools,
            ui_summariser=summarise_ui_dump,
            ui_fp_builder=build_ui_fingerprint,
            screenshot_dir=_plan_screenshot_dir,
        )
        # Wire up the open handler so plan replay can launch apps
        _plan_executor._open_handler = lambda ap: handle_open_action(
            ap, instruction, adb_tools,
            resolver_api_key, resolver_base_url, resolver_model,
        )
        # Auto-promotion: when a plan exists with 2+ successful completions,
        # switch to plan replay regardless of the explicit mode.  This means
        # the first 1-2 runs use the VLM normally, and subsequent runs skip
        # the VLM entirely.
        _plan_found = _plan_executor.find_plan(_canonical_intent_key)
        _auto_promote = (
            _plan_found is not None
            and _plan_found.success_count >= 2
            and args.memory_replay_mode in ("sequential", "plan")
        )

        if args.memory_replay_mode == "plan" or _auto_promote:
            if _plan_found:
                _plan_executor.start_replay(_plan_found)
                _plan_replay_active = True
                _plan_intent_key = _canonical_intent_key
                _tag = " [AUTO-PROMOTED]" if _auto_promote and args.memory_replay_mode != "plan" else ""
                _log_t(
                    f"[PLAN] replay started{_tag}: intent_key={_canonical_intent_key} "
                    f"steps={len(_plan_found.steps)} "
                    f"success_count={_plan_found.success_count} "
                    f"fail_count={_plan_found.fail_count}"
                )
            else:
                _log_t(
                    f"[PLAN] no stored plan for intent_key={_canonical_intent_key} "
                    "— will record a new plan if this run succeeds"
                )
        else:
            # Recording-only mode: collect steps for future plan building,
            # but do not attempt replay yet.
            _log_t(
                f"[PLAN] recording enabled (mode={args.memory_replay_mode}) "
                f"intent_key={_canonical_intent_key}"
                + (f" success_count={_plan_found.success_count}" if _plan_found else "")
            )
    except Exception as _plan_init_err:
        _plan_executor = None
        _log_t(f"[PLAN] init failed ({_plan_init_err!r}) — plan recording disabled")
    # Set to True once any physical action (click, swipe, type, etc.) is
    # executed.  Used to detect premature 'answer' refusals at step 0.
    any_real_action = False
    # Counts consecutive `wait` actions — used to detect a stuck agent.
    consecutive_waits = 0
    # Rolling window of the last 5 click coordinates.
    # Used to detect a stuck loop (same coordinate tapped 3+ times in a row).
    _recent_click_coords: list[tuple] = []
    _last_state_action_sig = ""
    _same_state_action_count = 0
    _last_state_action_relaxed_sig = ""
    _same_state_action_relaxed_count = 0
    _recent_state_action_relaxed: list[str] = []
    _memory_fastpath_replayed: set[str] = set()
    # Track executed actions globally (by action type + args, without state key)
    # to prevent replaying the same action even when UI state changes
    _executed_actions_global: set[str] = set()
    # Track the state_key of the last fastpath replay.  Sequential advance
    # (skipping already-replayed records) is only allowed when the screen
    # state has actually changed since the last replay.  If the state_key
    # is the same, the previous action didn't navigate to a new screen,
    # so the next cached record likely belongs to a different physical
    # screen that shares this coarse fingerprint.
    _last_fastpath_state_key: str = ""
    _sup_approved_cache: dict[str, float] = {}
    _SUP_APPROVE_CACHE_TTL_SECONDS = 180
    _SUP_APPROVE_CACHE_MAX_SIZE = 200
    _STATE_ACTION_LOOP_THRESHOLD = 3

    # Keywords in the model's action text that signal it is on the wrong screen.
    # When any of these appear the runner injects a Home-correction immediately.
    _WRONG_SCREEN_SIGNALS = [
        "调试界面", "调试工具", "debug interface", "developer",
        "PicoClaw", "picoclaw", "调试", "开发者",
    ]
    _TRANSIENT_DIALOG_KEYWORDS = [
        "退出导航", "确认退出", "退出",
        "close navigation", "exit navigation", "confirm",
    ]

    # Cache installed app display-names once, then reuse them for both the
    # supervisor and the VLM prompt constraints.
    _cached_sup_app_names: list[str] = []
    _cached_inst_app_names: list[str] = []
    _target_app_hint: str = ""
    _target_pkg_hint: str = ""
    _installed_pkg_set: set[str] = set()
    _launcher_pkgs = launcher_pkgs()
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
    _perf_steps = 0
    _perf_vlm_total = 0.0
    _perf_supervisor_total = 0.0
    _perf_vlm_primary_total = 0.0
    _perf_vlm_fallback_total = 0.0
    # Cache-hit counters — aggregated across all steps.
    _cache_fastpath_steps = 0       # memory pre-LLM fastpath
    _cache_plan_replay_steps = 0    # plan replay (VLM skipped)
    _cache_vlm_normal_steps = 0     # normal VLM call (primary or fallback)
    # Supervisor feedback injection — when the supervisor overrides an action
    # because the VLM is going against the task goal, the override reason is
    # injected into the next VLM call so the model course-corrects.
    _supervisor_feedback: str = ""
    for step_id in range(args.max_steps):
        _step_t0 = time.time()
        _step_metrics: dict[str, float] = {}
        _step_summary_emitted = False
        _step_state_key = ""
        _step_ui_fp = ""
        _provider_used = ""
        _step_action_type = ""
        _step_action_args: dict = {}
        _step_state_action_sig = ""
        _step_state_action_relaxed_sig = ""
        _used_memory_fastpath = False
        _memory_hit = False
        _memory_overrode_action = False
        _memory_blocked = False
        _memory_score = 0.0
        _memory_reason = "off"
        _memory_candidate_action: dict = {}

        def _emit_step_summary(_outcome: str) -> None:
            nonlocal _step_summary_emitted
            nonlocal _perf_steps
            nonlocal _perf_vlm_total, _perf_supervisor_total
            nonlocal _perf_vlm_primary_total, _perf_vlm_fallback_total
            nonlocal _cache_fastpath_steps, _cache_plan_replay_steps, _cache_vlm_normal_steps
            if _step_summary_emitted:
                return
            _step_summary_emitted = True

            _llm_primary = _step_metrics.get("vlm_primary", 0.0)
            _llm_fallback = _step_metrics.get("vlm_fallback", 0.0)
            _llm_total = _llm_primary + _llm_fallback
            _sup_total = _step_metrics.get("supervisor", 0.0)
            _den = max(_llm_total + _sup_total, 1e-9)
            _sup_share = (_sup_total / _den) * 100.0

            _perf_steps += 1
            _perf_vlm_total += _llm_total
            _perf_supervisor_total += _sup_total
            _perf_vlm_primary_total += _llm_primary
            _perf_vlm_fallback_total += _llm_fallback

            # Classify this step for the end-of-run cache-hit summary.
            if _used_memory_fastpath and _provider_used == "plan-replay":
                _cache_plan_replay_steps += 1
            elif _used_memory_fastpath and _provider_used == "memory-fastpath":
                _cache_fastpath_steps += 1
            else:
                _cache_vlm_normal_steps += 1

            _parts = [f"step={step_id}", f"outcome={_outcome}", f"total={time.time() - _step_t0:.2f}s"]
            for _k in (
                "screenshot", "ui_dump", "vlm_primary", "vlm_fallback",
                "supervisor", "action",
            ):
                if _k in _step_metrics:
                    _parts.append(f"{_k}={_step_metrics[_k]:.2f}s")
            _log_t("[STEP SUMMARY] " + " | ".join(_parts))
            _log_t(
                "[TIMING COMPARE] "
                f"step={step_id} | llm_total={_llm_total:.2f}s "
                f"(primary={_llm_primary:.2f}s, fallback={_llm_fallback:.2f}s) | "
                f"supervisor={_sup_total:.2f}s | supervisor_share={_sup_share:.1f}%"
            )
            
            # Enhanced step summary with action details
            if _step_action_type:
                _action_detail = f"type={_step_action_type}"
                if _step_action_args:
                    if _step_action_type == "type":
                        _action_detail += f" text={_step_action_args.get('text', '')!r}"
                    elif _step_action_type == "click":
                        _coord = _step_action_args.get("coordinate", [])
                        if len(_coord) >= 2:
                            _action_detail += f" coord={_coord}"
                    elif _step_action_type == "system_button":
                        _action_detail += f" button={_step_action_args.get('button', '')}"
                
                _provider_info = f"provider={_provider_used}"
                _memory_info = ""
                if _used_memory_fastpath:
                    _memory_info = " [MEMORY FASTPATH]"
                elif _memory_overrode_action:
                    _memory_info = " [MEMORY OVERRIDE]"
                
                _log_t(f"[STEP ACTION] {_action_detail} | {_provider_info}{_memory_info}")
            # Optional memory record persistence (telemetry -> actionable cache).
            if _memory_store is not None and _step_state_key and _step_action_type:
                try:
                    _action_non_cacheable = _step_action_type in _MEMORY_NON_CACHEABLE_ACTIONS
                    _is_good_memory = (
                        _outcome in _MEMORY_GOOD_OUTCOMES and not _action_non_cacheable
                    )
                    _is_bad_memory = _outcome in _MEMORY_BAD_OUTCOMES
                    # Do not write passive/terminal actions as positive cache
                    # records. A successful wait means only "nothing failed",
                    # not "wait is the next best action for this screen".
                    if _is_good_memory or _is_bad_memory:
                        # 新增：为点击动作捕获元素签名用于漂移验证
                        target_element_sig = None
                        original_screen_res = None
                        if _step_action_type == "click" and _is_good_memory:
                            try:
                                # 获取当前UI dump
                                ui_xml = adb_tools.get_ui_dump()
                                if ui_xml:
                                    # 根据缓存的坐标查找对应的UI元素
                                    click_coord = _step_action_args.get("coordinate", [0, 0])
                                    # 转换为实际像素坐标（需要知道截图尺寸）
                                    from PIL import Image
                                    img = Image.open(screenshot_path)
                                    actual_x = int(click_coord[0] / 1000 * img.width)
                                    actual_y = int(click_coord[1] / 1000 * img.height)
                                    
                                    # 查找包含该坐标的UI元素
                                    target_element_sig = find_element_at_coordinates(ui_xml, actual_x, actual_y)
                                    original_screen_res = (img.width, img.height)
                            except Exception as e:
                                _log_t(f"[MEMORY] Failed to capture element signature: {e}")
                        
                        _memory_store.append(
                            MemoryRecord(
                                state_key=_step_state_key,
                                intent_key=_intent_sig,
                                action_type=_step_action_type,
                                action_args=dict(_step_action_args),
                                success_count=1 if _is_good_memory else 0,
                                fail_count=1 if _is_bad_memory else 0,
                                forbidden=_is_bad_memory,
                                reason=_outcome,
                                source_run_id=_run_id,
                                source_step=step_id,
                                action_description=_step_action_description,
                                target_element_signature=target_element_sig,
                                original_screen_resolution=original_screen_res,
                            )
                        )
                except Exception:
                    pass
            try:
                _memory_logger.log_event({
                    "type": "step_outcome",
                    "run_id": _run_id,
                    "step": step_id,
                    "outcome": _outcome,
                    "instruction": instruction,
                    "intent_signature": _intent_sig,
                    "foreground_pkg": _fg_pkg,
                    "ui_fingerprint": _step_ui_fp,
                    "state_key": _step_state_key,
                    "provider_used": _provider_used,
                    "action_type": _step_action_type,
                    "action_args": _step_action_args,
                    "memory": {
                        "mode": args.memory_decision,
                        "hit": _memory_hit,
                        "overrode_action": _memory_overrode_action,
                        "used_fastpath": _used_memory_fastpath,
                        "blocked": _memory_blocked,
                        "score": round(_memory_score, 4),
                        "reason": _memory_reason,
                        "candidate_action": _memory_candidate_action,
                    },
                    "metrics": {
                        "step_total": round(time.time() - _step_t0, 4),
                        "screenshot": round(_step_metrics.get("screenshot", 0.0), 4),
                        "ui_dump": round(_step_metrics.get("ui_dump", 0.0), 4),
                        "vlm_primary": round(_llm_primary, 4),
                        "vlm_fallback": round(_llm_fallback, 4),
                        "vlm_total": round(_llm_total, 4),
                        "supervisor": round(_sup_total, 4),
                        "action": round(_step_metrics.get("action", 0.0), 4),
                        "supervisor_share": round(_sup_share, 2),
                    },
                })
            except Exception:
                # Read-only instrumentation must never affect runtime behavior.
                pass

        print(f"\n{'='*50}")
        print(f"STEP {step_id}")
        print(f"[STEP DEBUG] history_len={len(history)} any_real_action={any_real_action} max_steps={args.max_steps}")
        _log_t(f"[STEP START] step={step_id}")

        # ADB foreground-app check — get ground truth before screenshot + VLM.
        # If the device disappeared, wait for reconnection before proceeding.
        _fg_pkg = adb_tools.get_foreground_package()
        if _fg_pkg:
            _fg_names = PACKAGES_NAME_DICT.get(_fg_pkg, [])
            _fg_label = f"{_fg_pkg} ({', '.join(_fg_names)})" if _fg_names else _fg_pkg
            print(f"[Foreground] {_fg_label}")
        else:
            _fg_label = ""
            _log_t("[WARN] foreground check failed — device may be disconnected")
            if not adb_tools.wait_for_device(timeout=30):
                _log_t("[ERROR] device did not reappear — aborting run")
                termination_reason = "device_disconnected"
                break
        print(f"{'='*50}")

        # 1. Capture screenshot — if it fails the device may have been
        #    unplugged; wait for reconnection instead of looping uselessly.
        _t_screenshot = time.time()
        _ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:19]
        screenshot_path = os.path.join(task_dir, f"screenshot_{step_id}_{_ts}.png")
        if not adb_tools.get_screenshot(screenshot_path):
            print("[ERROR] Failed to capture screenshot — device may be disconnected.")
            _emit_step_summary("screenshot_failed")
            if adb_tools.wait_for_device(timeout=30):
                print("[ADB] Device reappeared — retrying screenshot on next iteration")
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
        _step_ui_fp = build_ui_fingerprint(_fg_pkg, _ui_summary)
        _step_state_key = build_state_key(_intent_sig, _step_ui_fp, "default")
        _log_t(f"[MEMORY KEY] state_key={_step_state_key} ui_fp={_step_ui_fp} intent={_intent_sig}")
        _ui_text_lc = ((_ui_summary or "") + "\n" + (_fg_label or "")).lower()
        _has_transient_confirm_dialog = any(k.lower() in _ui_text_lc for k in _TRANSIENT_DIALOG_KEYWORDS)
        if _has_transient_confirm_dialog:
            _log_t("[DIALOG] transient confirm dialog cues detected (may auto-dismiss quickly)")

        # ------------------------------------------------------------------
        # 1c-plan. Task-level plan replay (highest priority).
        # When a stored plan exists for this intent, execute the next step
        # directly via ADB without calling the VLM.  Verification is done
        # by checking the foreground package after each step.
        # ------------------------------------------------------------------
        _plan_step_executed = False
        if _plan_executor is not None and _plan_executor.is_replaying():
            _plan_step = _plan_executor.next_step()
            if _plan_step is not None:
                _plan_t0 = time.time()
                _log_t(
                    f"[PLAN] executing step {_plan_executor.replay_cursor}/"
                    f"{len(_plan_executor.replay_plan.steps) - 1}: "
                    f"{_plan_step.action_type} {_plan_step.action_args}"
                )
                _plan_pre_pkg = _fg_pkg or ""
                _plan_ok = _plan_executor.execute_and_verify(_plan_step)
                _plan_elapsed = time.time() - _plan_t0

                if _plan_ok:
                    _plan_step_executed = True
                    any_real_action = True
                    _step_action_type = _plan_step.action_type
                    _step_action_args = dict(_plan_step.action_args)
                    _step_action_description = f"[PLAN REPLAY] {_plan_step.action_description or _plan_step.action_type}"
                    _provider_used = "plan-replay"
                    _step_metrics["vlm_primary"] = 0.0
                    _step_metrics["vlm_fallback"] = 0.0
                    _step_metrics["supervisor"] = 0.0
                    _step_metrics["action"] = _plan_elapsed
                    _used_memory_fastpath = True  # reuse flag for telemetry
                    _memory_overrode_action = True
                    _log_t(
                        f"[PLAN] step {_plan_executor.replay_cursor - 1} OK "
                        f"({_plan_elapsed:.2f}s) — VLM skipped"
                    )
                    # Build a synthetic output_text for history recording
                    _plan_action_param = dict(_plan_step.action_args)
                    if "action" not in _plan_action_param:
                        _plan_action_param["action"] = _plan_step.action_type
                    # Emit an action line the wrapper's stdout monitor can parse
                    # so the user gets live progress narration during plan replay.
                    _plan_action_json = json.dumps(
                        {"name": "mobile_use", "arguments": _plan_action_param},
                        ensure_ascii=False,
                    )
                    print(f"[PLAN REPLAY ACTION] {_plan_action_json}", flush=True)
                    # Use stored description, stripping any [PLAN REPLAY] prefix
                    # from prior replays to avoid "[PLAN REPLAY] [PLAN REPLAY]..."
                    _plan_desc = (_plan_step.action_description or '').replace('[PLAN REPLAY] ', '')
                    output_text = (
                        f"Action: [PLAN REPLAY] {_plan_desc or 'replay cached action'}\n"
                        "<tool_call>\n"
                        + _plan_action_json
                        + "\n"
                    )
                    # Rescale coordinates for history/annotation
                    try:
                        img = Image.open(screenshot_path)
                        _rh, _rw = smart_resize(img.height, img.width, factor=16, min_pixels=3136, max_pixels=1003520 * 200)
                        action_parameter = rescale_coordinates(copy.deepcopy(_plan_action_param), _rw, _rh)
                    except Exception:
                        action_parameter = _plan_action_param
                    action = {"name": "mobile_use", "arguments": action_parameter}
                    # Record history and annotate screenshot
                    history.append({"output": output_text, "image": screenshot_path})
                    try:
                        annotate_screenshot(
                            screenshot_path,
                            action_parameter,
                            os.path.join(anno_dir, f"screenshot_anno_{step_id}.png"),
                        )
                    except Exception:
                        pass
                    _emit_step_summary("plan_replay_completed")
                    _log_t(f"[STEP END] step={step_id} total={time.time() - _step_t0:.2f}s")
                    # Check if plan is now exhausted (all steps done)
                    if _plan_executor.next_step() is None:
                        _log_t("[PLAN] all steps replayed successfully — auto-terminating")
                        termination_reason = "plan_replay_complete"
                        print("[TERMINATED] Task completed.")
                        _plan_executor.end_replay()
                        time.sleep(2)
                        break  # exit the step loop
                    time.sleep(2)
                    continue
                else:
                    _verify_detail = getattr(_plan_executor, 'last_verify_detail', '')
                    _log_t(
                        f"[PLAN] step {_plan_executor.replay_cursor} FAILED "
                        f"({_plan_elapsed:.2f}s) — falling back to VLM"
                        + (f" [{_verify_detail}]" if _verify_detail else "")
                    )
                    # Pause replay so VLM handles this step;
                    # resume_replay() will be called after VLM succeeds.
                    _plan_executor.pause_replay()

        # 1c. Memory pre-LLM fast path.
        # When memory decision is 'enforce' and there is a high-confidence cached
        # action for the current state, skip the VLM call entirely and replay
        # the cached action directly.  Supervisor is also skipped in this path
        # since the action was already approved in a prior run.
        _pre_llm_action_parameter: dict | None = None
        if (
            args.memory_decision == "enforce"
            and _memory_policy is not None
            and _step_state_key
        ):
            try:
                _dinput_pre = DecisionInput(
                    state=StateSignature(
                        foreground_pkg=_fg_pkg or "",
                        ui_fingerprint=_step_ui_fp,
                        intent_signature=_intent_sig,
                        device_bucket="default",
                    ),
                    proposed_action=None,
                )
                # Pass the replay set so the policy skips already-replayed
                # records and returns the next best unused cached action.
                # In "sequential" mode: allow sequential advance when the
                # screen state has changed since the last replay (the policy
                # also checks run_id+step provenance).
                # In "single" mode: never pass exclude_sigs — each state_key
                # gets at most one cache replay per run, then falls to VLM.
                if args.memory_replay_mode == "sequential":
                    _allow_sequential = (
                        bool(_memory_fastpath_replayed)
                        and _step_state_key != _last_fastpath_state_key
                    )
                    _exclude = _memory_fastpath_replayed if _allow_sequential else None
                else:
                    # single mode — no exclude_sigs, policy returns top match;
                    # the runner's own replay check below blocks duplicates.
                    _exclude = None
                _mout_pre = _memory_policy.decide(
                    _step_state_key, _intent_sig, _dinput_pre,
                    exclude_sigs=_exclude,
                    current_run_id=_run_id,
                )
                _memory_reason = _mout_pre.reason or "none"
                _memory_score = float((_mout_pre.diagnostics or {}).get("score", 0.0) or 0.0)
                _memory_blocked = bool(_mout_pre.blocked)
                _memory_hit = bool(_mout_pre.action is not None)
                _cached_action_description = ""
                if _mout_pre.action is not None:
                    _memory_candidate_action = {
                        "action_type": _mout_pre.action.action_type,
                        "arguments": copy.deepcopy(_mout_pre.action.arguments or {}),
                    }
                    _cached_action_description = getattr(_mout_pre.action, 'action_description', '')
                if _mout_pre.use_cached_action and _mout_pre.action is not None and not _mout_pre.blocked:
                    _mem_args = copy.deepcopy(_mout_pre.action.arguments or {})
                    if "action" not in _mem_args:
                        _mem_args["action"] = _mout_pre.action.action_type
                    _fastpath_action_type = _mem_args.get("action", "")
                    # Allow only active, deterministic actions for the fastpath.
                    # Exclude passive waits and terminal/user-dependent actions.
                    _fastpath_allowed_types = {"click", "key", "system_button", "open", "type"}
                    _replay_sig = (
                        f"{_step_state_key}|{_fastpath_action_type}|"
                        f"{json.dumps(_mem_args, ensure_ascii=False, sort_keys=True)}"
                    )
                    # Also create action-only signature (without state key) for global deduplication
                    _action_only_sig = f"{_fastpath_action_type}|{json.dumps(_mem_args, ensure_ascii=False, sort_keys=True)}"
                    
                    if _fastpath_action_type not in _fastpath_allowed_types:
                        _memory_reason = "cached_action_type_not_fastpath_safe"
                        _log_t(
                            f"[MEMORY] pre-LLM fastpath skipped: cached action type {_fastpath_action_type!r} "
                            "is not safe for replay"
                        )
                    elif _action_only_sig in _executed_actions_global and _fastpath_action_type not in ("type", "open"):
                        # type actions are verified by type_with_verification (UI dump
                        # check after typing), and open actions verify via foreground
                        # package check — allow them to replay on different states.
                        _memory_reason = "cached_action_already_executed_globally"
                        _log_t(
                            f"[MEMORY] pre-LLM fastpath skipped: action {_fastpath_action_type!r} with args "
                            f"{_mem_args} was already executed in this run (different state)"
                        )
                    elif _replay_sig in _memory_fastpath_replayed:
                        _memory_reason = "cached_action_already_replayed_this_run"
                        _log_t(
                            "[MEMORY] pre-LLM fastpath skipped: cached state/action "
                            "was already replayed in this run"
                        )
                    elif action_has_out_of_range_coords(_mem_args):
                        _memory_reason = "cached_action_non_normalized_coords"
                        _log_t(
                            "[MEMORY] pre-LLM fastpath skipped: cached coords are out of 0-1000 range"
                        )
                    elif _fastpath_action_type == "click":
                        # Click actions MUST have a target_element_signature so
                        # we can validate coordinate drift against the current
                        # screen.  Old memory records written before the
                        # element-signature code existed have no signature and
                        # must NOT be replayed — a cached click coordinate that
                        # was valid on a different screen 4 days ago can land
                        # anywhere on today's screen.
                        if (hasattr(_mout_pre.action, 'target_element_signature') and
                                _mout_pre.action.target_element_signature is not None):
                            # Drift validation — reuse the UI dump already captured
                            # at step start instead of calling get_ui_dump() again
                            # over ADB.
                            try:
                                if _ui_xml:
                                    _drift_valid = validate_coordinate_drift(
                                        _mem_args,
                                        _mout_pre.action.target_element_signature,
                                        _ui_xml,
                                        screenshot_path,
                                        log_func=_log_t,
                                    )
                                    if not _drift_valid:
                                        _memory_reason = "cached_action_coordinate_drift_too_large"
                                        _log_t(
                                            "[MEMORY] pre-LLM fastpath skipped: coordinate drift exceeds threshold"
                                        )
                                    else:
                                        _pre_llm_action_parameter = _mem_args
                                        _used_memory_fastpath = True
                                        _memory_overrode_action = True
                                        _memory_fastpath_replayed.add(_replay_sig)
                                        _last_fastpath_state_key = _step_state_key
                                        _log_t(
                                            f"[MEMORY] pre-LLM fastpath score={_memory_score:.3f} action={_pre_llm_action_parameter}"
                                        )
                                else:
                                    _memory_reason = "cached_action_cannot_validate_drift"
                                    _log_t(
                                        "[MEMORY] pre-LLM fastpath skipped: cannot validate coordinate drift without UI dump"
                                    )
                            except Exception as e:
                                _memory_reason = f"cached_action_drift_validation_error:{e.__class__.__name__}"
                                _log_t(f"[MEMORY] pre-LLM fastpath skipped: drift validation error ({e!r})")
                        else:
                            _memory_reason = "cached_click_missing_element_signature"
                            _log_t(
                                "[MEMORY] pre-LLM fastpath skipped: cached click has no "
                                "target_element_signature (record is too old — delete "
                                "records.jsonl to start fresh)"
                            )
                    else:
                        _pre_llm_action_parameter = _mem_args
                        _used_memory_fastpath = True
                        _memory_overrode_action = True
                        _memory_fastpath_replayed.add(_replay_sig)
                        _executed_actions_global.add(_action_only_sig)
                        _last_fastpath_state_key = _step_state_key
                        _log_t(
                            f"[MEMORY] pre-LLM fastpath score={_memory_score:.3f} action={_pre_llm_action_parameter}"
                        )
            except Exception as _mem_pre_err:
                _memory_reason = f"error:{_mem_pre_err.__class__.__name__}"
                _log_t(f"[MEMORY] pre-LLM fastpath error ({_mem_pre_err!r}) — fallback to normal path")

        # Detect ride-hailing keywords in UI dump — used for LLM warning
        # injection (below) and supervisor skip prevention (3c block).
        _is_nav_task = any(
            kw in instruction.lower()
            for kw in ("导航", "navigate", "路线", "direction")
        )
        _has_taxi_keywords = bool(
            _ui_summary and any(
                kw in (_ui_summary or "").lower()
                for kw in ("打车", "叫车", "呼叫", "立即叫车", "网约车")
            )
        )

        # 2. Call the VLM (delegated to VLMProvider for primary + fallback chain).
        if _pre_llm_action_parameter is None:
            # Inject supervisor feedback from previous overrides so the VLM
            # knows why its action was rejected and course-corrects.
            _effective_instruction = instruction
            if _supervisor_feedback:
                _effective_instruction = (
                    f"{instruction}\n\n[IMPORTANT] Your previous action was "
                    f"overridden: {_supervisor_feedback}"
                )
                _supervisor_feedback = ""  # clear after injection
            # Inject transport mode warning when the UI shows ride-hailing
            # keywords but the task requires free driving navigation.
            if _is_nav_task and _has_taxi_keywords:
                _nav_warning = (
                    "\n\n[CRITICAL] The screen shows 打车/ride-hailing mode active "
                    "(¥ prices or 呼叫/叫车 buttons visible). This is WRONG for a "
                    "navigation task. Click the 驾车 (driving) tab FIRST before "
                    "anything else. Never click 同意授权 or导航 in taxi mode."
                )
                _effective_instruction = _effective_instruction.rstrip() + _nav_warning
            _vlm_result = _vlm_provider.call(
                screenshot_path, _effective_instruction, history, args.model,
                foreground_pkg=_fg_label,
                ui_summary=_ui_summary,
                installed_apps_hint=", ".join(_cached_inst_app_names[:80]),
                target_app_hint=_target_app_hint,
            )
            output_text = _vlm_result.output_text
            _step_metrics["vlm_primary"] = _vlm_result.primary_seconds
            _step_metrics["vlm_fallback"] = _vlm_result.fallback_seconds
            _provider_used = _vlm_result.provider_label

            print(f"[MODEL OUTPUT]\n{output_text}")
        else:
            output_text = (
                f"Action: [MEMORY FASTPATH] {_cached_action_description or 'reuse cached action'}\n"
                "<tool_call>\n"
                + json.dumps({"name": "mobile_use", "arguments": _pre_llm_action_parameter}, ensure_ascii=False)
                + "\n</tool_call>"
            )
            _provider_used = "memory-fastpath"
            _step_metrics["vlm_primary"] = 0.0
            _step_metrics["vlm_fallback"] = 0.0
            _step_metrics["supervisor"] = 0.0
            print(f"[VLM] provider used: {_provider_used}")
            print(f"[MODEL OUTPUT]\n{output_text}")

        # 3a. Wrong-screen early exit: if the model text explicitly mentions a
        # debug/developer/wrong-app screen, press Home and restart rather than
        # blindly executing the suggested action.
        _action_text = output_text.split("<tool_call>")[0]
        _step_action_description = _action_text.strip()[:200]  # Capture VLM reasoning for memory cache
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
        _step_action_type = str(action_parameter.get("action", ""))
        _step_action_args = copy.deepcopy(action_parameter)

        # Hard block: the VLM must NEVER click authorization/consent buttons.
        # Prompt rules are not enough — gui-owl ignores them.  We override
        # before the action reaches the supervisor or execution.
        if _step_action_type == "click" and _action_text_has_auth(_action_text):
            print(
                "[BLOCKED] VLM proposed clicking an authorization/consent button "
                f"— overriding to Back: {_action_text[:120]}"
            )
            _blocked_note = (
                "Action: [BLOCKED] authorization click rejected — pressing Back\n"
                "<tool_call>\n"
                + json.dumps({"name": "mobile_use", "arguments": {"action": "system_button", "button": "Back"}}, ensure_ascii=False)
                + "\n</tool_call>"
            )
            history.append({"output": _blocked_note, "image": screenshot_path})
            adb_tools.back()
            _emit_step_summary("blocked_auth_click")
            time.sleep(1.5)
            continue

        # Track this action globally to prevent memory from replaying it later
        _current_action_only_sig = f"{_step_action_type}|{json.dumps(_step_action_args, ensure_ascii=False, sort_keys=True)}"
        _executed_actions_global.add(_current_action_only_sig)
        # _step_action_description already captured at line ~1140 from output_text
        _step_state_action_sig = (
            f"{_step_state_key}|{_step_action_type}|"
            f"{json.dumps(_step_action_args, ensure_ascii=False, sort_keys=True)}"
        )
        # Relaxed detector: bucket coordinates so that jitter on the same
        # button (±10-20 units) collapses to one bucket, while clicks on
        # different buttons (hundreds of units apart) stay distinct.
        _bucketed_sig = bucketed_action_sig(_step_action_type, _step_action_args)
        _step_state_action_relaxed_sig = f"{_step_state_key}|{_bucketed_sig}"

        # State-action loop detector: same scene + same action repeated several times.
        if _step_state_action_sig and _step_state_action_sig == _last_state_action_sig:
            _same_state_action_count += 1
        else:
            _same_state_action_count = 1
            _last_state_action_sig = _step_state_action_sig

        # Relaxed detector: ignore volatile action args (e.g. slightly changing coordinates).
        if _step_state_action_relaxed_sig and _step_state_action_relaxed_sig == _last_state_action_relaxed_sig:
            _same_state_action_relaxed_count += 1
        else:
            _same_state_action_relaxed_count = 1
            _last_state_action_relaxed_sig = _step_state_action_relaxed_sig

        if _step_state_action_relaxed_sig:
            _recent_state_action_relaxed.append(_step_state_action_relaxed_sig)
            if len(_recent_state_action_relaxed) > 12:
                _recent_state_action_relaxed.pop(0)

        _loop_cycle_detected, _loop_period, _loop_pattern = detect_relaxed_cycle(_recent_state_action_relaxed)
        _log_t(
            "[LOOP DEBUG] "
            f"strict_count={_same_state_action_count} "
            f"relaxed_count={_same_state_action_relaxed_count} "
            f"cycle_detected={_loop_cycle_detected} period={_loop_period}"
        )
        if _loop_cycle_detected:
            _log_t(f"[LOOP DEBUG] cycle_pattern={_loop_pattern}")

        _loop_recovery_relaunch = False
        if (
            _same_state_action_count >= _STATE_ACTION_LOOP_THRESHOLD
            or _same_state_action_relaxed_count >= _STATE_ACTION_LOOP_THRESHOLD
            or _loop_cycle_detected
        ):
            _log_t(
                f"[LOOP] detected (strict={_same_state_action_count}, "
                f"relaxed={_same_state_action_relaxed_count}, "
                f"cycle={_loop_cycle_detected}/p{_loop_period}); "
                "will force recovery path"
            )
            _loop_recovery_relaunch = True

        # 3aa. Optional memory decision layer (default: off).
        _memory_confirmed_vlm = False
        if _memory_policy is not None and _step_state_key:
            try:
                _dinput = DecisionInput(
                    state=StateSignature(
                        foreground_pkg=_fg_pkg or "",
                        ui_fingerprint=_step_ui_fp,
                        intent_signature=_intent_sig,
                        device_bucket="default",
                    ),
                    proposed_action=ActionCandidate(
                        action_type=_step_action_type,
                        arguments=dict(_step_action_args),
                        source="llm",
                    ),
                )
                _mout = _memory_policy.decide(_step_state_key, _intent_sig, _dinput)
                _memory_reason = _mout.reason or "none"
                _memory_score = float((_mout.diagnostics or {}).get("score", 0.0) or 0.0)
                _memory_blocked = bool(_mout.blocked)
                _memory_hit = bool(_mout.action is not None)

                if _mout.action is not None:
                    _memory_candidate_action = {
                        "action_type": _mout.action.action_type,
                        "arguments": copy.deepcopy(_mout.action.arguments or {}),
                    }

                if args.memory_decision == "shadow":
                    if _mout.use_cached_action and _mout.action is not None and not _mout.blocked:
                        _log_t(
                            f"[MEMORY] shadow hit score={_memory_score:.3f} "
                            f"would_override={_memory_candidate_action}"
                        )
                elif args.memory_decision == "enforce":
                    if _mout.use_cached_action and _mout.action is not None and not _mout.blocked:
                        # Check if VLM independently agreed with the cached action.
                        # When both memory and VLM produce the same action for the same
                        # state, the supervisor check is redundant — the action was already
                        # validated in a prior run and confirmed by the VLM.
                        _cached_type = _mout.action.action_type
                        _cached_args = _mout.action.arguments or {}
                        _vlm_bucketed = bucketed_action_sig(_step_action_type, _step_action_args)
                        _cached_bucketed = bucketed_action_sig(_cached_type, _cached_args)
                        if _step_action_type == _cached_type and _vlm_bucketed == _cached_bucketed:
                            _memory_confirmed_vlm = True
                            _log_t(
                                f"[MEMORY] enforce confirmed VLM score={_memory_score:.3f} "
                                f"action={_step_action_type} — supervisor will be skipped"
                            )
                        else:
                            _memory_reason = "post_llm_override_disabled"
                            _log_t(
                                f"[MEMORY] enforce hit score={_memory_score:.3f} "
                                "but VLM action differs from cached; keeping current LLM action"
                            )
            except Exception as _mem_err:
                _memory_reason = f"error:{_mem_err.__class__.__name__}"
                _log_t(f"[MEMORY] decision error ({_mem_err!r}) — fallback to normal path")

        # 3b. Rule-based guard — delegates to runner.guards for the pure
        # decision logic; override application stays here because it mutates
        # history, calls adb_tools, and interacts with supervisor.
        _proposed_action = action_parameter.get("action", "")
        _rb_override = decide_rule_override(
            _proposed_action,
            _action_text,
            any_real_action=any_real_action,
            loop_recovery_relaunch=_loop_recovery_relaunch,
            target_pkg_hint=_target_pkg_hint,
            target_app_hint=_target_app_hint,
            fg_pkg=_fg_pkg or "",
        )

        if _rb_override:
            if supervisor is not None:
                print("[SUPERVISOR] skipped (rule override already applied)")
            _rb_note = (
                f"Action: [RULE OVERRIDE] {_rb_override}\n"
                f"\n{json.dumps({'name': 'mobile_use', 'arguments': _rb_override}, ensure_ascii=False)}\n"
            )
            history.append({"output": _rb_note, "image": screenshot_path})
            # Track rule override action globally
            _rule_action_sig = f"{_rb_override.get('action', '')}|{json.dumps(_rb_override, ensure_ascii=False, sort_keys=True)}"
            _executed_actions_global.add(_rule_action_sig)
            if _rb_override.get("action") == "system_button" and _rb_override.get("button") == "Home":
                print("[ACTION EXEC] RULE override -> Home (start)")
                if not adb_tools.home():
                    print("[ACTION EXEC] RULE override -> Home failed")
                else:
                    print("[ACTION EXEC] RULE override -> Home done")
                if _loop_recovery_relaunch and _target_app_hint:
                    # Force-close the app before reopening to clear stuck state
                    # (e.g., authentication dialogs, popups). Without this, the
                    # app resumes in the same blocked state and the loop continues.
                    try:
                        if _target_pkg_hint:
                            _force_cmd = f"{adb_tools.adb_path}{adb_tools._device_flag}shell am force-stop {_target_pkg_hint}"
                            print(f"[ACTION EXEC] LOOP recovery -> force-stop {_target_pkg_hint}")
                            # Use the imported subprocess module
                            import subprocess as sp
                            sp.run(_force_cmd, capture_output=True, text=True, shell=True, timeout=5)
                            time.sleep(0.5)  # brief delay for force-stop to complete
                        print(f"[ACTION EXEC] LOOP recovery relaunch -> open {_target_app_hint!r} (start)")
                        _opened = handle_open_action(
                            {"action": "open", "text": _target_app_hint},
                            instruction,
                            adb_tools,
                            resolver_api_key,
                            resolver_base_url,
                            resolver_model,
                        )
                        if _opened:
                            print("[ACTION EXEC] LOOP recovery relaunch -> done")
                        else:
                            print("[ACTION EXEC] LOOP recovery relaunch -> failed")
                    except Exception as e:
                        print(f"[ACTION EXEC] LOOP recovery failed: {e}")
                        # Continue with normal flow even if recovery fails
                # Reset loop detectors after explicit recovery to avoid
                # repeatedly triggering on stale pre-recovery signatures.
                _last_state_action_sig = ""
                _same_state_action_count = 0
                _last_state_action_relaxed_sig = ""
                _same_state_action_relaxed_count = 0
                _recent_state_action_relaxed.clear()
                consecutive_waits = 0
                _emit_step_summary("rule_override_home")
                time.sleep(2)
                continue
            action_parameter = _rb_override
            action["arguments"] = _rb_override
            _step_action_type = str(action_parameter.get("action", ""))
            _step_action_args = copy.deepcopy(action_parameter)

        # 3c. Supervisor validation — fast text LLM checks intent vs. tool_call.
        # Passes UI dump so it can verify answer claims against actual screen content.
        # Only active when supervisor is configured.
        # Skip for 'answer' actions — validate() checks tool_call correctness
        # (coordinates, app names), but for answer the question is 'is the task
        # done?', which is handled by is_task_complete() in the answer handler.
        if (supervisor is not None and _rb_override is None
                and _proposed_action != "answer"):
            _skip_supervisor = False
            _sup_skip_reason = ""
            _sup_apps_hint = ""
            # Skip supervisor for memory fastpath — the action was already approved
            # in a prior run; re-validating it every time defeats the purpose.
            # EXCEPTION: navigation tasks where the UI dump shows transport mode
            # keywords (打车/叫车).  The memory fastpath replays a cached action
            # from a prior run, but the transport mode may have changed — e.g. the
            # user used 打车 last time and now we need 驾车.  Without supervisor
            # validation the VLM navigates in the wrong mode.
            if _used_memory_fastpath and not _has_taxi_keywords:
                _skip_supervisor = True
                _sup_skip_reason = "memory_fastpath_cached_action"
            if _proposed_action == "open" and _cached_sup_app_names:
                _sup_apps_hint = ", ".join(_cached_sup_app_names[:60])

            # Fast-path for transient confirmation dialogs to avoid waiting 10-20s
            # while the dialog auto-dismisses and causes stale-click loops.
            # Only skip when the VLM is NOT trying to exit/cancel — if the VLM
            # wants to confirm the exit, the supervisor must still validate.
            if (_has_transient_confirm_dialog and _proposed_action == "click"
                    and not _vlm_wants_to_exit(_action_text)):
                _skip_supervisor = True
                _sup_skip_reason = "transient_confirm_dialog_fast_path"

            # Skip supervisor if the same state+action was approved recently.
            _sup_cache_key = _step_state_action_sig
            if _sup_cache_key and not _skip_supervisor:
                _cached_until = _sup_approved_cache.get(_sup_cache_key, 0.0)
                _now = time.time()
                if _cached_until > _now:
                    _skip_supervisor = True
                    _sup_skip_reason = "recent_same_state_action_already_approved"
                elif _cached_until > 0:
                    # Expired — remove stale entry so the dict stays lean.
                    del _sup_approved_cache[_sup_cache_key]

            # Skip supervisor when post-LLM memory confirmed the VLM action.
            # Both memory (validated in a prior run) and VLM independently agree
            # on the same action for the same state — supervisor is redundant.
            # Exception: same as above — don't skip when taxi keywords visible.
            if (not _skip_supervisor) and _memory_confirmed_vlm and not _has_taxi_keywords:
                _skip_supervisor = True
                _sup_skip_reason = "memory_confirmed_vlm_action"

            if _skip_supervisor:
                print(f"[SUPERVISOR] skipped ({_sup_skip_reason})")
                _step_metrics["supervisor"] = 0.0
            else:
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

            if (not _skip_supervisor) and _sup_verdict.get("verdict") == "override":
                _reason = _sup_verdict.get("reason", "")
                print(f"[SUPERVISOR] overriding action — {_reason}")
                # Inject the override reason as feedback so the VLM
                # knows why its action was wrong and course-corrects on
                # the next step instead of repeating the same mistake.
                _supervisor_feedback = _reason
                _override_tc = _sup_verdict.get("tool_call", {})
                if _override_tc and "arguments" in _override_tc:
                    # Validate the override has required fields for its action type.
                    # A supervisor hallucination (e.g. long_press without coordinate)
                    # should be caught here rather than crashing the runner.
                    _ov_args = _override_tc.get("arguments", {})
                    _ov_type = _ov_args.get("action", "")
                    _ov_missing = _missing_required_fields(_ov_type, _ov_args)
                    if _ov_missing:
                        print(
                            f"[SUPERVISOR] override rejected — missing required "
                            f"fields {_ov_missing} for action {_ov_type!r}"
                        )
                    else:
                        action = _override_tc
                        action_parameter = action["arguments"]
                        # Update step tracking to reflect the override action,
                        # NOT the VLM's rejected proposal.  Without this the
                        # memory store and plan recorder learn the wrong
                        # action as a "success", and memory_confirmed_vlm
                        # later skips the supervisor for the same bad action.
                        _step_action_type = str(action_parameter.get("action", ""))
                        _step_action_args = copy.deepcopy(action_parameter)
                        # Track supervisor override action globally
                        _sup_action_sig = f"{_step_action_type}|{json.dumps(_step_action_args, ensure_ascii=False, sort_keys=True)}"
                        _executed_actions_global.add(_sup_action_sig)
                        _sup_note = (
                            f"Action: [SUPERVISOR OVERRIDE] {_reason}\n"
                            f"\n{json.dumps(_override_tc, ensure_ascii=False)}\n"
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
                # Cache approve on exact same state+action for short TTL.
                if _step_state_action_sig:
                    # Enforce a max-size cap to prevent unbounded growth in
                    # long-running sessions.  Evict expired entries first;
                    # if still above limit, evict the oldest (by expiry).
                    if len(_sup_approved_cache) >= _SUP_APPROVE_CACHE_MAX_SIZE:
                        _prune_now = time.time()
                        _prune_expired = [k for k, v in _sup_approved_cache.items()
                                          if v <= _prune_now]
                        for _k in _prune_expired:
                            del _sup_approved_cache[_k]
                        if len(_sup_approved_cache) >= _SUP_APPROVE_CACHE_MAX_SIZE:
                            _oldest = min(_sup_approved_cache,
                                          key=_sup_approved_cache.__getitem__)
                            del _sup_approved_cache[_oldest]
                    _sup_approved_cache[_step_state_action_sig] = (
                        time.time() + _SUP_APPROVE_CACHE_TTL_SECONDS
                    )

        # 4. Rescale coordinates from 1000x1000 to actual resolution
        img = Image.open(screenshot_path)
        resized_h, resized_w = smart_resize(
            img.height, img.width,
            factor=16,
            min_pixels=3136,
            max_pixels=1003520 * 200,
        )
        action_parameter = rescale_coordinates(action_parameter, resized_w, resized_h)

        # 5. Validate required fields before executing.
        # Truncated VLM outputs can parse as valid JSON but miss required
        # keys (e.g. click without coordinate).  Catch these early instead
        # of crashing with KeyError mid-execution.
        action_type = action_parameter.get("action", "")
        _missing = _missing_required_fields(action_type, action_parameter)
        if _missing:
            print(
                f"[WARN] action {action_type!r} missing required fields "
                f"{_missing} — skipping step (VLM output may be truncated)"
            )
            history.append({"output": output_text, "image": screenshot_path})
            _emit_step_summary("parse_failed")
            time.sleep(1)
            continue

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
                    "\n"
                    '{"name": "mobile_use", "arguments": {"action": "system_button", "button": "Back"}}\n'
                    ""
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
            print(f"\n[ACTION EXEC] === TYPE ACTION START ===")
            print(f"[ACTION EXEC] Text to type: {_text!r}")
            
            # Get UI state before typing
            _pre_ui_xml = adb_tools.get_ui_dump()
            _pre_text_count = _pre_ui_xml.count("<node") if _pre_ui_xml else 0
            print(f"[ACTION EXEC] Pre-type UI state: {_pre_text_count} nodes")
            
            _ok = adb_tools.type_with_verification(_text, retries=2)
            
            # Get UI state after typing for comparison
            _post_ui_xml = adb_tools.get_ui_dump()
            _post_text_count = _post_ui_xml.count("<node") if _post_ui_xml else 0
            print(f"[ACTION EXEC] Post-type UI state: {_post_text_count} nodes")
            
            if _ok:
                print("[ACTION EXEC] ✅ TYPE DONE (verified in UI)")
                
                # Show where text was found
                try:
                    import xml.etree.ElementTree as ET
                    root = ET.fromstring(_post_ui_xml)
                    found_nodes = []
                    for node in root.iter("node"):
                        node_text = node.attrib.get("text", "")
                        node_desc = node.attrib.get("content-desc", "")
                        if _text in node_text or _text in node_desc:
                            bounds = node.attrib.get("bounds", "")
                            found_nodes.append({
                                'text': node_text[:50],
                                'desc': node_desc[:50],
                                'bounds': bounds
                            })
                    
                    if found_nodes:
                        print(f"[ACTION EXEC] Text found in {len(found_nodes)} node(s):")
                        for i, n in enumerate(found_nodes[:3], 1):  # Show first 3
                            print(f"[ACTION EXEC]   Node {i}: text={n['text']!r}, desc={n['desc']!r}, bounds={n['bounds']}")
                except Exception as e:
                    print(f"[ACTION EXEC] Could not parse post-type UI: {e}")
            else:
                print("[ACTION EXEC] ❌ TYPE FAILED OR UNVERIFIED")
                print("[WARN] Input command may have succeeded but text was not observed in UI")
                
                # Debug: show current search bar content
                try:
                    import xml.etree.ElementTree as ET
                    root = ET.fromstring(_post_ui_xml)
                    search_fields = []
                    for node in root.iter("node"):
                        class_name = node.attrib.get("class", "").lower()
                        if "edit" in class_name or "input" in class_name or "search" in class_name.lower():
                            text = node.attrib.get("text", "")
                            hint = node.attrib.get("hint", "")
                            bounds = node.attrib.get("bounds", "")
                            if text or hint:
                                search_fields.append({
                                    'class': node.attrib.get("class", ""),
                                    'text': text[:80],
                                    'hint': hint[:80],
                                    'bounds': bounds
                                })
                    
                    if search_fields:
                        print(f"[ACTION EXEC DEBUG] Found {len(search_fields)} input/search field(s):")
                        for i, sf in enumerate(search_fields[:3], 1):
                            print(f"[ACTION EXEC DEBUG]   Field {i}: class={sf['class']}, text={sf['text']!r}, hint={sf['hint']!r}")
                except Exception as e:
                    print(f"[ACTION EXEC DEBUG] Could not analyze input fields: {e}")
            
            _step_metrics["action"] = time.time() - _t_action
            _log_t(f"[TIMING] action_type={_step_metrics['action']:.2f}s")
            print(f"[ACTION EXEC] === TYPE ACTION END (took {_step_metrics['action']:.2f}s) ===\n")

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
                    "\n"
                    '{"name": "mobile_use", "arguments": '
                    '{"action": "system_button", "button": "Home"}}\n'
                    ""
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
            # Treat terminate as task completion regardless of status value.
            # The VLM decided the task is done — emit the completion marker
            # that the wrapper (mobile_agent.py) recognises.  Without this,
            # status="unknown" causes the wrapper to report timeout.
            print("[TERMINATED] Task completed.")
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

        # 5b. Proactive task-completion check: when the supervisor overrides
        # an action because the VLM is going against the task goal, the task
        # may already be done.  Ask the supervisor if we should just finish.
        if (supervisor is not None
                and _supervisor_feedback
                and _goal_conflict_in_feedback(_supervisor_feedback)):
            try:
                _completion = supervisor.is_task_complete(
                    task=instruction,
                    fg_label=_fg_label,
                    ui_summary=_ui_summary,
                    history=history,
                    conclusion=_action_text[:500],
                    screenshot_path=str(screenshot_path),
                )
                if _completion.get("complete", False):
                    print("[SUPERVISOR] task confirmed complete after goal-conflict override")
                    print("[TERMINATED] Task completed.")
                    termination_reason = "proactive_completion_check"
                    _emit_step_summary("answer_confirmed_complete")
                    time.sleep(1)
                    break
            except Exception:
                pass

        # 6. Record history and annotate screenshot
        history.append({"output": output_text, "image": screenshot_path})
        annotate_screenshot(
            screenshot_path,
            action_parameter,
            os.path.join(anno_dir, f"screenshot_anno_{step_id}.png"),
        )

        # 6b. Record step for plan building (only for LLM-driven steps,
        #     not plan-replay steps which are already in the stored plan).
        #     Also captures a post-action UI fingerprint for future replay verification.
        if _plan_executor is not None and not _plan_step_executed:
            try:
                _post_pkg = adb_tools.get_foreground_package() or ""
                # Take a post-action UI dump and build fingerprint for plan verification
                _post_action_ui_fp = ""
                try:
                    _post_xml = adb_tools.get_ui_dump()
                    _post_summary = summarise_ui_dump(_post_xml)
                    _post_action_ui_fp = build_ui_fingerprint(_post_pkg, _post_summary)
                except Exception:
                    pass
                # Capture target element signature for click actions so
                # plan replay can use element-based targeting later.
                # IMPORTANT: use action_parameter (already rescaled to actual
                # pixels at line ~1412) NOT _step_action_args (normalised
                # 0-1000 coords).  Passing normalised coords to
                # find_element_at_coordinates causes it to fall back to the
                # nearest element (often the root FrameLayout), producing a
                # useless signature that never matches during replay.
                _plan_target_el: dict | None = None
                if _step_action_type == "click" and _ui_xml:
                    try:
                        _coord = action_parameter.get("coordinate", [0, 0])
                        if len(_coord) >= 2:
                            _plan_target_el = find_element_at_coordinates(
                                _ui_xml, int(_coord[0]), int(_coord[1]),
                            )
                    except Exception:
                        pass

                _plan_executor.record_step(
                    step_index=step_id,
                    action_type=_step_action_type,
                    action_args=dict(_step_action_args),
                    pre_action_pkg=_fg_pkg or "",
                    post_action_pkg=_post_pkg,
                    action_description=_step_action_description[:200] if _step_action_description else "",
                    post_action_ui_fp=_post_action_ui_fp,
                    pre_action_ui_fp=_step_ui_fp,
                    target_element_signature=_plan_target_el,
                )
            except Exception:
                pass

        # 6c. If plan replay was paused and VLM just handled a step,
        #     try to resume replay for the remaining steps.
        if (_plan_executor is not None
                and not _plan_executor.is_replaying()
                and _plan_executor._paused
                and _plan_replay_active):
            _plan_executor.resume_replay()
            if _plan_executor.is_replaying():
                _log_t("[PLAN] replay resumed after VLM-handled step")

        _emit_step_summary("completed")
        _log_t(f"[STEP END] step={step_id} total={time.time() - _step_t0:.2f}s")
        time.sleep(2)
    else:
        termination_reason = f"max_steps_reached ({args.max_steps})"
        print(f"[TERMINATED] Reached max_steps={args.max_steps} without explicit completion.")

    print(f"[TERMINATION REASON] {termination_reason}")

    # ── Cache-Hit Summary ──────────────────────────────────────────────
    if _perf_steps > 0:
        _cache_total = _cache_fastpath_steps + _cache_plan_replay_steps + _cache_vlm_normal_steps
        _cache_hit_total = _cache_fastpath_steps + _cache_plan_replay_steps
        _cache_hit_pct = (_cache_hit_total / max(_cache_total, 1)) * 100.0
        _bar_width = 40
        _hit_bars = int(_bar_width * _cache_hit_pct / 100)
        _miss_bars = _bar_width - _hit_bars

        print()
        print("╔" + "═" * 58 + "╗")
        print("║" + "  📊 CACHE-HIT SUMMARY".ljust(58) + "║")
        print("╠" + "═" * 58 + "╣")
        print(f"║  Plan-replay steps (VLM skipped):   {_cache_plan_replay_steps:>3d}".ljust(58) + "║")
        print(f"║  Memory-fastpath steps (VLM skipped):{_cache_fastpath_steps:>3d}".ljust(58) + "║")
        print(f"║  Normal VLM steps:                  {_cache_vlm_normal_steps:>3d}".ljust(58) + "║")
        print(f"║  ─────────────────────────────────".ljust(58) + "║")
        print(f"║  Total steps:                       {_cache_total:>3d}".ljust(58) + "║")
        print(f"║  Cache HIT:  {_cache_hit_pct:5.1f}%  [{'█' * _hit_bars}{'░' * _miss_bars}]".ljust(58) + "║")
        print(f"║  Cache MISS: {100.0 - _cache_hit_pct:5.1f}%  VLM calls: {_cache_vlm_normal_steps}".ljust(58) + "║")
        print("╚" + "═" * 58 + "╝")
        print()

    if _perf_steps > 0:
        _avg_vlm = _perf_vlm_total / _perf_steps
        _avg_sup = _perf_supervisor_total / _perf_steps
        _den_total = max(_perf_vlm_total + _perf_supervisor_total, 1e-9)
        _sup_share_total = (_perf_supervisor_total / _den_total) * 100.0
        _log_t(
            "[TIMING SUMMARY] "
            f"steps={_perf_steps} | llm_total={_perf_vlm_total:.2f}s "
            f"(primary={_perf_vlm_primary_total:.2f}s, fallback={_perf_vlm_fallback_total:.2f}s) | "
            f"supervisor_total={_perf_supervisor_total:.2f}s | "
            f"avg_llm_per_step={_avg_vlm:.2f}s | avg_supervisor_per_step={_avg_sup:.2f}s | "
            f"supervisor_share_total={_sup_share_total:.1f}%"
        )
    print("\n[DONE] Agent execution finished.")

    # ------------------------------------------------------------------
    # Plan recording / counter update at run end
    # ------------------------------------------------------------------
    _run_succeeded = termination_reason in (
        "answer_confirmed_complete",
        "plan_replay_complete",
    ) or termination_reason.startswith("terminate_action")

    if _plan_executor is not None:
        try:
            if _plan_replay_active and _run_succeeded:
                # Plan replay completed all steps successfully
                _plan_executor.note_plan_success(_canonical_intent_key)
                _log_t(f"[PLAN] replay succeeded — incremented success counter for {_canonical_intent_key}")
            elif _plan_replay_active and not _run_succeeded:
                # Plan replay failed mid-way
                _plan_executor.note_plan_failure(_canonical_intent_key)
                _log_t(f"[PLAN] replay failed — incremented fail counter for {_canonical_intent_key}")
            elif not _plan_replay_active and _run_succeeded:
                # LLM-driven run succeeded — record a new plan for future replay
                _new_plan = _plan_executor.build_and_store_plan(
                    intent_key=_canonical_intent_key,
                    instruction=instruction,
                    run_id=_run_id,
                    device_bucket="default",
                )
                if _new_plan:
                    _log_t(
                        f"[PLAN] recorded new plan: intent_key={_canonical_intent_key} "
                        f"steps={len(_new_plan.steps)}"
                    )
        except Exception as _plan_end_err:
            _log_t(f"[PLAN] end-of-run error ({_plan_end_err!r})")

    # Clean up screenshot directories after task ends
    _cleanup()
    print("[CLEANUP] Screenshot directories removed.")


if __name__ == "__main__":
    main()
