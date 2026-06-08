"""VLM calling with primary + fallback provider chain.

Extracted from the runner's ``main()`` so the provider-selection,
retry, cooldown, and context-management logic are independently
testable and don't bloat the step loop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from utils import build_messages, ERROR_CALLING_LLM, GUIOwlWrapper


# Cooldown applied when the primary provider returns persistent errors,
# so every step doesn't waste time retrying a broken endpoint.
PRIMARY_RECOVERY_COOLDOWN_SECONDS = 120  # 2 minutes


@dataclass
class ProviderConfig:
    api_key: str
    base_url: str
    model: str
    max_context_size: int | None = None


@dataclass
class VLMResult:
    output_text: str
    provider_label: str           # e.g. "primary:gui-owl @ http://..."
    primary_seconds: float = 0.0
    fallback_seconds: float = 0.0
    primary_in_cooldown: bool = False


class VLMProvider:
    """Calls the VLM, falling back to a secondary provider on failure.

    Manages a cooldown timer so a persistently-failing primary endpoint
    is skipped for ``PRIMARY_RECOVERY_COOLDOWN_SECONDS``, avoiding
    wasted per-step retries.
    """

    def __init__(
        self,
        primary: ProviderConfig,
        fallback: ProviderConfig | None = None,
        *,
        compact_mode: bool = False,
        install_checker: object = None,  # duck-typed: has .check(device) -> bool
    ):
        self._primary = primary
        self._fallback = fallback
        self._compact_mode = compact_mode
        self._install_checker = install_checker

        # Cooldown state
        self._cooldown_until: float = 0.0
        self._cooldown_reason: str = ""
        self._consecutive_fallback_steps: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def call(
        self,
        screenshot_path: str,
        instruction: str,
        history: list[dict],
        model_name: str,
        *,
        foreground_pkg: str = "",
        ui_summary: str = "",
        installed_apps_hint: str = "",
        target_app_hint: str = "",
    ) -> VLMResult:
        """Call the VLM (primary → fallback) and return the result."""

        messages = build_messages(
            screenshot_path, instruction, history, model_name,
            foreground_pkg=foreground_pkg,
            ui_summary=ui_summary,
            installed_apps_hint=installed_apps_hint,
            target_app_hint=target_app_hint,
            compact=self._compact_mode,
        )

        vllm = GUIOwlWrapper(
            self._primary.api_key,
            self._primary.base_url,
            self._primary.model,
            max_retry=1,
            max_context_size=self._primary.max_context_size,
        )

        output_text = ERROR_CALLING_LLM
        primary_seconds = 0.0
        fallback_seconds = 0.0
        provider_label = f"primary:{self._primary.model} @ {self._primary.base_url}"
        primary_in_cooldown = False
        primary_failed = False

        _t_primary = time.time()
        _now = time.time()

        if _now < self._cooldown_until:
            _remaining = int(self._cooldown_until - _now)
            # Force a primary retry after 3 consecutive fallback steps —
            # the primary error may have been transient and 10 min of
            # fallback decisions is worse than one failed primary attempt.
            if self._consecutive_fallback_steps >= 3:
                print(
                    f"[VLM] forcing primary retry after "
                    f"{self._consecutive_fallback_steps} fallback steps "
                    f"(cooldown had {_remaining}s left)"
                )
                self._cooldown_until = 0.0
            else:
                primary_in_cooldown = True
                print(
                    f"[VLM] primary provider in cooldown "
                    f"({_remaining}s left, reason={self._cooldown_reason}) "
                    f"— skipping primary"
                )
        else:
            for _p_try in range(1, 3):  # 2 attempts
                print(f"[VLM] primary attempt {_p_try}/2")
                output_text, _, _ = vllm.predict_mm(messages)
                if output_text != ERROR_CALLING_LLM:
                    break
                if _p_try < 2:
                    print("[VLM] primary attempt failed — retrying in 2s")
                    time.sleep(2)
            primary_failed = (output_text == ERROR_CALLING_LLM)

        primary_seconds = time.time() - _t_primary

        # Fallback provider
        if output_text == ERROR_CALLING_LLM and self._fallback is not None:
            if not primary_in_cooldown and primary_failed:
                self._cooldown_until = time.time() + PRIMARY_RECOVERY_COOLDOWN_SECONDS
                self._cooldown_reason = "primary_error"
                print(
                    f"[VLM] entering primary cooldown for "
                    f"{PRIMARY_RECOVERY_COOLDOWN_SECONDS}s"
                )

            print(f"[VLM] Primary provider failed — switching to fallback: {self._fallback.model}")

            fb_compact = bool(
                self._fallback.max_context_size
                and self._fallback.max_context_size <= 2048
            )
            if fb_compact and not self._compact_mode:
                fb_messages = build_messages(
                    screenshot_path, instruction, history, self._fallback.model,
                    foreground_pkg=foreground_pkg,
                    ui_summary="",  # omit — saves ~150 tokens
                    installed_apps_hint=installed_apps_hint,
                    target_app_hint=target_app_hint,
                    compact=True,
                    history_n=1,
                )
            else:
                fb_messages = messages

            fb_vllm = GUIOwlWrapper(
                self._fallback.api_key,
                self._fallback.base_url,
                self._fallback.model,
                max_retry=1,
                max_context_size=self._fallback.max_context_size,
            )

            _t_fallback = time.time()
            for _fb_try in range(1, 4):  # 3 attempts
                print(f"[VLM] fallback attempt {_fb_try}/3")
                output_text, _, _ = fb_vllm.predict_mm(fb_messages)
                if output_text != ERROR_CALLING_LLM:
                    break
                if _fb_try < 3:
                    print("[VLM] fallback attempt failed — retrying in 2s")
                    time.sleep(2)
            if output_text == ERROR_CALLING_LLM:
                print("[VLM] fallback exhausted all retries")
            fallback_seconds = time.time() - _t_fallback
            provider_label = f"fallback:{self._fallback.model} @ {self._fallback.base_url}"

        elif output_text == ERROR_CALLING_LLM:
            print("[VLM] primary failed and no fallback provider is configured")

        if output_text == ERROR_CALLING_LLM:
            print(f"[VLM] provider used: {provider_label} (ERROR_CALLING_LLM)")
        else:
            print(f"[VLM] provider used: {provider_label}")

        # Track consecutive fallback usage so we can force a primary retry
        # after N steps of inferior fallback decisions.
        if "fallback:" in provider_label:
            self._consecutive_fallback_steps += 1
        else:
            self._consecutive_fallback_steps = 0

        return VLMResult(
            output_text=output_text,
            provider_label=provider_label,
            primary_seconds=primary_seconds,
            fallback_seconds=fallback_seconds,
            primary_in_cooldown=primary_in_cooldown,
        )
