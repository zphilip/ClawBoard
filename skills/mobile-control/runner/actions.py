"""Lightweight action utilities.

Full action execution dispatch stays in ``main()`` because every
handler reads / writes closure state (``any_real_action``,
``consecutive_waits``, ``termination_reason``, ``history``, etc.).
This module provides pure helpers that don't mutate runner state.
"""

from __future__ import annotations


def action_display_name(action_type: str, action_args: dict) -> str:
    """Human-readable one-line summary of an action for logging."""
    if action_type == "click":
        coord = action_args.get("coordinate", [])
        if len(coord) >= 2:
            return f"click {coord}"
        return "click"
    if action_type == "type":
        text = action_args.get("text", "")
        return f'type "{text}"'
    if action_type == "system_button":
        btn = action_args.get("button", "")
        return f"system_button {btn}"
    if action_type in ("scroll", "swipe"):
        return "swipe/scroll"
    if action_type == "long_press":
        return "long_press"
    if action_type == "open":
        app = action_args.get("text", "")
        return f"open {app}"
    return action_type
