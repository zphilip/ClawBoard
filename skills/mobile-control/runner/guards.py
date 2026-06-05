"""Rule-based guards — pure decision functions that catch VLM mistakes.

These functions are deliberately free of side effects (no ADB calls,
no history mutation) so they can be reasoned about and tested in
isolation.  The caller in ``main()`` applies the returned override.
"""

from __future__ import annotations

from typing import Optional

# Home-related keywords across Chinese / English — when the VLM says
# "Home" in its reasoning text but the tool_call is a click, the guard
# overrides to a proper system_button=Home.
_HOME_KEYWORDS = ("home", "主页", "主屏幕",
                  "返回桌面", "按home", "按主页")

# Package names that identify launcher / home-screen apps.
# When the foreground app is one of these and the task has a clear
# target, we force an ``open`` action instead of an ambiguous icon tap.
_LAUNCHER_PKGS: frozenset[str] = frozenset({
    "net.oneplus.launcher",
    "com.android.launcher",
    "com.android.launcher3",
    "com.miui.home",
    "com.huawei.android.launcher",
    "com.oppo.launcher",
    "com.vivo.launcher",
    "com.samsung.android.launcher",
})


def launcher_pkgs() -> frozenset[str]:
    """Return the set of known launcher package names."""
    return _LAUNCHER_PKGS


def decide_rule_override(
    proposed_action: str,
    action_text: str,
    *,
    any_real_action: bool,
    loop_recovery_relaunch: bool,
    target_pkg_hint: str = "",
    target_app_hint: str = "",
    fg_pkg: str = "",
) -> dict | None:
    """Return an override action dict, or ``None`` to proceed normally.

    Checks, in priority order:

    (a) Intent/action mismatch — VLM says "Home" but proposes a click.
    (b) Premature answer — model gives ``answer`` before any real action.
    (c) Loop recovery — same scene+action repeated; force Home.
    (d) App disambiguation — on launcher / wrong app with a known target;
        replace ambiguous click with exact ``open``.
    """
    # (a) Intent/action mismatch
    if proposed_action == "click" and any(
        kw in action_text.lower() for kw in _HOME_KEYWORDS
    ):
        return {"action": "system_button", "button": "Home"}

    # (b) Premature answer
    if proposed_action == "answer" and not any_real_action:
        return {"action": "system_button", "button": "Home"}

    # (c) Loop recovery (skip terminal actions)
    if (
        loop_recovery_relaunch
        and proposed_action not in {"answer", "terminate", "interact"}
    ):
        return {"action": "system_button", "button": "Home"}

    # (d) App disambiguation / wrong-app recovery
    if target_pkg_hint and target_app_hint:
        fg_clean = (fg_pkg or "").strip()
        on_launcher = fg_clean in _LAUNCHER_PKGS
        in_wrong_app = bool(
            fg_clean and not on_launcher and fg_clean != target_pkg_hint
        )

        if on_launcher and proposed_action == "click":
            return {"action": "open", "text": target_app_hint}

        if in_wrong_app and proposed_action in (
            "wait", "click", "type", "scroll", "swipe", "long_press",
        ):
            return {"action": "open", "text": target_app_hint}

    return None
