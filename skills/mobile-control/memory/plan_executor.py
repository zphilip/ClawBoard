"""Plan recording and replay engine for mobile-control.

Responsibilities:
  1. **Record**: After a successful LLM-driven run, collect the executed
     (action_type, action_args, foreground_pkg_before, foreground_pkg_after)
     tuples into a TaskPlan and persist it via PlanStore.

  2. **Replay**: Given a plan, execute each step via ADB, verifying that
     the foreground package after each step matches the expected package.
     On verification failure, signal the runner to fall back to the VLM
     for that step only, then attempt to resume the plan.

This module is intentionally **decoupled from the VLM / supervisor**.
It only knows about ADB actions (through the ``adb_tools`` interface)
and plan storage (through ``PlanStore``).

Usage in the runner (pseudo-code)::

    executor = PlanExecutor(plan_store, adb_tools)

    # At run start:
    plan = executor.find_plan(intent_key)
    if plan:
        executor.start_replay(plan)

    # Inside the step loop:
    if executor.is_replaying():
        step = executor.next_step()
        if step:
            ok = executor.execute_and_verify(step)
            if ok:
                # step done, continue to next step
                ...
            else:
                # verification failed — fall back to VLM for this step
                executor.pause_replay()
                ...
        else:
            # plan exhausted — fall through to normal VLM path
            executor.end_replay()

    # At run end (success):
    executor.record_plan(intent_key, instruction, collected_steps, run_id)
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .models import PlanStep, TaskPlan
from .plan_store import PlanStore

# Lazy import to avoid circular dependency at module load time.
_find_matching_element = None


def _get_find_matching_element():
    global _find_matching_element
    if _find_matching_element is None:
        from utils import find_matching_element as _fme
        _find_matching_element = _fme
    return _find_matching_element

if TYPE_CHECKING:
    # AdbTools is imported at runtime; the type hint is only for IDE support.
    from utils import AdbTools  # pragma: no cover


def _compute_screen_hash(screenshot_path: str) -> str:
    """Return a hex-encoded 8×8 perceptual hash of *screenshot_path*.

    The hash is ~64 hex chars and captures the coarse visual structure
    of the screen.  Two screenshots of the same app state (same screen,
    different ads/recommendations) produce similar hashes; two different
    screens produce very different hashes.
    """
    try:
        from PIL import Image
        import numpy as np

        img = Image.open(screenshot_path).convert("L").resize((8, 8))
        arr = np.array(img, dtype=np.uint8)
        median = np.median(arr)
        bits = (arr > median).flatten()
        # Pack 8 bits per byte → hex string
        packed = bytearray(
            sum(int(b) << (7 - j) for j, b in enumerate(bits[i:i + 8]))
            for i in range(0, 64, 8)
        )
        return packed.hex()
    except Exception:
        return ""


def _screen_hashes_similar(h1: str, h2: str, max_distance: int = 8) -> tuple[bool, int]:
    """Return (is_similar, hamming_distance) for two screen hashes."""
    if not h1 or not h2 or len(h1) != len(h2):
        return (False, 99)
    try:
        b1 = bytes.fromhex(h1)
        b2 = bytes.fromhex(h2)
        dist = sum(bin(a ^ b).count("1") for a, b in zip(b1, b2))
        return (dist <= max_distance, dist)
    except Exception:
        return (False, 99)


def _template_match_crop(
    screenshot_path: str, crop_b64: str, confidence_threshold: float = 0.60,
) -> tuple[int, int] | None:
    """Try to locate *crop_b64* in *screenshot_path* via template matching.

    Returns (centre_x, centre_y) in actual screen pixels when the best match
    exceeds *confidence_threshold*, or None when no reliable match is found.

    Used as a fallback when element-based targeting fails — the crop is a
    small PNG region around the click point captured during plan recording.
    Works on WebView/canvas content that is invisible to uiautomator.
    """
    try:
        import base64
        import cv2
        import numpy as np

        _crop_bytes = base64.b64decode(crop_b64)
        _crop_arr = np.frombuffer(_crop_bytes, np.uint8)
        crop = cv2.imdecode(_crop_arr, cv2.IMREAD_COLOR)
        if crop is None:
            return None

        screen = cv2.imread(screenshot_path)
        if screen is None:
            return None

        # Crop must be smaller than screen in both dimensions
        if crop.shape[0] >= screen.shape[0] or crop.shape[1] >= screen.shape[1]:
            return None

        result = cv2.matchTemplate(screen, crop, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)

        if max_val < confidence_threshold:
            print(f"[PLAN] crop match failed: best confidence={max_val:.3f} "
                  f"< threshold={confidence_threshold} "
                  f"(crop={crop.shape[1]}x{crop.shape[0]}px)")
            return None

        h, w = crop.shape[:2]
        cx = max_loc[0] + w // 2
        cy = max_loc[1] + h // 2
        print(f"[PLAN] crop match confidence={max_val:.3f} at ({cx},{cy})")
        return (cx, cy)
    except Exception as _e:
        print(f"[PLAN] crop match error: {_e}")
        return None


# Actions that are safe to replay blindly.  Excludes passive / terminal /
# user-dependent actions (same set as policy.NON_CACHEABLE_ACTIONS).
_REPLAYABLE_ACTION_TYPES: frozenset[str] = frozenset({
    "click",
    "type",
    "open",
    "system_button",
    "key",
    "swipe",
    "scroll",
    "long_press",
})

# How long (seconds) to wait after executing a step before checking the
# foreground package for verification.
_POST_ACTION_SETTLE_SECONDS: float = 1.5

# Extra settle time for type actions — the app needs time to show
# search suggestions / autocomplete results after text is entered.
_POST_TYPE_SETTLE_SECONDS: float = 3.0

# Maximum consecutive plan-step failures before giving up on replay.
_MAX_CONSECUTIVE_FAILURES: int = 2

# Minimum UI change required after a type action (new interactive elements).
# If fewer than this many new elements appear, the type is considered unverified.
_MIN_NEW_UI_ELEMENTS_AFTER_TYPE: int = 2


class RecordedStep:
    """Transient container for a step executed during the current run.

    The runner populates these as it goes; at the end of a successful run
    they are converted into ``PlanStep`` objects and stored.
    """

    __slots__ = (
        "step_index", "action_type", "action_args",
        "pre_action_pkg", "post_action_pkg", "action_description",
        "post_action_ui_fp", "pre_action_ui_fp", "target_element_signature",
        "crop_b64", "screen_hash",
    )

    def __init__(
        self,
        step_index: int,
        action_type: str,
        action_args: dict[str, Any],
        pre_action_pkg: str = "",
        post_action_pkg: str = "",
        action_description: str = "",
        post_action_ui_fp: str = "",
        pre_action_ui_fp: str = "",
        target_element_signature: dict[str, Any] | None = None,
        crop_b64: str = "",
        screen_hash: str = "",
    ):
        self.step_index = step_index
        self.action_type = action_type
        self.action_args = action_args
        self.pre_action_pkg = pre_action_pkg
        self.post_action_pkg = post_action_pkg
        self.action_description = action_description
        self.post_action_ui_fp = post_action_ui_fp
        self.pre_action_ui_fp = pre_action_ui_fp
        self.target_element_signature = target_element_signature
        self.crop_b64 = crop_b64
        self.screen_hash = screen_hash


def _step_is_duplicate(prev: "PlanStep", rec: "RecordedStep") -> bool:
    """Return True if *rec* is identical to *prev* and should be skipped.

    Compares action_type and the semantically meaningful parts of
    action_args, ignoring jitter in coordinates for click actions.
    """
    if prev.action_type != rec.action_type:
        return False
    if prev.action_type == "type":
        return prev.action_args.get("text") == rec.action_args.get("text")
    if prev.action_type in ("open", "system_button", "key"):
        return prev.action_args == rec.action_args
    # For click / long_press / swipe — compare bucketed coords so jitter
    # (±10-20 units) on the same button collapses.
    return _bucketed_action_sig(prev.action_type, prev.action_args) == \
           _bucketed_action_sig(rec.action_type, rec.action_args)


def _bucketed_action_sig(action_type: str, action_args: dict) -> str:
    """Coarse action signature with bucketed coordinates (imported lazily)."""
    from runner.coords import bucketed_action_sig
    return bucketed_action_sig(action_type, action_args)


class PlanExecutor:
    """Manages plan lookup, step-by-step replay, and plan recording."""

    def __init__(
        self,
        store: PlanStore,
        adb_tools: Any,  # AdbTools — duck-typed to avoid circular import
        settle_seconds: float = _POST_ACTION_SETTLE_SECONDS,
        ui_summariser: Any = None,  # callable(xml) -> summary string (optional)
        ui_fp_builder: Any = None,  # callable(pkg, summary) -> fp string (optional)
        screenshot_dir: str | Path = "",  # where to save post-action screenshots
        verify_mode: str = "crop",  # "crop" or "crop+hash"
    ):
        self.store = store
        self.adb = adb_tools
        self.settle_seconds = settle_seconds
        self._ui_summariser = ui_summariser
        self._ui_fp_builder = ui_fp_builder
        self._screenshot_dir = Path(screenshot_dir) if screenshot_dir else None
        self._verify_mode = verify_mode

        # Replay state (reset per run)
        self._replay_plan: TaskPlan | None = None
        self._replay_cursor: int = 0          # next step index to execute
        self._replay_active: bool = False
        self._consecutive_failures: int = 0
        self._paused: bool = False
        self._failed_step_indices: set[int] = set()  # steps that failed during replay

        # Recording state (populated during the run)
        self._recorded_steps: list[RecordedStep] = []

        # Last verification detail (for logging / debugging)
        self.last_verify_detail: str = ""

    # ------------------------------------------------------------------
    # Plan lookup
    # ------------------------------------------------------------------

    def find_plan(self, intent_key: str) -> TaskPlan | None:
        """Look up the best stored plan for *intent_key*.

        Returns None when no plan exists or the best plan has a low score.
        """
        plans = self.find_plans(intent_key)
        return plans[0] if plans else None

    def find_plans(self, intent_key: str) -> list[TaskPlan]:
        """Return all healthy plans for *intent_key*, sorted best-first.

        Multi-route: the same task may have multiple UI paths (e.g.
        with vs without a popup).  All are returned so the executor
        can try each one in order.
        """
        candidates = self.store.find_all_healthy(intent_key, min_success=1)
        return [
            p for p in candidates
            if (p.success_count / max(p.success_count + p.fail_count, 1)) >= 0.6
        ]

    def try_replay_plans(
        self, intent_key: str, max_tries: int = 3,
    ) -> bool:
        """Try to start replaying any healthy plan for *intent_key*.

        Attempts up to *max_tries* plans in score order.  Returns True
        if a plan was loaded and replay started, False if no viable
        plan found.

        When multiple routes exist for the same task (e.g. the app had
        a popup in one run but not another), this picks the first route
        that matches the current screen state.
        """
        _candidates = self.find_plans(intent_key)
        if not _candidates:
            return False

        _tried = 0
        for _plan in _candidates:
            if _tried >= max_tries:
                break
            _tried += 1
            print(
                f"[PLAN] trying route #{_tried}/"
                f"{min(len(_candidates), max_tries)} "
                f"(score={_plan.success_count / max(_plan.success_count + _plan.fail_count, 1):.2f}, "
                f"{len(_plan.steps)} steps)"
            )
            self.start_replay(_plan)
            return True

        return False

    # ------------------------------------------------------------------
    # Replay control
    # ------------------------------------------------------------------

    def start_replay(self, plan: TaskPlan) -> None:
        """Begin replaying *plan* from step 0."""
        self._replay_plan = plan
        self._replay_cursor = 0
        self._replay_active = True
        self._consecutive_failures = 0
        self._paused = False
        self._failed_step_indices.clear()

        # Warn if any step has accumulated failures — the plan may need
        # self-healing via upsert on the next successful VLM-driven run.
        if hasattr(self, 'store'):
            from .plan_store import plan_is_healthy
            if not plan_is_healthy(plan):
                _bad_steps = [
                    f"s{s.step_index}(s{s.success_count}/f{s.fail_count})"
                    for s in plan.steps if s.fail_count > 0
                ]
                print(
                    f"[PLAN] ⚠️ replaying unhealthy plan: "
                    f"{', '.join(_bad_steps)}"
                )

    def is_replaying(self) -> bool:
        return self._replay_active and not self._paused

    def pause_replay(self) -> None:
        """Temporarily pause replay (e.g. to let VLM handle one step)."""
        self._paused = True

    def resume_replay(self) -> None:
        """Resume replay after a VLM-handled step.

        Advances the cursor past the step that failed (the VLM handled it),
        then scans forward to find the next step whose pre_action_pkg
        matches the current foreground package.  If the VLM got us to a
        different screen than the plan expected, we skip ahead to the
        right position instead of retrying the same failing step forever.
        """
        if not self._paused or self._replay_plan is None:
            return
        # Advance past the failed step — the VLM already handled it.
        self._replay_cursor += 1
        if self._replay_cursor >= len(self._replay_plan.steps):
            self.end_replay()
            return
        # Find the next step that matches our current screen.
        try:
            current_pkg = self.adb.get_foreground_package() or ""
        except Exception:
            current_pkg = ""
        if current_pkg:
            for i in range(self._replay_cursor, len(self._replay_plan.steps)):
                step = self._replay_plan.steps[i]
                if step.pre_action_pkg and step.pre_action_pkg == current_pkg:
                    self._replay_cursor = i
                    break
            else:
                # No step matches the current screen — abandon replay.
                self.end_replay()
                return
        self._paused = False
        self._consecutive_failures = 0

    def end_replay(self) -> None:
        """Stop replay entirely (plan exhausted or abandoned)."""
        self._replay_active = False
        self._paused = False

    def trim_plan_to(self, step_index: int) -> None:
        """Truncate the current replay plan to *step_index* steps.

        Called when a completion check detects the task is already done
        before reaching the plan's original end — the trailing steps were
        unnecessary and should be pruned so future replays are shorter.
        """
        if self._replay_plan is None:
            return
        _steps = self._replay_plan.steps
        # Convert step_index to plan steps (step_index may include non-replayable steps)
        # Find the position of the last executed step in the plan
        _cut_at = 0
        for i, s in enumerate(_steps):
            if s.step_index <= step_index:
                _cut_at = i + 1
            else:
                break
        if _cut_at > 0 and _cut_at < len(_steps):
            _old_len = len(_steps)
            self._replay_plan.steps = _steps[:_cut_at]
            # Persist the shorter plan immediately via upsert
            try:
                self.store.upsert_by_intent(self._replay_plan)
            except Exception:
                pass
            print(
                f"[PLAN] trimmed from {_old_len} to {_cut_at} steps "
                f"(task was complete at step {step_index})"
            )

    @property
    def replay_plan(self) -> TaskPlan | None:
        return self._replay_plan

    @property
    def replay_cursor(self) -> int:
        return self._replay_cursor

    # ------------------------------------------------------------------
    # Step execution
    # ------------------------------------------------------------------

    def next_step(self) -> PlanStep | None:
        """Return the next plan step to execute, or None if plan is exhausted."""
        if not self._replay_plan or self._replay_cursor >= len(self._replay_plan.steps):
            return None
        return self._replay_plan.steps[self._replay_cursor]

    def execute_and_verify(self, step: PlanStep, screenshot_path: str = "") -> bool:
        """Execute one plan step via ADB and verify the result.

        For click actions with a ``target_element_signature``, the
        executor attempts to locate the same UI element on the current
        screen (by resource-id, then text, then content-desc) and
        clicks its centre.  This makes click steps resilient to layout
        drift between runs.

        Verification layers:
          0. Pre-execution foreground-package check (NEW — skips the
             step early if we are on the wrong screen)
          1. Foreground package check
          2. (click with element match) element-found confirmation
          3. UI fingerprint comparison (skipped for Home/Back/open,
             AND for clicks that matched their target element)
        """
        if step.action_type not in _REPLAYABLE_ACTION_TYPES:
            return False

        # ── Layer 0: pre-execution screen check ─────────────────────────
        # Skip the step immediately if the current foreground package does
        # not match the expected pre-action package.  This catches the
        # common case where a plan lacks an initial ``open`` step and the
        # device is on the launcher instead of the target app.
        #
        # Not applied to ``open`` (it IS the corrective action) or
        # ``system_button`` (Home/Back navigate anywhere intentionally).
        if step.pre_action_pkg and step.action_type not in ("open", "system_button"):
            try:
                _pre_pkg = self.adb.get_foreground_package() or ""
            except Exception:
                _pre_pkg = ""
            if _pre_pkg and _pre_pkg != step.pre_action_pkg:
                print(
                    f"[PLAN] pre-check failed: expected {step.pre_action_pkg!r} "
                    f"but foreground is {_pre_pkg!r} — skipping step"
                )
                self._failed_step_indices.add(step.step_index)
                self._consecutive_failures += 1
                if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    self.end_replay()
                return False

        # ── Element-based targeting for clicks ────────────────────────
        _element_matched = False
        _crop_matched = False  # True when crop matching (not element) succeeded
        _had_element_sig = (
            step.action_type == "click"
            and step.target_element_signature is not None
        )
        if _had_element_sig:
            try:
                _ui_xml = self.adb.get_ui_dump()
                _fme = _get_find_matching_element()
                _current_el = _fme(step.target_element_signature, _ui_xml)
                if _current_el:
                    _bounds = _current_el["bounds"]
                    _cx = (_bounds[0] + _bounds[2]) // 2
                    _cy = (_bounds[1] + _bounds[3]) // 2
                    step.action_args["coordinate"] = [_cx, _cy]
                    _element_matched = True
                    print(
                        f"[PLAN] element match: "
                        f"id={_current_el.get('resource_id','')} "
                        f"text={_current_el.get('text','')[:40]} "
                        f"centre=({_cx},{_cy})"
                    )
                else:
                    print(
                        "[PLAN] element NOT found on current screen "
                        "— trying crop match as fallback"
                    )
            except Exception as _el_err:
                print(f"[PLAN] element lookup error ({_el_err}) — trying crop match")
        # ── Pre-action screen state check ───────────────────────────
        # Only active with --plan-verify crop+hash.  Verifies the
        # current screen is visually similar to the recorded pre-click
        # screen before attempting crop matching.
        _screen_state_ok = True
        if (self._verify_mode == "crop+hash"
                and not _element_matched
                and step.action_type == "click"
                and screenshot_path
                and getattr(step, 'pre_action_screen_hash', '')):
            _cur_hash = _compute_screen_hash(screenshot_path)
            if _cur_hash:
                _hash_ok, _hash_dist = _screen_hashes_similar(
                    _cur_hash, step.pre_action_screen_hash,
                )
                if not _hash_ok:
                    _screen_state_ok = False
                    print(
                        f"[PLAN] pre-action screen hash mismatch "
                        f"(dist={_hash_dist}/64) — screen has changed, "
                        f"skipping step"
                    )
                else:
                    print(
                        f"[PLAN] pre-action screen hash match "
                        f"(dist={_hash_dist}/64)"
                    )

        # ── Screenshot crop template matching ────────────────────────
        # Runs when element matching was skipped (no sig) or failed.
        # Works on WebView/canvas content invisible to uiautomator.
        if (_screen_state_ok
                and not _element_matched
                and step.action_type == "click"
                and screenshot_path):
            _crop = getattr(step, 'target_element_crop_b64', '')
            if _crop:
                _tm = _template_match_crop(screenshot_path, _crop)
                if _tm is not None:
                    step.action_args["coordinate"] = [_tm[0], _tm[1]]
                    _element_matched = True
                    _crop_matched = True
                    print(
                        f"[PLAN] crop match at ({_tm[0]},{_tm[1]}) "
                        f"— template matching succeeded"
                    )
        # ── If element sig existed but both layers failed → hard fail ──
        if _had_element_sig and not _element_matched:
            print(
                "[PLAN] step FAILED — element not found + no crop match "
                "(screen has changed, stored coords are stale)"
            )
            self._failed_step_indices.add(step.step_index)
            self._consecutive_failures += 1
            if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                self.end_replay()
            return False
        # ── If screen hash check failed → screen has changed, FAIL ──
        if not _screen_state_ok:
            print(
                "[PLAN] step FAILED — pre-action screen hash mismatch "
                "(screen has visually changed)"
            )
            self._failed_step_indices.add(step.step_index)
            self._consecutive_failures += 1
            if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                self.end_replay()
            return False

        try:
            ok = self._execute_action(step)
        except Exception:
            ok = False

        if not ok:
            self._failed_step_indices.add(step.step_index)
            self._consecutive_failures += 1
            if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                self.end_replay()
            return False

        # Settle time — type actions need extra wait for search results
        settle = self.settle_seconds
        if step.action_type == "type":
            settle = max(settle, _POST_TYPE_SETTLE_SECONDS)
        time.sleep(settle)

        # Verification layer 1: check foreground package
        try:
            actual_pkg = self.adb.get_foreground_package() or ""
        except Exception:
            actual_pkg = ""

        pkg_ok = True
        if step.expected_pkg and actual_pkg:
            if actual_pkg != step.expected_pkg:
                pkg_ok = False

        if not pkg_ok:
            self._failed_step_indices.add(step.step_index)
            self._consecutive_failures += 1
            if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                self.end_replay()
            return False

        # Verification layer 2: post-action screenshot (debugging)
        if self._screenshot_dir is not None:
            try:
                self._screenshot_dir.mkdir(parents=True, exist_ok=True)
                _ts = int(time.time() * 1000)
                self.adb.get_screenshot(
                    str(self._screenshot_dir
                        / f"plan_step_{step.step_index}_{_ts}.png")
                )
            except Exception:
                pass

        # Verification layer 3: UI fingerprint comparison.
        # SKIP for:
        #   - Home/Back           (launcher screens differ every run)
        #   - open                (post-launch ads / splash screens differ)
        #   - element/crop match  (pre-action check already verified)
        ui_fp_ok = True
        _skip_fp_check = (
            _element_matched  # both element + crop match skip
            or step.action_type in ("open", "type")
            or (
                step.action_type == "system_button"
                and step.action_args.get("button") in ("Home", "Back")
            )
        )
        if not _skip_fp_check and step.post_action_ui_fp and self._ui_summariser and self._ui_fp_builder:
            try:
                _post_xml = self.adb.get_ui_dump()
                _post_summary = self._ui_summariser(_post_xml)
                _post_fp = self._ui_fp_builder(actual_pkg, _post_summary)
                if _post_fp != step.post_action_ui_fp:
                    ui_fp_ok = False
            except Exception:
                ui_fp_ok = False

        # For type actions: extra check — did new interactive elements appear?
        type_ok = True
        if step.action_type == "type" and self._ui_summariser:
            try:
                _post_xml = self.adb.get_ui_dump()
                _post_summary = self._ui_summariser(_post_xml)
                _typed_text = step.action_args.get("text", "")
                _new_lines = [l for l in (_post_summary or "").splitlines() if l.strip()]
                _non_text_elements = [
                    l for l in _new_lines
                    if _typed_text.lower() not in l.lower()
                ]
                if len(_non_text_elements) < _MIN_NEW_UI_ELEMENTS_AFTER_TYPE:
                    type_ok = False
            except Exception:
                pass

        verified = pkg_ok and ui_fp_ok and type_ok

        # Build detail string for logging
        _parts = []
        if not pkg_ok:
            _parts.append(f"pkg_mismatch(expected={step.expected_pkg}, actual={actual_pkg})")
        if not ui_fp_ok:
            _parts.append("ui_fp_mismatch")
        if not type_ok:
            _parts.append("type_no_search_results")
        if _element_matched:
            _parts.append("element_matched")
        self.last_verify_detail = "; ".join(_parts) if _parts else "all_passed"

        if verified:
            self._replay_cursor += 1
            self._consecutive_failures = 0
            return True
        else:
            self._failed_step_indices.add(step.step_index)
            self._consecutive_failures += 1
            if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                self.end_replay()
            return False

    # ------------------------------------------------------------------
    # Action dispatch (minimal — reuses ADB directly)
    # ------------------------------------------------------------------

    def _execute_action(self, step: PlanStep) -> bool:
        """Execute a single action via ADB.  Returns True on success."""
        atype = step.action_type
        args = step.action_args

        if atype == "click":
            coord = args.get("coordinate")
            if not coord or len(coord) != 2:
                return False
            return bool(self.adb.click(int(coord[0]), int(coord[1])))

        if atype == "long_press":
            coord = args.get("coordinate")
            if not coord or len(coord) != 2:
                return False
            return bool(self.adb.long_press(int(coord[0]), int(coord[1])))

        if atype == "type":
            text = str(args.get("text", ""))
            if not text:
                return False
            return bool(self.adb.type_with_verification(text, retries=2))

        if atype in ("swipe", "scroll"):
            c1 = args.get("coordinate")
            c2 = args.get("coordinate2")
            if not c1 or not c2:
                return False
            return bool(self.adb.slide(
                int(c1[0]), int(c1[1]), int(c2[0]), int(c2[1]),
            ))

        if atype == "system_button":
            button = args.get("button", "")
            if button == "Home":
                return bool(self.adb.home())
            if button == "Back":
                return bool(self.adb.back())
            return False

        if atype == "key":
            keycode = args.get("keycode") or args.get("key", "")
            if not keycode:
                return False
            return bool(self.adb._run_safe(f"shell input keyevent {keycode}"))

        if atype == "open":
            # Delegate to the runner's handle_open_action via a simple
            # monkey-patch pattern — the runner must set self._open_handler.
            handler = getattr(self, "_open_handler", None)
            if handler:
                return bool(handler(args))
            return False

        return False

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_step(
        self,
        step_index: int,
        action_type: str,
        action_args: dict[str, Any],
        pre_action_pkg: str = "",
        post_action_pkg: str = "",
        action_description: str = "",
        post_action_ui_fp: str = "",
        pre_action_ui_fp: str = "",
        target_element_signature: dict[str, Any] | None = None,
        crop_b64: str = "",
        screen_hash: str = "",
    ) -> None:
        """Record a step executed during the current run for later plan creation."""
        if action_type not in _REPLAYABLE_ACTION_TYPES:
            return
        self._recorded_steps.append(RecordedStep(
            step_index=step_index,
            action_type=action_type,
            action_args=dict(action_args),
            pre_action_pkg=pre_action_pkg,
            post_action_pkg=post_action_pkg,
            action_description=action_description,
            post_action_ui_fp=post_action_ui_fp,
            pre_action_ui_fp=pre_action_ui_fp,
            target_element_signature=target_element_signature,
            crop_b64=crop_b64,
            screen_hash=screen_hash,
        ))

    def repair_and_store_plan(
        self,
        intent_key: str,
        instruction: str,
        run_id: str,
        device_bucket: str = "default",
    ) -> TaskPlan | None:
        """Repair a plan that had step failures during replay.

        Merges the successfully-replayed plan steps with VLM-recorded
        replacement steps so the plan incrementally improves instead of
        being replaced wholesale (which loses counter history).

        - Steps that replayed OK → kept, success_count incremented.
        - Steps that failed → replaced with the corresponding VLM step,
          old step fail_count incremented.
        - Additional VLM steps → appended.
        """
        if self._replay_plan is None or not self._recorded_steps:
            # Fall back to building a fresh plan from VLM steps
            return self.build_and_store_plan(
                intent_key, instruction, run_id, device_bucket,
            )

        _old_steps = list(self._replay_plan.steps)
        _failed = self._failed_step_indices
        _vlm_steps = self._recorded_steps

        # Build the repaired step list
        _repaired: list[PlanStep] = []
        _vlm_idx = 0  # index into _vlm_steps for replacement steps

        for _old_step in _old_steps:
            if _old_step.step_index in _failed:
                # This step failed — replace with VLM's version.
                # Create a fresh step with the VLM's action; counters
                # start at 0 — _merge_step_counters in upsert_by_intent
                # will carry forward matching old counters.
                if _vlm_idx < len(_vlm_steps):
                    _rec = _vlm_steps[_vlm_idx]
                    _vlm_idx += 1
                    _repaired.append(PlanStep(
                        step_index=len(_repaired) + 1,
                        action_type=_rec.action_type,
                        action_args=dict(_rec.action_args),
                        expected_pkg=_rec.post_action_pkg,
                        action_description=_rec.action_description,
                        pre_action_pkg=_rec.pre_action_pkg,
                        post_action_ui_fp=_rec.post_action_ui_fp,
                        target_element_signature=_rec.target_element_signature,
                        target_element_crop_b64=getattr(_rec, 'crop_b64', ''),
                        pre_action_screen_hash=getattr(_rec, 'screen_hash', ''),
                        success_count=0,
                        fail_count=0,
                    ))
                # If no VLM step available, just drop the failed step
            else:
                # This step replayed OK — keep it with a FRESH PlanStep
                # carrying ONLY this run's +1 success.  _merge_step_counters
                # in upsert_by_intent adds the stored history on top.
                _repaired.append(PlanStep(
                    step_index=len(_repaired) + 1,
                    action_type=_old_step.action_type,
                    action_args=dict(_old_step.action_args),
                    expected_pkg=_old_step.expected_pkg,
                    action_description=_old_step.action_description,
                    pre_action_pkg=_old_step.pre_action_pkg,
                    post_action_ui_fp=_old_step.post_action_ui_fp,
                    target_element_signature=_old_step.target_element_signature,
                    target_element_crop_b64=getattr(_old_step, 'target_element_crop_b64', ''),
                    pre_action_screen_hash=getattr(_old_step, 'pre_action_screen_hash', ''),
                    success_count=1,
                    fail_count=0,
                ))

        # Append any remaining VLM steps (beyond plan scope)
        while _vlm_idx < len(_vlm_steps):
            _rec = _vlm_steps[_vlm_idx]
            _vlm_idx += 1
            # Skip no-effect steps (same pre/post UI fingerprint)
            if (_rec.action_type != "type"
                    and _rec.pre_action_ui_fp
                    and _rec.post_action_ui_fp
                    and _rec.pre_action_ui_fp == _rec.post_action_ui_fp):
                continue
            # Skip duplicate consecutive steps
            if _repaired and _step_is_duplicate(_repaired[-1], _rec):
                continue
            _repaired.append(PlanStep(
                step_index=len(_repaired) + 1,
                action_type=_rec.action_type,
                action_args=dict(_rec.action_args),
                expected_pkg=_rec.post_action_pkg,
                action_description=_rec.action_description,
                pre_action_pkg=_rec.pre_action_pkg,
                post_action_ui_fp=_rec.post_action_ui_fp,
                target_element_signature=_rec.target_element_signature,
                target_element_crop_b64=getattr(_rec, 'crop_b64', ''),
                pre_action_screen_hash=getattr(_rec, 'screen_hash', ''),
                success_count=0,
                fail_count=0,
            ))

        if not _repaired:
            return None

        now = time.time()
        plan = TaskPlan(
            intent_key=intent_key,
            instruction_sample=instruction[:300],
            steps=_repaired,
            success_count=1,
            fail_count=0,
            last_verified=now,
            created_at=self._replay_plan.created_at,
            source_run_id=run_id,
            device_bucket=device_bucket,
        )

        self.store.upsert_by_intent(plan)
        self._replay_plan = None
        return plan

    def build_and_store_plan(
        self,
        intent_key: str,
        instruction: str,
        run_id: str,
        device_bucket: str = "default",
    ) -> TaskPlan | None:
        """Convert recorded steps into a TaskPlan and persist it.

        Only called after a successful run.  Returns the stored plan, or
        None if there are no recordable steps.
        """
        if not self._recorded_steps:
            return None

        plan_steps: list[PlanStep] = []
        for rec in self._recorded_steps:
            # Skip steps that produced no visible screen change — the action
            # had no effect (e.g. Home press on launcher, open when already
            # in the target app, click on a non-interactive element).
            # Type actions are excluded: the UI structure may not change
            # even though text was entered successfully.
            if (
                rec.action_type != "type"
                and rec.pre_action_ui_fp
                and rec.post_action_ui_fp
                and rec.pre_action_ui_fp == rec.post_action_ui_fp
            ):
                continue
            # Deduplicate consecutive identical steps — a VLM retry loop
            # (e.g. typing the same text 3 times in a row) should only
            # produce one plan step.  Overwrite the previous duplicate
            # rather than skipping, so the LAST attempt (the one that
            # actually worked) is the one that survives.
            if plan_steps and _step_is_duplicate(plan_steps[-1], rec):
                plan_steps[-1] = PlanStep(
                    step_index=rec.step_index,
                    action_type=rec.action_type,
                    action_args=rec.action_args,
                    expected_pkg=rec.post_action_pkg,
                    action_description=rec.action_description,
                    pre_action_pkg=rec.pre_action_pkg,
                    post_action_ui_fp=rec.post_action_ui_fp,
                    target_element_signature=rec.target_element_signature,
                    target_element_crop_b64=getattr(rec, 'crop_b64', ''),
                    pre_action_screen_hash=getattr(rec, 'screen_hash', ''),
                )
                continue
            plan_steps.append(PlanStep(
                step_index=rec.step_index,
                action_type=rec.action_type,
                action_args=rec.action_args,
                expected_pkg=rec.post_action_pkg,
                action_description=rec.action_description,
                pre_action_pkg=rec.pre_action_pkg,
                post_action_ui_fp=rec.post_action_ui_fp,
                target_element_signature=rec.target_element_signature,
                target_element_crop_b64=getattr(rec, 'crop_b64', ''),
                pre_action_screen_hash=getattr(rec, 'screen_hash', ''),
            ))

        now = time.time()
        plan = TaskPlan(
            intent_key=intent_key,
            instruction_sample=instruction[:300],
            steps=plan_steps,
            success_count=1,
            fail_count=0,
            last_verified=now,
            created_at=now,
            source_run_id=run_id,
            device_bucket=device_bucket,
        )

        # Upsert: replace older plans for the same intent so the store
        # always has the freshest recording.
        self.store.upsert_by_intent(plan)
        return plan

    def clear_recording(self) -> None:
        """Discard any recorded steps (e.g. after a failed run)."""
        self._recorded_steps.clear()

    def note_plan_success(self, intent_key: str) -> None:
        """Increment success counter for a plan that just replayed fully."""
        try:
            self.store.increment_success(intent_key)
        except Exception:
            pass

    def note_plan_failure(self, intent_key: str) -> None:
        """Increment fail counter for a plan that failed during replay."""
        try:
            self.store.increment_fail(intent_key)
        except Exception:
            pass

    def note_step_failure(self, intent_key: str) -> None:
        """Increment the fail_count of the step that failed during replay.

        Safe to call even when no step failed (no-op).  This is how the
        plan executor reports step-level failures back to the store so
        the offending step eventually becomes unhealthy.
        """
        for _idx in self._failed_step_indices:
            try:
                self.store.increment_step_fail(intent_key, _idx)
            except Exception:
                pass
