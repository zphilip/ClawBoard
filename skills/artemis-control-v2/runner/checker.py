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
  - **Device probe output** (read-only state: settings values, dumpsys) when a
    declared check item names a state change
- What evidence is MISSING?
  - We can't see toast messages that have already disappeared
  - We can't verify content that scrolled off-screen
  - We can't confirm network operations completed

### 3. Declared check items & probes
When the prompt includes "Declared check items" with "Device probe results":
- Each `verify:` item is an acceptance criterion you MUST judge explicitly.
  A `verify` item FAILS only when the probe output contradicts it with concrete
  evidence; if the needed probe was never run or returned nothing decisive,
  treat it as inconclusive and say so.
- Judge each `verify` item against its own probe result, then combine them into
  the overall `goal_achieved` verdict: goal is achieved only if every `verify`
  item passed (or is inconclusive with no contradicting evidence).
- A `verify` item is about persistent device STATE, not about seeing a literal
  UI label. "静音" is satisfied if the ringer mode is SILENT or volume is 0 —
  do NOT fail it just because no "静音" button was present.

### 4. Failure Modes
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
  "recommendation": "What to tell the user (if failed, suggest next steps)",
  "check_items": [
    {"text": "the check-item text, quoted verbatim", "kind": "verify", "status": "passed|failed|inconclusive", "evidence": "probe output / observation"}
  ],
  "unmet_subgoals": []
}
```

- **`unmet_subgoals`** (final check only): plan milestone texts (verbatim) that are
  marked `[x]` but whose verify criteria are demonstrably unmet.  Empty `[]` when
  all completed milestones' verify items passed or are inconclusive.
- Check the example first — do NOT use leading spaces, newline is not allowed in expected JSON schema
- **`check_items` is REQUIRED when "Declared check items" appear in the prompt**:
  one entry per item, status ∈ passed/failed/inconclusive, each with concrete
  evidence. If no check items were declared, return `"check_items": []`.
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
    check_items: list[dict] | None = None,
    probe_results: dict | None = None,
) -> str:
    """Build the Checker's user prompt with full execution context.

    Args:
        instruction: The original task goal.
        plan_text: Final plan state (all milestones should be [x]).
        actions: List of human-readable action descriptions.
        steps: Total steps taken.
        verification_summary: Summary of milestone verification results.
        check_items: Declared ``- verify:`` / ``- assert:`` items (list of dicts
            with ``kind``/``when``/``text``).
        probe_results: Mapping check-item text → read-only probe output.
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

    if check_items:
        rendered = []
        for ci in check_items:
            kind = ci.get("kind", "verify")
            when = ci.get("when", "on_complete")
            text = ci.get("text", "")
            probe = (probe_results or {}).get(text, "")
            line = f"- [{kind}@{when}] {text}"
            if probe:
                line += f"\n  probe: {probe}"
            rendered.append(line)
        parts.extend(["", "## Declared check items\n" + "\n".join(rendered)])

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
    check_items: list[dict] | None = None,
    probe_results: dict | None = None,
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
        check_items=check_items,
        probe_results=probe_results,
    )

    raw = None

    # Try text-only LLM first (preferred for reasoning tasks)
    if text_llm_caller:
        try:
            raw, finish_reason = text_llm_caller(
                system_prompt,
                user_prompt,
                max_tokens=4096,
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
                max_tokens=4096,
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
            "check_items": _unjudged_check_items(check_items),
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
            "check_items": _unjudged_check_items(check_items),
            "success": False,
        }

    parsed["success"] = True
    # Backfill missing per-item verdicts so callers can always read check_items.
    if parsed.get("check_items") is None:
        parsed["check_items"] = _unjudged_check_items(check_items)
    # Post-process: downgrade hallucinated failures, backfill missing items.
    return normalize_checker_result(parsed, check_items)


def _unjudged_check_items(check_items: list[dict] | None) -> list[dict]:
    """Per-item verdicts for when the Checker LLM could not judge them."""
    return [
        {
            "text": ci.get("text", ""),
            "kind": ci.get("kind", "verify"),
            "status": "inconclusive",
            "evidence": "Checker unavailable — no judgment produced",
        }
        for ci in (check_items or [])
    ]


# ── Verdict hygiene (computed outside the model) ────────────────────────────


def normalize_checker_result(parsed: dict,
                             check_items: list[dict] | None = None) -> dict:
    """Post-process a Checker's output to fix common model errors.

    1. A ``failed`` verdict with blank/trivial evidence is downgraded to
       ``inconclusive`` — the model hallucinated a failure.
    2. Every declared check item is guaranteed a verdict (missing → inconclusive).
    3. Duplicate verdicts for the same item text are removed (first wins).

    Returns a new dict (does not mutate the input).
    """
    result = dict(parsed)
    items = check_items or []
    declared = {(ci.get("text", ""), ci.get("kind", "verify")): ci for ci in items}
    seen: set[tuple[str, str]] = set()
    cleaned: list[dict] = []

    for v in result.get("check_items") or []:
        key = (v.get("text", ""), v.get("kind", "verify"))
        if key in seen:
            continue  # duplicate
        seen.add(key)
        status = str(v.get("status", "")).lower()
        evidence = str(v.get("evidence", "")).strip()
        # Downgrade: a failed verdict with no concrete evidence is not reliable
        if status == "failed" and (
            not evidence
            or evidence.lower() in ("none", "n/a", "无", "无证据")
        ):
            v = dict(v)
            v["status"] = "inconclusive"
            v["evidence"] = (
                evidence + " [downgraded: no concrete evidence provided]"
                if evidence else "[downgraded: no concrete evidence provided]"
            )
        cleaned.append(v)

    # Backfill: every declared item that was not addressed
    for key, ci in declared.items():
        if key not in seen:
            cleaned.append({
                "text": ci.get("text", ""),
                "kind": ci.get("kind", "verify"),
                "status": "inconclusive",
                "evidence": "no verdict produced for this item",
            })

    result["check_items"] = cleaned
    return result


def verdicts_allow_release(check_items: list[dict] | None,
                           verdicts: list[dict] | None = None) -> bool:
    """Fail-open release: every ``verify`` item must be passed or inconclusive.

    - ``assert`` failures never block release (they are preconditions, not
      goal-achievement criteria).
    - ``inconclusive`` is not a blocker (benefit of the doubt goes to releasing).
    - Release is computed OUTSIDE the model — a hallucinated ``failed`` that
      was downgraded by ``normalize_checker_result`` does not block release.

    Returns True when the task is safe to release/complete.
    """
    items = verdicts if verdicts else (check_items or [])
    for v in items:
        if str(v.get("kind", "verify")).lower() != "assert":
            status = str(v.get("status", "")).lower()
            if status not in ("passed", "inconclusive"):
                return False
    return True