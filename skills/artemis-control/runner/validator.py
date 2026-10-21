"""Validator node: independent VLM call that judges the Operator's proposed action.

Part of the Pro multi-agent graph:
  Planner → Operator → Validator → (execute) → Checker

The Validator is a separate VLM call with its own system prompt that judges:
  1. Safety: Is the proposed action safe to execute?
  2. Progress: Does this action advance the current milestone toward the goal?
  3. Correctness: Is this the right element/target given the current screen?

Returns structured approval/rejection with reasoning that feeds back into the
Operator's next prompt for self-correction.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

from runner.planner import PlanSnapshot, parse_plan

# ---------------------------------------------------------------------------
# Validator prompt (separate VLM system prompt — NOT mixed with Operator)
# ---------------------------------------------------------------------------

_VALIDATOR_SYSTEM_PROMPT = """## Role
You are an Action Validator — a fast, independent second-opinion judge. Your job is
to review a proposed mobile-device action and decide whether it is safe, correct,
and advancing the task. You never execute or modify actions; you only evaluate them.

## Evaluation Criteria

### 1. Safety (FAIL if violated)
- Would this action cause data loss, send unintended messages, make purchases, or
  change irreversible system settings?
- Would it crash or destabilize the app?
- Is it tapping on a destructive UI element (delete, uninstall, logout, factory reset)?

### 2. Progress Toward Goal (WARN if not advancing)
- Does this action move the task closer to completing the current active milestone?
- Is it repeating an action that already failed? (check the Previous Incidents)
- Is it a no-op — tapping an already-selected tab, typing empty text, swiping on a
  non-scrollable area?

### 3. Element Correctness (FAIL if wrong target)
- Does the tapped element match the action's intent?
  - Tapping the settings gear when the goal says "open WeChat" → FAIL
  - Tapping a search bar when the milestone says "find a contact" → APPROVE
- Is the coordinate landing on the correct element, or is it off-target?

## Response Format
You MUST respond with a single JSON object. No other text.

```json
{
  "verdict": "approve" | "reject" | "warn",
  "reason": "One sentence explaining your judgment",
  "concern": "If warn or reject: what specifically is wrong",
  "suggestion": "If reject: what the Operator should do instead"
}
```

- **approve**: Action is safe, correct, and advances the task. Execute it.
- **warn**: Action is safe but may not be optimal. Execute it but note the concern.
- **reject**: Action is unsafe/wrong/irrelevant. DO NOT execute. Provide a concrete
  alternative.

## Judgment Guidelines
- Be strict about safety: reject anything destructive.
- Be lenient about strategy: if the Operator chose a different but valid approach,
  approve it. Don't second-guess reasonable choices.
- Flag repeated failures: if the action is identical to a previously failed one,
  reject with a concrete alternative.
- For "finish" actions: approve only if the plan shows all milestones complete;
  otherwise reject with the remaining milestones listed."""


def build_validator_system_prompt() -> str:
    """Return the Validator's system prompt."""
    return _VALIDATOR_SYSTEM_PROMPT


def build_validator_user_prompt(
    instruction: str,
    plan_text: str,
    proposed_action: str,
    action_type: str,
    action_args: dict | None = None,
    previous_incidents: str = "",
) -> str:
    """Build the Validator's user prompt with full context.

    Args:
        instruction: The original task goal.
        plan_text: Current plan state (markdown).
        proposed_action: Human-readable description of the Operator's proposed action.
        action_type: The action type (tap, swipe, type, open_app, etc.).
        action_args: The action arguments (coordinates, text, etc.).
        previous_incidents: Rendered incident history, if any.
    """
    args_str = json.dumps(action_args or {}, ensure_ascii=False)

    snapshot = parse_plan(plan_text) if plan_text else None
    current_milestone = ""
    if snapshot:
        active = snapshot.active_milestone()
        if active:
            current_milestone = active.text
        elif snapshot.top_level:
            # Show first pending as the implied current target
            for item in snapshot.top_level:
                if item.is_pending:
                    current_milestone = item.text
                    break

    plan_summary = snapshot.render_compact() if snapshot else "(no plan)"

    parts = [
        f"## Task Goal\n{instruction}",
        "",
        f"## Current Plan State\n{plan_summary}",
    ]

    if current_milestone:
        parts.append(f"\n**Current milestone**: {current_milestone}")

    parts.extend([
        "",
        f"## Proposed Action\nType: `{action_type}`",
        f"Arguments: {args_str}",
        f"Description: {proposed_action}",
    ])

    if previous_incidents:
        parts.extend(["", "## Previous Incidents", previous_incidents])

    parts.extend([
        "",
        "Evaluate this action against the three criteria (Safety, Progress, Correctness).",
        "Respond with a single JSON object:",
    ])

    return "\n".join(parts)


def call_validator(
    instruction: str,
    plan_text: str,
    proposed_action: str,
    action_type: str,
    action_args: dict | None,
    screenshot_path: str,
    vlm_caller: Callable[
        [str, str, str, int], tuple[Optional[str], str]
    ],
    previous_incidents: str = "",
) -> dict:
    """Call the Validator VLM to judge a proposed action.

    Returns:
        dict with keys:
          - verdict: "approve" | "reject" | "warn"
          - reason: explanation string
          - concern: specific issue (if warn/reject)
          - suggestion: what to do instead (if reject)
          - raw_response: the VLM's raw output
          - success: whether the VLM call succeeded
    """
    system_prompt = build_validator_system_prompt()
    user_prompt = build_validator_user_prompt(
        instruction=instruction,
        plan_text=plan_text,
        proposed_action=proposed_action,
        action_type=action_type,
        action_args=action_args,
        previous_incidents=previous_incidents,
    )

    raw, finish_reason = vlm_caller(
        system_prompt,
        user_prompt,
        screenshot_path,
        max_tokens=1024,
    )

    if not raw:
        # On VLM failure, default to approve (don't block progress)
        return {
            "verdict": "approve",
            "reason": "Validator VLM unavailable — defaulting to approve",
            "concern": "",
            "suggestion": "",
            "raw_response": "",
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
            "verdict": "approve",
            "reason": f"Validator JSON parse failed — defaulting to approve",
            "concern": raw[:200],
            "suggestion": "",
            "raw_response": raw,
            "success": False,
        }

    return {
        "verdict": parsed.get("verdict", "approve"),
        "reason": parsed.get("reason", ""),
        "concern": parsed.get("concern", ""),
        "suggestion": parsed.get("suggestion", ""),
        "raw_response": raw,
        "success": True,
    }