"""Task Planner: milestone-based plan generation and plan-guided execution.

Ported from Artemis core:
  - artemis/agents/planner/planner.py       — PlannerNode, validate_plan_format
  - artemis/agents/planner/planner.json     — prompt template blocks
  - artemis/utils/plan_grammar.py           — parse_plan, PlanItem, PlanSnapshot

Provides:
  1. Plan grammar: parse_plan(), PlanItem, PlanSnapshot
  2. Plan generation: build_planner_system_prompt(), generate_plan()
  3. Plan-guided execution: build_operator_plan_block(), apply_plan_updates()

The grammar is a simplified port — no CheckItem, no finding lines, no async
validator. The VLM generates a markdown plan with checkbox milestones, and the
plan state is injected into subsequent Operator prompts for progress tracking.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Plan grammar constants (from artemis/utils/plan_grammar.py)
# ---------------------------------------------------------------------------

STATUS_PENDING = " "
STATUS_ACTIVE = "/"
STATUS_DONE = "x"
STATUS_BLOCKED = "!"
STATUS_CHARS = STATUS_PENDING + STATUS_ACTIVE + STATUS_DONE + STATUS_BLOCKED

LOOP_TAG = "[Loop]"
CONTINUOUS_LOOP_TAG = "[Loop:continuous]"

CHECKBOX_LINE_RE = re.compile(
    r"^(?P<indent>\s*)-\s*\[(?P<status>["
    + re.escape(STATUS_CHARS)
    + r"])\]\s*(?P<text>.*)$"
)
_LOOP_TAG_RE = re.compile(r"\[Loop(?::continuous)?\]")
_CONTINUOUS_TAG_RE = re.compile(re.escape(CONTINUOUS_LOOP_TAG))

# Maximum plan generation retries on parse failure
MAX_PLAN_RETRIES = 3


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlanItem:
    """One checkbox line of the plan's machine channel."""

    indent: int
    status: str
    text: str
    line_no: int

    @property
    def is_top_level(self) -> bool:
        return self.indent == 0

    @property
    def is_pending(self) -> bool:
        return self.status == STATUS_PENDING

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE

    @property
    def is_done(self) -> bool:
        return self.status == STATUS_DONE

    @property
    def is_blocked(self) -> bool:
        return self.status == STATUS_BLOCKED

    @property
    def is_loop(self) -> bool:
        return bool(_LOOP_TAG_RE.search(self.text))

    @property
    def is_continuous(self) -> bool:
        return bool(_CONTINUOUS_TAG_RE.search(self.text))


@dataclass(frozen=True)
class PlanSnapshot:
    """Read-only derived view of a plan. Never persisted, never written back."""

    items: tuple[PlanItem, ...] = field(default_factory=tuple)

    @property
    def top_level(self) -> tuple[PlanItem, ...]:
        return tuple(i for i in self.items if i.is_top_level)

    @property
    def has_top_level(self) -> bool:
        return bool(self.top_level)

    @property
    def all_top_level_done(self) -> bool:
        top = self.top_level
        return bool(top) and all(i.is_done for i in top)

    @property
    def pending_count(self) -> int:
        return sum(1 for i in self.top_level if i.is_pending)

    @property
    def done_count(self) -> int:
        return sum(1 for i in self.top_level if i.is_done)

    @property
    def total_count(self) -> int:
        return len(self.top_level)

    def active_milestone(self) -> PlanItem | None:
        """The milestone currently being executed.

        An explicitly active top-level item wins; otherwise the first pending
        milestone. Returns None when everything is done or nothing exists.
        """
        for item in self.top_level:
            if item.is_active:
                return item
        for item in self.top_level:
            if item.is_pending:
                return item
        return None

    def children_of(self, item: PlanItem) -> tuple[PlanItem, ...]:
        """Nested items under a top-level item (up to the next top-level line)."""
        if not item.is_top_level:
            return ()
        children: list[PlanItem] = []
        for candidate in self.items:
            if candidate.line_no <= item.line_no:
                continue
            if candidate.is_top_level:
                break
            children.append(candidate)
        return tuple(children)

    def milestone_texts(self) -> tuple[str, ...]:
        return tuple(i.text.strip() for i in self.top_level)

    def render_for_prompt(self) -> str:
        """Render the plan as a prompt block showing status of all milestones."""
        if not self.items:
            return "(no plan)"

        lines = ["--- Current Task Plan ---"]
        for item in self.items:
            status_display = {
                STATUS_PENDING: "⬜",
                STATUS_ACTIVE: "🔄",
                STATUS_DONE: "✅",
                STATUS_BLOCKED: "❌",
            }.get(item.status, f"[{item.status}]")

            indent_str = "  " * (item.indent // 2) if item.indent else ""
            lines.append(f"{indent_str}{status_display} [{item.status}] {item.text}")

        # Progress summary
        done = self.done_count
        total = self.total_count
        if total > 0:
            lines.append(f"\nProgress: {done}/{total} milestones complete")
            if self.all_top_level_done:
                lines.append("ALL MILESTONES COMPLETE — task finished.")

        return "\n".join(lines)

    def render_compact(self) -> str:
        """Compact one-line-per-milestone summary (for operator context)."""
        if not self.items:
            return "(no plan)"

        lines = []
        for item in self.items:
            if item.is_top_level:
                status_display = {
                    STATUS_PENDING: "⬜",
                    STATUS_ACTIVE: "🔄",
                    STATUS_DONE: "✅",
                    STATUS_BLOCKED: "❌",
                }.get(item.status, f"[{item.status}]")
                lines.append(f"{status_display} {item.text}")
            elif item.indent <= 2:
                lines.append(f"    └─ [{item.status}] {item.text}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Plan parsing
# ---------------------------------------------------------------------------

def parse_plan(content: str | None) -> PlanSnapshot:
    """Parse plan markdown into its machine channel.

    Non-checkbox lines are ignored. Only ``- [<status>] <text>`` lines
    contribute to the snapshot.
    """
    items: list[PlanItem] = []
    if content:
        for line_no, line in enumerate(content.splitlines()):
            match = CHECKBOX_LINE_RE.match(line)
            if match:
                items.append(
                    PlanItem(
                        indent=len(match.group("indent")),
                        status=match.group("status"),
                        text=match.group("text").strip(),
                        line_no=line_no,
                    )
                )
    return PlanSnapshot(items=tuple(items))


def validate_plan_format(content: str) -> tuple[bool, str]:
    """Validate a freshly generated plan against the machine-channel grammar.

    Returns (is_valid, error_message).
    """
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("- [") and not CHECKBOX_LINE_RE.match(stripped):
            allowed = ", ".join(f"'[{c}]'" for c in STATUS_CHARS)
            return (
                False,
                f"Invalid task status in line: {line}. Must be one of {allowed}.",
            )

    snapshot = parse_plan(content)
    if not snapshot.items:
        return (
            False,
            "Plan must contain at least one subgoal starting with '- [ ]'.",
        )

    return True, ""


# ---------------------------------------------------------------------------
# Plan grammar spec for prompt injection
# ---------------------------------------------------------------------------

_PLAN_GRAMMAR_BASE = f"""### Task Plan Grammar (machine-enforced contract)
Every checklist line MUST match `<indent>- [<status>] <text>`:
- Status characters: `[{STATUS_PENDING}]` pending, `[{STATUS_ACTIVE}]` in progress, `[{STATUS_DONE}]` completed, `[{STATUS_BLOCKED}]` blocked.
- Top-level lines (no indentation) are strategic milestones. Nested lines are their sub-goals: 2 spaces for a sub-goal, 4 spaces for a sub-sub-goal beneath it (deeper nesting is allowed). Only zero-indent lines count as milestones for termination and validation; nested lines are the executor's live ledger. All other text in the note is free-form context.
- `{LOOP_TAG}` tags a BOUNDED iterative milestone: declare its exit boundary (e.g., `(Exit: <condition>; Interval: <cadence>)`) and mark it `[{STATUS_DONE}]` only once that exit condition is verifiably met.
- `{CONTINUOUS_LOOP_TAG}` tags an UNBOUNDED continuous-monitoring milestone: it must stay `[{STATUS_ACTIVE}]` and can never be marked `[{STATUS_DONE}]`, deleted, or untagged by you — the system mechanically rejects such edits. Only an explicit external stop signal injected by the user unlocks its completion; that signal is an external interruption delivered by the system, never something inferred from the screen or the plan.
- The task terminates only when every top-level milestone is `[{STATUS_DONE}]`."""


def render_plan_grammar_spec() -> str:
    """Render the grammar spec taught to agents."""
    return _PLAN_GRAMMAR_BASE


# ---------------------------------------------------------------------------
# Planner system prompt blocks (adapted from artemis/agents/planner/planner.json)
# ---------------------------------------------------------------------------

_PLANNER_ROLE = """## Role
You are a Senior Test Engineer leading a mobile execution agent. Your objective is to deeply analyze user goals, anticipate execution pitfalls, and formulate robust, observable, and feasibility-bounded milestone blueprints for Android device automation."""

_PLANNER_THINKING_PROCESS = """## Systematic Thinking Process & Operating Principles
Before outputting your plan, you MUST systematically analyze the task following these 5 sequential thinking steps:

### 1. Deep Intent Deconstruction & Task Composition
**Core Principle**: Natural language requests describe the *substantive intent and desired outcome*, not rigid literal step-by-step instructions. Never mechanically map user phrasing into strict literal UI actions.

**Ask yourself**:
- *Task Composition*: What phases make up this task? Is it a sequence of linear steps, an iterative loop over items, or a monitoring/polling task?
- *Semantic Expansion*: What information or outcome is the user truly seeking? For example, "check battery" might mean opening Settings > Battery, using a quick-settings tile, or running an ADB command.
- *App Selection*: Which apps are needed? What package names or search terms map to those apps?

### 2. Visual Grounding & Starting Context
**Core Principle**: Every plan must be rooted in the current physical state of the device.

**Ask yourself**:
- *Current State*: What is visible on the provided screenshot? Home screen? An open app? A dialog?
- *Starting Point*: Does the plan need to navigate home first, or is the device already in the right state?

### 3. Tiered Strategy & Feasibility Safeguards
**Core Principle**: Formulate an efficient hierarchy of execution strategies.

**Ask yourself**:
- *Optimal Path*: What is the fastest way to achieve the goal (direct app launch, settings search, etc.)?
- *Fallback Path*: If the optimal path fails (app not found, UI changed), what is the backup approach?
- *Feasibility Bounds*: For iterative/search tasks, set explicit caps (e.g., "check top 10 results") to prevent infinite scrolling.

### 4. Risk Pre-mortem & Defensive Design
**Core Principle**: Proactively assume obstacles and build defenses into the milestones.

**Ask yourself**:
- *Common Blockers*: Login/auth walls, permission dialogs, popup ads, app updates/changes, network latency?
- *Embedded Defense*: What observable criteria confirm success for each milestone? How does the executor know a step worked vs. a loading spinner?

### 5. Milestone Granularity & Outcome-Orientation
**Core Principle**: 2-5 strategic macro-phases describing *what result to achieve*, not micromanaging atomic UI actions.

**Rules**:
- Top-level milestones describe outcomes ("Open Settings and navigate to Battery page")
- Sub-tasks (indented) describe concrete steps within a milestone
- Avoid click-by-click micromanagement — the executor is competent at finding and tapping elements"""

_PLANNER_CAPABILITIES = """## Execution Capabilities

The execution agent can perform these actions on the Android device:
- **open_app**: Launch any app by name or package name (e.g., "微信", "com.android.settings")
- **tap**: Tap at specific screen coordinates or on named UI elements
- **swipe**: Scroll up/down/left/right
- **type**: Enter text into input fields
- **system_button**: Press back, home, enter, recent apps
- **ADB shell**: Execute arbitrary shell commands on the device
- **long_press**: Long-press at coordinates
- **Screenshot analysis**: The agent sees the screen after each action

### Capability Boundaries
- App switching requires going home first, then launching the target app
- Text input requires the keyboard/input field to be focused first
- Complex multi-app workflows should be split into distinct milestones per app"""

_PLANNER_OUTPUT_PROTOCOL = f"""## Plan Formatting Rules

Your output must be ONLY the task plan in markdown format. No introductions, no explanations, no conversational text.

1. Format strictly as a Markdown task list starting with `- [ ]` for uncompleted items.
2. Respond with ONLY the plan text — no surrounding commentary.

{render_plan_grammar_spec()}

### Plan Structure Rules
- For linear milestones: `- [ ] <Phase Description>`
- For bounded repetitive/polling milestones: `- [ ] [Loop] <Goal> (Exit: <condition>; Interval: <cadence>)`
- For unbounded continuous monitoring: `- [ ] [Loop:continuous] <Goal> (Interval: <cadence>)`
- **Strict Two-Tier Hierarchy**: Sub-tasks MUST be 2-space indented under their parent `[Loop]` milestone
- Every `[Loop]` milestone MUST be initialized with at least one concrete first-instance sub-task
- NEVER flatten individual iterations to top-level milestones

### Milestone Writing Guidelines
- Each milestone describes a *completed state*, not an action
  - ✅ "WeChat app is open and conversation list is visible"
  - ❌ "Tap WeChat icon"
- Include enough context for the executor to understand what "done" looks like
- For verification-sensitive steps, describe the screen state that confirms success

### Example Plan Format
```
- [ ] Launch Settings app and navigate to Battery section
- [ ] Check battery percentage and health status
- [ ] Note the current battery level and estimated remaining time
```"""

_PLANNER_EXAMPLE = """## Example: Multi-App Workflow

**Goal**: "Open WeChat and send a message to Mom saying I'll be home at 7pm"

### Correct Approach (Outcome-Oriented Milestones):
```
- [ ] Navigate to home screen (press home if needed)
- [ ] Launch WeChat app
- [ ] Navigate to chat with Mom (search or select from conversation list)
- [ ] Type and send message: "I'll be home at 7pm"
```

### Incorrect Approach (Overly Granular / Action-Oriented):
```
- [ ] Press home button
- [ ] Tap WeChat icon at coordinates [160, 376]
- [ ] Wait for WeChat to load
- [ ] Tap search bar
- [ ] Type "Mom"
```
*(Fails: micromanages exact coordinates that may differ per device; treats UI micro-actions as milestones rather than strategic phases)*"""


def build_planner_system_prompt() -> str:
    """Assemble the full system prompt for plan generation.

    Combines role, thinking process, capabilities, output protocol, and example
    blocks — matching the assembly order from artemis/agents/planner/planner.json
    ``initial_plan`` mode.
    """
    return "\n\n".join(
        [
            _PLANNER_ROLE,
            _PLANNER_THINKING_PROCESS,
            _PLANNER_CAPABILITIES,
            _PLANNER_OUTPUT_PROTOCOL,
            _PLANNER_EXAMPLE,
        ]
    )


# ---------------------------------------------------------------------------
# Plan generation
# ---------------------------------------------------------------------------

def generate_plan(
    instruction: str,
    screenshot_path: str,
    vlm_caller: Callable[
        [str, str, str, int], tuple[Optional[str], str]
    ],
    max_retries: int = MAX_PLAN_RETRIES,
) -> dict:
    """Generate a milestone plan for the given instruction.

    Args:
        instruction: The natural-language task.
        screenshot_path: Path to a current device screenshot.
        vlm_caller: Function (system_prompt, user_prompt, image_path, max_tokens)
            → (response_text, finish_reason).
        max_retries: How many times to retry on parse failure.

    Returns:
        dict with keys:
          - ``plan_text``: The raw markdown plan text (or "" on failure)
          - ``plan_snapshot``: Parsed :class:`PlanSnapshot`
          - ``raw_response``: The VLM's raw response
          - ``success``: bool — whether a valid plan was generated
          - ``error``: Error message if failed
    """
    system_prompt = build_planner_system_prompt()
    user_prompt = f"Goal: {instruction}\n\nGenerate a task plan for this Android automation goal based on the screenshot showing the current device state."

    last_error = ""

    for attempt in range(1, max_retries + 1):
        raw, finish_reason = vlm_caller(
            system_prompt,
            user_prompt,
            screenshot_path,
            max_tokens=2048,
        )

        if not raw:
            last_error = f"VLM returned empty response (attempt {attempt})"
            continue

        # Extract plan text: try to find markdown task list
        plan_text = _extract_plan_text(raw)

        # Validate the plan
        is_valid, error_msg = validate_plan_format(plan_text)
        if is_valid:
            snapshot = parse_plan(plan_text)
            return {
                "plan_text": plan_text,
                "plan_snapshot": snapshot,
                "raw_response": raw,
                "success": True,
                "error": "",
            }

        last_error = f"Plan validation failed (attempt {attempt}): {error_msg}"

    # All retries exhausted
    return {
        "plan_text": "",
        "plan_snapshot": PlanSnapshot(),
        "raw_response": "",
        "success": False,
        "error": last_error,
    }


def _extract_plan_text(raw_response: str) -> str:
    """Extract the markdown plan from a VLM response that may contain extra text.

    Strategies (in order):
    1. If the response contains only checkbox lines, return it as-is.
    2. Try to find lines starting with ``- [`` and extract from the first one.
    3. Fall back to returning the raw response.
    """
    lines = raw_response.splitlines()
    checkbox_lines = [i for i, line in enumerate(lines) if CHECKBOX_LINE_RE.match(line)]

    if not checkbox_lines:
        return raw_response.strip()

    # Start from the first checkbox line
    first = checkbox_lines[0]

    # Look backward for a header/fence to strip
    start = first
    for i in range(first - 1, max(first - 5, -1), -1):
        if lines[i].strip().startswith("```"):
            start = i + 1
            break

    # Look forward for a closing fence
    plan_lines = []
    for i in range(start, len(lines)):
        line = lines[i]
        if line.strip().startswith("```"):
            break
        # If we started capturing and hit a plain text line after checkbox lines,
        # include it only if it's directly after a checkbox line and not clearly
        # conversational
        plan_lines.append(line)

    return "\n".join(plan_lines).strip()


# ---------------------------------------------------------------------------
# Plan-guided operator prompt
# ---------------------------------------------------------------------------

_OPERATOR_PLAN_SYSTEM_ADDENDUM = """## Plan Tracking
You are executing a multi-step task plan. The current plan state is shown in the
USER message. As you take actions, track progress by including a `plan_updates`
field in your JSON response:

- When you START working on a milestone, set it as active:
  `"plan_updates": {"set_active": "<exact text of the milestone>"}`

- When you COMPLETE a milestone, mark it done:
  `"plan_updates": {"mark_done": "<exact text of the milestone>"}`

- When a milestone is BLOCKED (impossible to complete), mark it:
  `"plan_updates": {"mark_blocked": {"text": "<milestone text>", "reason": "<why>"}}`

- If no milestone status change this step, omit `plan_updates` entirely.

The task is complete only when ALL top-level milestones are `[x]` done.
Use `{"action": "finish"}` to signal task completion."""


def build_operator_plan_system_prompt() -> str:
    """The plan-tracking addendum appended to the Operator's system prompt."""
    return _OPERATOR_PLAN_SYSTEM_ADDENDUM


def build_operator_plan_user_block(plan_text: str) -> str:
    """Render the current plan state as a user-prompt block for the Operator."""
    snapshot = parse_plan(plan_text)
    return snapshot.render_for_prompt()


# ---------------------------------------------------------------------------
# Plan update logic
# ---------------------------------------------------------------------------

def apply_plan_updates(
    plan_text: str,
    plan_updates: dict | None,
) -> tuple[str, bool]:
    """Apply VLM-reported plan status updates to the plan text.

    Args:
        plan_text: The current plan markdown.
        plan_updates: Dict from the VLM with optional ``set_active``,
            ``mark_done``, and ``mark_blocked`` keys.

    Returns:
        (updated_plan_text, changed) — ``changed`` is True if any status was
        actually modified.
    """
    if not plan_updates or not plan_text:
        return plan_text, False

    changed = False
    lines = plan_text.splitlines()
    snapshot = parse_plan(plan_text)

    # --- set_active ---
    target_active = plan_updates.get("set_active", "")
    if target_active:
        lines, snapshot, delta = _set_status(
            lines, snapshot, target_active, STATUS_ACTIVE
        )
        changed = changed or delta

    # --- mark_done ---
    target_done = plan_updates.get("mark_done", "")
    if target_done:
        lines, snapshot, delta = _set_status(
            lines, snapshot, target_done, STATUS_DONE
        )
        changed = changed or delta

    # --- mark_blocked ---
    blocked = plan_updates.get("mark_blocked")
    if blocked:
        if isinstance(blocked, dict):
            target_text = blocked.get("text", "")
            # Append reason to the line text
            reason = blocked.get("reason", "")
            lines, snapshot, delta = _set_status(
                lines, snapshot, target_text, STATUS_BLOCKED, suffix=f" — {reason}" if reason else ""
            )
            changed = changed or delta
        elif isinstance(blocked, str):
            lines, snapshot, delta = _set_status(
                lines, snapshot, blocked, STATUS_BLOCKED
            )
            changed = changed or delta

    return "\n".join(lines), changed


def _set_status(
    lines: list[str],
    snapshot: PlanSnapshot,
    target_text: str,
    new_status: str,
    suffix: str = "",
) -> tuple[list[str], PlanSnapshot, bool]:
    """Set the status of the item whose text best matches ``target_text``.

    Returns (updated_lines, updated_snapshot, changed).
    """
    target_lower = target_text.strip().lower()

    # Find best matching item (prefer top-level exact, then top-level substring,
    # then any exact, then any substring)
    candidates: list[tuple[int, PlanItem]] = []  # (score, item)

    for item in snapshot.items:
        item_lower = item.text.lower()

        if item_lower == target_lower:
            # Bonus for top-level exact match
            candidates.append((100 if item.is_top_level else 80, item))
        elif target_lower in item_lower or item_lower in target_lower:
            candidates.append((50 if item.is_top_level else 30, item))
        else:
            # Fuzzy: check word overlap
            target_words = set(target_lower.split())
            item_words = set(item_lower.split())
            overlap = target_words & item_words
            if len(overlap) >= 2:
                candidates.append((10, item))

    if not candidates:
        return lines, snapshot, False

    # Pick highest-scoring match
    candidates.sort(key=lambda x: x[0], reverse=True)
    best = candidates[0][1]

    if best.status == new_status:
        return lines, snapshot, False

    # Update the line
    line_idx = best.line_no
    old_line = lines[line_idx]
    new_line = CHECKBOX_LINE_RE.sub(
        lambda m: f"{m.group('indent')}- [{new_status}] {m.group('text').strip()}{suffix}",
        old_line,
    )
    lines[line_idx] = new_line

    # Rebuild snapshot for next query
    new_snapshot = parse_plan("\n".join(lines))
    return lines, new_snapshot, True


def is_task_complete(plan_text: str) -> bool:
    """Check if all top-level milestones are done."""
    snapshot = parse_plan(plan_text)
    return snapshot.all_top_level_done