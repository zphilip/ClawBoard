"""Checker node: final audit of task completion against the original goal.

Part of the Pro multi-agent graph:
  Planner → Operator → Validator → (execute) → ... → Checker (final)

The Checker is the last node to run — a separate VLM/LLM call that reviews the
completed task against the original goal. It receives the final screenshot,
execution history, and plan state, and decides whether the goal was truly achieved.

Uses the ``supervisor_provider`` from config.json when configured (text-only LLM
preferred for this task); otherwise reuses the primary VLM provider.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# Checker prompt (separate LLM call — prefers text-only model from config)
# ---------------------------------------------------------------------------

_CHECKER_SYSTEM_PROMPT = """## Role
You are a Task Completion Auditor. Your job is to review a completed Android
automation task and determine whether the original goal was achieved. You are
the final quality gate — your verdict determines whether the task is reported
as success or failure to the user.

## Evaluation Criteria

### 1. Goal Achievement
- Read the original goal carefully. What was the user actually asking for?
- Compare the final state against each component of the goal.
- Were ALL parts of the goal completed? Partial success is still failure.

### 2. Evidence Quality
- What concrete evidence supports completion?
  - Screenshot showing the expected final state
  - Action history showing the right sequence
  - Plan milestones all marked complete
- What evidence is MISSING?
  - We can't see toast messages that have already disappeared
  - We can't verify content that scrolled off-screen
  - We can't confirm network operations completed

### 3. Failure Modes
- Was the wrong app opened? Wrong account? Wrong setting changed?
- Did the agent get stuck on a permission dialog or ad?
- Did the agent navigate to the right screen but fail to perform the final action?

## Response Format
You MUST respond with a single JSON object. No other text.

```json
{
  "goal_achieved": true,
  "confidence": 0.0,
  "summary": "One sentence summarizing what happened",
  "evidence": ["bullet point", "bullet point"],
  "concerns": ["missing evidence", "potential issue"],
  "recommendation": "What to tell the user (if failed, suggest next steps)"
}
```

- **goal_achieved**: true if you are confident the goal was met
- **confidence**: 0.0-1.0 — how certain you are (0.5 = unsure)
- **summary**: One line describing the outcome
- **evidence**: Concrete observations supporting your verdict
- **concerns**: Anything that might indicate the task wasn't fully done
- **recommendation**: For the user — "Task completed successfully" or "Task may have failed because..."

## Judgment Guidelines
- **Benefit of the doubt goes to the agent.** If the evidence suggests success but
  you can't be 100% certain (e.g., a toast might have appeared and disappeared),
  still report success with lowered confidence.
- **Be specific about failure.** Don't say "task failed" — say "the app was opened
  but the search was never performed" so the user knows exactly what went wrong.
- **Confidence reflects evidence quality.** Screenshot clearly showing the expected
  state → high confidence. No screenshot, only action history → low confidence."""


def build_checker_system_prompt() -> str:
    """Return the Checker's system prompt."""
    return _CHECKER_SYSTEM_PROMPT


def build_checker_user_prompt(
    instruction: str,
    plan_text: str,
    actions: list[str],
    steps: int,
    verification_summary: str = "",
) -> str:
    """Build the Checker's user prompt with full execution context.

    Args:
        instruction: The original task goal.
        plan_text: Final plan state (all milestones should be [x]).
        actions: List of human-readable action descriptions.
        steps: Total steps taken.
        verification_summary: Summary of milestone verification results.
    """
    action_summary = "\n".join(f"  {i+1}. {a}" for i, a in enumerate(actions[-20:]))
    if len(actions) > 20:
        action_summary = f"  ... ({len(actions) - 20} earlier steps)\n" + action_summary

    parts = [
        f"## Original Goal\n{instruction}",
        "",
        f"## Final Plan State\n```\n{plan_text}\n```",
        "",
        f"## Execution Summary\nTotal steps: {steps}\nActions taken:\n{action_summary}",
    ]

    if verification_summary:
        parts.extend(["", f"## Milestone Verification\n{verification_summary}"])

    parts.extend([
        "",
        "Evaluate whether the original goal was achieved based on the evidence above.",
        "Respond with a single JSON object.",
    ])

    return "\n".join(parts)


def call_checker(
    instruction: str,
    plan_text: str,
    actions: list[str],
    steps: int,
    screenshot_path: str,
    vlm_caller: Callable[
        [str, str, str, int], tuple[Optional[str], str]
    ],
    text_llm_caller: Callable[
        [str, str, int], tuple[Optional[str], str]
    ] | None = None,
    verification_summary: str = "",
) -> dict:
    """Call the Checker to audit task completion.

    Uses the text LLM caller (supervisor_provider from config) when available,
    since the Checker task benefits from a strong reasoning model and doesn't
    necessarily need vision. Falls back to the VLM caller with screenshot.

    Args:
        instruction: The original task goal.
        plan_text: Final plan state.
        actions: List of action descriptions.
        steps: Total steps taken.
        screenshot_path: Path to the final screenshot (for VLM fallback).
        vlm_caller: Vision-capable LLM caller (system_prompt, user_prompt,
            image_path, max_tokens) → (response, finish_reason).
        text_llm_caller: Text-only LLM caller (system_prompt, user_prompt,
            max_tokens) → (response, finish_reason). Preferred when available.
        verification_summary: Milestone verification summary string.

    Returns:
        dict with keys from the checker output schema plus ``success`` (bool)
        indicating whether the checker call itself succeeded.
    """
    system_prompt = build_checker_system_prompt()
    user_prompt = build_checker_user_prompt(
        instruction=instruction,
        plan_text=plan_text,
        actions=actions,
        steps=steps,
        verification_summary=verification_summary,
    )

    raw = None

    # Try text-only LLM first (preferred for reasoning tasks)
    if text_llm_caller:
        try:
            raw, finish_reason = text_llm_caller(
                system_prompt,
                user_prompt,
                max_tokens=1024,
            )
        except Exception:
            raw = None

    # Fall back to VLM if text LLM failed or not available
    if not raw:
        try:
            raw, finish_reason = vlm_caller(
                system_prompt,
                user_prompt,
                screenshot_path,
                max_tokens=2048,
            )
        except Exception:
            raw = None

    if not raw:
        return {
            "goal_achieved": True,
            "confidence": 0.3,
            "summary": "Checker unavailable — reporting task as complete with low confidence.",
            "evidence": [],
            "concerns": ["Checker LLM call failed — no audit performed"],
            "recommendation": "Task appears complete but could not be verified.",
            "success": False,
        }

    # Robust JSON extraction
    json_str = raw
    json_str = re.sub(r"<[^>]+/?>", "", json_str)
    json_str = re.sub(r"```\w*\n?", "", json_str)

    brace_start = json_str.find("{")
    brace_end = json_str.rfind("}")
    if brace_start >= 0 and brace_end > brace_start:
        json_str = json_str[brace_start : brace_end + 1]

    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError:
        return {
            "goal_achieved": True,
            "confidence": 0.3,
            "summary": "Checker JSON parse failed.",
            "evidence": [],
            "concerns": [f"Raw response: {raw[:300]}"],
            "recommendation": "Task appears complete but verification failed.",
            "success": False,
        }

    parsed["success"] = True
    return parsed