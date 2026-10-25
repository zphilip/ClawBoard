"""Lazy Diagnoser for artemis-control (Phase 2).

Runs only when the agent has stalled: the ``IncidentTracker``'s
``consecutive_failures`` crosses a threshold (default 2).  It fires at most
once per failure burst and hands recovery hints back to the next Operator
prompt (via ``IncidentTracker.add_recovery_hint``) plus optional notes to
persist.

Text-LLM based when a text caller is supplied; otherwise falls back to a
deterministic diagnosis built from the incident reasons (no network needed).

No LangChain — pure functions + a single JSON round-trip.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_DIAGNOSER_SYSTEM_PROMPT = """## Role
You are a mobile-automation Failure Diagnoser. The agent is STUCK: several of
its recent actions were rejected or blocked in a row. Diagnose the root cause
and propose concrete recovery.

## Inputs
- The original task goal
- The current plan (checklist)
- The actions taken so far
- The recent incidents (what was blocked / rejected, and why)

## Rules
- Read the incidents carefully — the reason field is the strongest signal.
- A root cause is specific ("the login button moved off-screen after the ad
  banner loaded"), not generic ("it failed").
- Recovery hints must be ACTIONABLE by the operator on its next turn.
- If a useful persistent fact was learned (e.g. "this app requires SMS login,
  there is no password tab"), record it under ``notes`` so future runs remember.

## Response Format
You MUST respond with a single JSON object. No other text.

```json
{
  "root_cause": "one line",
  "recovery_hints": ["hint 1", "hint 2"],
  "notes": {"key": "content"}
}
```

- ``recovery_hints``: 1-3 concrete next actions for the operator.
- ``notes``: optional persistent notes (empty object if none).
"""


def build_diagnoser_system_prompt() -> str:
    return _DIAGNOSER_SYSTEM_PROMPT


def build_diagnoser_user_prompt(
    instruction: str,
    plan_text: str,
    actions: list[str],
    incident_text: str,
    notes_text: str = "",
) -> str:
    action_summary = "\n".join(f"  {i+1}. {a}" for i, a in enumerate(actions[-20:]))
    if len(actions) > 20:
        action_summary = f"  ... ({len(actions) - 20} earlier steps)\n" + action_summary

    parts = [
        f"## Original Goal\n{instruction}",
        "",
        f"## Current Plan\n```\n{plan_text or '(no plan)'}\n```",
        "",
        f"## Actions Taken\n{action_summary or '(none)'}",
        "",
        f"## Recent Incidents\n{incident_text or '(none)'}",
    ]
    if notes_text:
        parts.extend(["", f"## Persistent Notes\n{notes_text}"])

    parts.extend([
        "",
        "Diagnose why the agent is stuck and give actionable recovery hints.",
        "Respond with a single JSON object.",
    ])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Deterministic fallback (no LLM)
# ---------------------------------------------------------------------------

def deterministic_diagnosis(incidents: list) -> dict:
    """Build a hint from incident reasons without calling an LLM."""
    reasons = []
    categories: list[str] = []
    for inc in incidents[-5:]:
        reason = (getattr(inc, "reason", "") or "").strip()
        category = (getattr(inc, "category", "") or "").strip()
        if reason:
            reasons.append(reason)
        if category:
            categories.append(category)

    root_cause = reasons[-1] if reasons else "unknown"
    unique_reasons = list(dict.fromkeys(reasons))

    # Deterministic heuristic: anti-loop + de-duplication + backoff.
    hints: list[str] = []
    if unique_reasons:
        hints.append(
            "The last blocked action was: " + unique_reasons[-1] +
            ". Do NOT retry it — pick a different target or action type."
        )
    if len(set(categories)) == 1 and categories:
        hints.append(
            f"All recent failures share category '{categories[0]}'. "
            "Re-evaluate whether the current screen actually matches the plan milestone."
        )
    hints.append(
        "If the screen has not changed for several steps, press BACK once, "
        "then re-screenshot and reassess before continuing."
    )

    return {
        "success": True,
        "root_cause": root_cause,
        "recovery_hints": hints,
        "notes": {},
        "deterministic": True,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _render_incidents(incidents: list) -> str:
    lines = []
    for inc in incidents[-6:]:
        lines.append(
            f"  - [{getattr(inc, 'category', '')}] {getattr(inc, 'reason', '')}"
            f" (consecutive_failures={getattr(inc, 'consecutive_failures', 1)})"
        )
    return "\n".join(lines)


def call_diagnoser(
    instruction: str,
    plan_text: str,
    actions: list[str],
    incidents: list,
    notes_text: str = "",
    text_llm_caller: Callable[[str, str, int], tuple[Optional[str], str]] | None = None,
) -> dict:
    """Run one diagnosis round.

    Returns a normalized dict::

        {"success": bool, "root_cause": str, "recovery_hints": [str], "notes": {str: str}}

    ``success`` is False only when the LLM call + parse both failed (callers
    should then just proceed without hints).  ``notes`` is always a dict.
    """
    incident_text = _render_incidents(incidents)

    raw = None
    if text_llm_caller:
        try:
            raw, _finish = text_llm_caller(
                build_diagnoser_system_prompt(),
                build_diagnoser_user_prompt(
                    instruction, plan_text, actions, incident_text, notes_text,
                ),
                1024,
            )
        except Exception:
            raw = None

    if raw:
        json_str = re.sub(r"<[^>]+/?>", "", raw)
        json_str = re.sub(r"```\w*\n?", "", json_str)
        brace_start = json_str.find("{")
        brace_end = json_str.rfind("}")
        if brace_start >= 0 and brace_end > brace_start:
            json_str = json_str[brace_start : brace_end + 1]
        try:
            parsed = json.loads(json_str)
            hints = parsed.get("recovery_hints") or []
            if isinstance(hints, str):
                hints = [hints]
            notes = parsed.get("notes") or {}
            if not isinstance(notes, dict):
                notes = {}
            return {
                "success": True,
                "root_cause": parsed.get("root_cause", ""),
                "recovery_hints": [str(h) for h in hints if str(h).strip()],
                "notes": {str(k): str(v) for k, v in notes.items() if v},
            }
        except json.JSONDecodeError:
            pass

    # Fall back to deterministic hints (never blocks the run).
    return deterministic_diagnosis(incidents)