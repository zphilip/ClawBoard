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


def _log(msg: str) -> None:
    """Log sink. ``artemis_agent`` rebinds this to its own logger on import."""
    return None

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

# Check lines are indented attachments under a top-level subgoal
# (``- verify: ...`` / ``- assert: ...``) or task-level free items at zero
# indent. The optional ``@end`` suffix moves the judgment moment from the
# anchored subgoal's completion to task exit (mirrors upstream
# artemis/utils/plan_grammar.py CHECK_LINE_RE).
CHECK_LINE_RE = re.compile(
    r"^(?P<indent>\s*)-\s*(?P<kind>verify|assert)(?P<at_end>@end)?\s*:\s*(?P<text>\S.*?)\s*$"
)

#: Strategy-ladder lines: alternative implementations of the SAME milestone,
#: written as siblings of the milestone (zero-indent), never as sub-tasks.
#:   - path 1 [cost=1 kind=primary]: 按音量键调出面板 → 点铃铛
#: ``cost`` is the number of UI actions (effort); ``kind`` is how faithful the
#: path is to the requested state (primary / alternative / equivalent). They are
#: SEPARATE axes: DND is cheap in actions but only semantically equivalent, so it
#: must sort last despite a low cost.
PATH_LINE_RE = re.compile(
    r"^(?P<indent>\s*)-\s*path\s*(?P<idx>\d+)\s*"
    r"(?:\[(?P<attrs>[^\]]*)\])?\s*:\s*(?P<text>\S.*?)\s*$"
)

#: ``cost=3`` / ``kind=primary`` inside the attribute brackets (order-agnostic).
PATH_ATTR_RE = re.compile(r"(?P<key>cost|kind)\s*=\s*(?P<value>[A-Za-z]+|\d+)", re.IGNORECASE)

#: Semantic-fidelity tiers, best first. This is the PRIMARY sort key.
PATH_KIND_RANK = {"primary": 0, "alternative": 1, "equivalent": 2}
PATH_KINDS = tuple(PATH_KIND_RANK)

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

    @property
    def key(self) -> str:
        """Stable identity hash of the item text (mirrors upstream subgoal_hash)."""
        import hashlib
        return hashlib.md5(self.text.strip().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CheckItem:
    """One declared check line of the plan's machine channel.

    ``verify`` marks a completion acceptance criterion (a failure triggers
    repair); ``assert`` marks a test assertion (a failure is recorded as a
    legitimate test result, never repaired). Mirrors upstream
    ``artemis/utils/plan_grammar.py CheckItem``.
    """

    kind: str  # "verify" | "assert"
    when: str  # "on_complete" | "at_end"
    text: str
    parent_key: Optional[str] = None

    @property
    def is_verify(self) -> bool:
        return self.kind == "verify"

    @property
    def is_assert(self) -> bool:
        return self.kind == "assert"


@dataclass(frozen=True)
class StrategyPath:
    """One alternative implementation of a milestone (part of the ladder).

    Only ONE path is ever executed for a milestone. The two attributes are
    deliberately orthogonal:

    - ``kind`` — how faithful the path is to the requested state.
      ``primary`` (the state itself) → ``alternative`` (same state, other route)
      → ``equivalent`` (a related state with side effects, e.g. DND for silent).
    - ``cost`` — how many UI actions it takes (1–10), used to order paths
      *within* a tier.

    Ordering is ``(kind_rank, cost, index)``, so a cheap-but-only-equivalent
    path (DND = 2 actions) still runs after an expensive-but-exact one.
    """

    index: int       # declared order (1-based)
    cost: int        # lower = tried first WITHIN the same kind
    text: str        # the concrete approach
    parent_key: str  # owning milestone
    kind: str = "alternative"  # primary | alternative | equivalent

    @property
    def kind_rank(self) -> int:
        return PATH_KIND_RANK.get(self.kind, 1)

    @property
    def is_fallback(self) -> bool:
        """Last-resort path: reaches a *related* state, not the requested one."""
        return self.kind == "equivalent"


@dataclass(frozen=True)
class PlanSnapshot:
    """Read-only derived view of a plan. Never persisted, never written back."""

    items: tuple[PlanItem, ...] = field(default_factory=tuple)
    check_items: tuple[CheckItem, ...] = field(default_factory=tuple)
    paths: tuple[StrategyPath, ...] = field(default_factory=tuple)

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

    def check_items_of(self, item: PlanItem) -> tuple[CheckItem, ...]:
        """Check lines anchored to the given top-level subgoal."""
        if not item.is_top_level:
            return ()
        return tuple(ci for ci in self.check_items if ci.parent_key == item.key)

    @property
    def task_level_check_items(self) -> tuple[CheckItem, ...]:
        """``verify@end`` / ``assert@end`` lines at zero indent (task level)."""
        return tuple(ci for ci in self.check_items if ci.parent_key is None)

    @property
    def all_check_items(self) -> tuple[CheckItem, ...]:
        """Every declared check item, regardless of anchor."""
        return self.check_items

    def paths_of(self, item: "PlanItem") -> tuple[StrategyPath, ...]:
        """The strategy ladder for a milestone, in execution order.

        Sorted by ``(kind_rank, cost, index)``: semantic fidelity first, effort
        second. A cheap-but-only-equivalent path therefore never pre-empts an
        exact one.
        """
        if not item.is_top_level:
            return ()
        ladder = [p for p in self.paths if p.parent_key == item.key]
        return tuple(
            sorted(ladder, key=lambda p: (p.kind_rank, p.cost, p.index))
        )

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

            # Strategy ladder: show the alternatives under their milestone so the
            # Operator (and the flash profile) can see them; the executor tells
            # the Operator which ONE to follow.
            if item.is_top_level:
                for p in self.paths_of(item):
                    lines.append(
                        f"    ↳ path {p.index} [{p.kind}, cost={p.cost}]: {p.text}"
                    )

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

    Non-checkbox lines are ignored for milestone tracking, EXCEPT check lines
    (``- verify:`` / ``- assert:``), which are extracted into
    :class:`CheckItem` entries anchored to the nearest top-level milestone:
    indented check lines attach to that milestone (``on_complete``), zero-indent
    lines are task-level (``at_end`` equivalent).
    """
    items: list[PlanItem] = []
    checks: list[CheckItem] = []
    paths: list[StrategyPath] = []
    current_top: Optional[PlanItem] = None
    if content:
        for line_no, line in enumerate(content.splitlines()):
            match = CHECKBOX_LINE_RE.match(line)
            if match:
                item = PlanItem(
                    indent=len(match.group("indent")),
                    status=match.group("status"),
                    text=match.group("text").strip(),
                    line_no=line_no,
                )
                items.append(item)
                if item.is_top_level:
                    current_top = item
                continue
            check_match = CHECK_LINE_RE.match(line)
            if check_match:
                indented = bool(check_match.group("indent"))
                at_end = bool(check_match.group("at_end"))
                parent_key = current_top.key if (indented and current_top) else None
                checks.append(
                    CheckItem(
                        kind=check_match.group("kind"),
                        when="at_end" if at_end else "on_complete",
                        text=check_match.group("text").strip(),
                        parent_key=parent_key,
                    )
                )
                continue
            path_match = PATH_LINE_RE.match(line)
            if path_match and current_top is not None:
                try:
                    idx = int(path_match.group("idx"))
                except (TypeError, ValueError):
                    idx = len(paths) + 1
                cost: Optional[int] = None
                kind = ""
                for attr in PATH_ATTR_RE.finditer(path_match.group("attrs") or ""):
                    key = attr.group("key").lower()
                    value = attr.group("value").strip().lower()
                    if key == "cost":
                        try:
                            cost = int(value)
                        except ValueError:
                            cost = None
                    elif key == "kind" and value in PATH_KIND_RANK:
                        kind = value
                resolved_cost = cost if cost is not None else idx
                if not kind:
                    # Back-compat with plans that only carry [cost=K]: a very high
                    # cost was the old way of marking "last resort".
                    kind = "equivalent" if resolved_cost >= 8 else "alternative"
                paths.append(
                    StrategyPath(
                        index=idx,
                        cost=resolved_cost,
                        text=path_match.group("text").strip(),
                        parent_key=current_top.key,
                        kind=kind,
                    )
                )
    return PlanSnapshot(items=tuple(items), check_items=tuple(checks), paths=tuple(paths))


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
- The task terminates only when every top-level milestone is `[{STATUS_DONE}]`.

### Acceptance check lines (for state-change / verification-sensitive tasks)
Under a milestone you MAY write check lines (2-space indented) that declare how
the milestone's success is judged. They are NEVER milestones; they are the
acceptance contract the Checker audits:

- `- verify: <observable acceptance criterion>` — a completion acceptance
  criterion. The final Checker judges it against device state; a failed verify
  sends the milestone back for repair.
- `- assert: <test expectation>` — a test assertion recorded truthfully, never
  repaired.
- `- verify@end: <criterion>` (zero indent) — judged at task exit against the
  final device state.

For state-change tasks (mute, Wi-Fi, brightness, DND, airplane mode, …) ALWAYS
add a `- verify:` line that names a READ-ONLY device-state probe, not a visual
look. Preferred probe tags: `[probe: volume ring]` (ring volume; 0 = silent),
`[probe: audio]` (ringer mode), `[probe: setting global zen_mode]`,
`[probe: setting system screen_brightness]`, `[probe: setting global wifi_on]`,
`[probe: foreground]`, `[probe: battery]`.

### Strategy Ladder (multiple ways to reach the SAME milestone)

When a milestone changes device STATE and more than one approach exists, you MUST
emit an ordered ladder of 2–5 `- path` lines as SIBLINGS of the milestone. The
`verify` line is the SHARED acceptance test for every path.

```
- [ ] 将手机切换到静音模式（铃声静音）
  - verify: 响铃音量为 0 或铃声模式为静音 [probe: volume ring]
  - path 1 [cost=1 kind=primary]:     按 system_button volume_mute 直接切换静音
  - path 2 [cost=3 kind=primary]:     连续按 system_button volume_down（3~5 次）直到铃声模式变为静音
  - path 3 [cost=3 kind=primary]:     设置 → 声音与振动 → 响铃/振动/静音 单选 → 静音
  - path 4 [cost=5 kind=alternative]: 无静音控件 → set_slider 把铃声滑条设为 0 + 关闭「静音时振动」
  - path 5 [cost=2 kind=equivalent]:  下拉快捷面板 → 点「勿扰」（会静音铃声，但连通知/闹钟一起压掉）
```

### The two attributes are INDEPENDENT — do not merge them

`kind` = how faithful the path is to the requested state (the PRIMARY sort key):
- `primary` — reaches the requested state itself.
- `alternative` — reaches the SAME state by a different route (drag sliders to 0
  for "silent" is the same state; only the route differs).
- `equivalent` — reaches a RELATED but different state, with side effects. You
  MUST name the side effect in the path text. 勿扰模式 for "静音" is
  `equivalent`: the ringer stops, but notifications and alarms are suppressed
  too. It is cheap in actions yet still the LAST resort.

`cost` = number of UI actions (1–10), secondary key, orders paths within a tier:
- each tap / long-press / swipe / key press / app launch counts 1
- entering a second-level screen adds 1
- examples: 下拉面板+点磁贴 = 2；设置→声音与振动→点静音 = 3；
  打开面板+拖 3 条滑条 = 4

### Direction rule for the FIRST action of a path
**Never open a path with an action that INCREASES the quantity you want to
reduce.** "Press VOLUME_UP → tap the bell" is invalid for a mute goal: the first
action raises the ringer, the guardrail rejects the whole path, and the bell is
never reached. Move toward the target instead — `volume_down` / `volume_mute`,
or the settings radio.

The harness sorts by `(kind, cost)`. So a **cheap but only-equivalent** path
(e.g. 勿扰 at cost=2) runs AFTER an **expensive but exact** one — never score
`equivalent` paths as expensive to force them last; mark them `kind=equivalent`.
Example:

```
- [ ] 将手机设置为静音
  - verify: 响铃音量已降到 0 且铃声模式为静音 [probe: audio]
```"""


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
- **Two-Tier Hierarchy**: Sub-tasks MUST be 2-space indented under their milestone
- `- path N [cost=K]:` lines are **SIBLINGS of the milestone** (zero indent), never
  sub-tasks and never milestones: they are ALTERNATIVE IMPLEMENTATIONS of the same
  milestone, and exactly ONE of them is executed
- NEVER express alternatives as multiple sub-tasks or multiple milestones
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

_PLANNER_STATE_CHANGE_EXAMPLE = """## Example: State-Change Task (verify line + strategy ladder)

**Goal**: "把手机设置成静音"

### Correct Approach (outcome milestone + scriptable acceptance + ladder):
```
- [ ] 将手机切换到静音模式（铃声静音）
  - verify: 响铃音量为 0 或铃声模式为静音 [probe: volume ring]
  - path 1 [cost=1 kind=primary]:     按 system_button volume_mute 直接切换静音
  - path 2 [cost=3 kind=primary]:     连续按 system_button volume_down（3~5 次）直到铃声模式变为静音
  - path 3 [cost=3 kind=primary]:     设置 → 声音与振动 → 响铃/振动/静音 单选 → 静音
  - path 4 [cost=5 kind=alternative]: 无静音控件 → set_slider 把铃声滑条设为 0 + 关闭「静音时振动」
  - path 5 [cost=2 kind=equivalent]:  下拉快捷面板 → 点「勿扰」（副作用：通知与闹钟一并静音）
```

*(Note path 4: 勿扰 needs only 2 actions — it is CHEAP — but it is marked
`kind=equivalent` because it suppresses notifications and alarms as well. Sorting
is by (kind, cost), so it still runs last even though it is the cheapest.)*

### Incorrect Approach (symptoms you must avoid):
```
- [ ] 找到并点击"静音"按钮
- [ ] 若无静音按钮则进入声音设置
- [ ] 若仍失败则拖动音量条
```
*(Fails three ways: it invents a "静音" button that many skins do not have; the
alternatives are listed as separate STEPS rather than `- path` lines, so the
executor will do all of them; and there is no acceptance test, so nothing can
tell whether the phone actually ended up silent.)*"""


#: Milestone-level check items whose probe reads persistent STATE. A milestone
#: carrying one of these is a state-change milestone and therefore needs a
#: strategy ladder (see ``missing_ladder_milestones``).
_STATE_PROBE_KINDS = frozenset({"volume", "audio", "setting"})


def missing_ladder_milestones(snapshot: "PlanSnapshot") -> list:
    """State-change milestones that declare fewer than two ``- path`` lines.

    Prompt instructions alone do not reliably produce a ladder, so the caller
    re-asks the model ONCE with this list. Only milestones whose acceptance is a
    persistent-state probe are checked: a linear milestone legitimately has no
    alternatives.
    """
    missing = []
    for item in snapshot.top_level:
        checks = snapshot.check_items_of(item)
        if not checks:
            continue
        is_state = False
        for ci in checks:
            if ci.kind != "verify":
                continue
            try:
                from runner.probes import derive_probes

                if any(kind in _STATE_PROBE_KINDS for kind, _params in derive_probes(ci.text)):
                    is_state = True
                    break
            except Exception:
                continue
        if is_state and len(snapshot.paths_of(item)) < 2:
            missing.append(item)
    return missing


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
            _PLANNER_STATE_CHANGE_EXAMPLE,
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
    max_retries: int = 2,
    timeout: float = 90.0,
    total_budget: float = 150.0,
    thinking: bool = False,
) -> dict:
    """Generate a milestone plan for the given instruction.

    Cost control (this used to take 8 minutes):
    - ``max_retries`` defaults to **2**: enough to survive one transient
      gateway hiccup (a plan failure downgrades pro → flash, which loses the
      safety net AND the probe-backed Checker), while staying bounded.
    - ``timeout`` is the per-HTTP-call socket timeout.
    - ``total_budget`` is a hard WALL-CLOCK deadline. The previous code nested
      three retry layers (generate_plan × _call_vlm × plan-parse) with a 120 s
      socket timeout each — a slow gateway could spend up to 21 HTTP calls, and
      the 120 s socket timeout does not bound total wall time because it applies
      per blocking socket operation, not per request.
    - ``thinking`` is passed to the caller as ``False`` by default: the planner
      only needs a ~10-line markdown answer, so a reasoning pass on a ~3.9k-token
      system prompt is pure latency.

    Args:
        instruction: The natural-language task.
        screenshot_path: Path to a current device screenshot.
        vlm_caller: Function (system_prompt, user_prompt, image_path, max_tokens)
            → (response_text, finish_reason). Extra keyword arguments (timeout,
            retries, thinking) are passed when the caller accepts them.
        max_retries: How many times to retry on parse failure.
        timeout: Per-call socket timeout in seconds.
        total_budget: Wall-clock budget for the whole planning step.
        thinking: Whether to let the model reason before answering.

    Returns:
        dict with keys:
          - ``plan_text``: The raw markdown plan text (or "" on failure)
          - ``plan_snapshot``: Parsed :class:`PlanSnapshot`
          - ``raw_response``: The VLM's raw response
          - ``success``: bool — whether a valid plan was generated
          - ``error``: Error message if failed
          - ``elapsed_s``: Wall-clock time spent
    """
    import time as _time

    started = _time.monotonic()
    deadline = started + max(1.0, float(total_budget))
    system_prompt = build_planner_system_prompt()
    user_prompt = f"Goal: {instruction}\n\nGenerate a task plan for this Android automation goal based on the screenshot showing the current device state."

    last_error = ""
    corrective = False  # the one-shot strategy-ladder re-ask has not fired yet

    def _budget_spent() -> bool:
        """True when less than a couple of seconds remain in the total budget."""
        return (deadline - _time.monotonic()) <= 2.0

    def _call(prompt_timeout: float) -> tuple[Optional[str], str]:
        """Call the VLM, passing the optional knobs only if it accepts them."""
        kwargs = {"timeout": prompt_timeout, "retries": 0, "thinking": thinking}
        try:
            return vlm_caller(
                system_prompt, user_prompt, screenshot_path, 4096, **kwargs
            )
        except TypeError:
            # Older/test callers take only (system, user, image, max_tokens).
            return vlm_caller(system_prompt, user_prompt, screenshot_path, 4096)

    for attempt in range(1, max(1, max_retries) + 1):
        remaining = deadline - _time.monotonic()
        if remaining <= 1.0:
            last_error = (
                f"Planning budget of {total_budget:.0f}s exhausted before attempt {attempt}"
            )
            break

        raw, finish_reason = _call(min(float(timeout), remaining))

        elapsed = _time.monotonic() - started
        if not raw:
            last_error = (
                f"VLM returned empty response (attempt {attempt}, {elapsed:.1f}s)"
            )
            continue

        if finish_reason == "length":
            last_error = (
                f"Planner VLM truncated output at length limit (attempt {attempt}, {elapsed:.1f}s)"
            )
            continue

        # Extract plan text: try to find markdown task list
        plan_text = _extract_plan_text(raw)

        # Validate the plan
        is_valid, error_msg = validate_plan_format(plan_text)
        if is_valid:
            snapshot = parse_plan(plan_text)
            # A state-change milestone without a strategy ladder means the
            # executor has no fallback when the chosen UI control does not exist
            # on this skin. Re-ask ONCE (asking twice is what made planning slow
            # before); the wall-clock budget still applies.
            missing = [] if corrective else missing_ladder_milestones(snapshot)
            if missing and attempt < max(1, max_retries) and not _budget_spent():
                titles = "\n".join(f'- "{i.text}"' for i in missing)
                _log(
                    f"[planner] {len(missing)} state milestone(s) lack a strategy"
                    " ladder — re-asking once with a corrective instruction"
                )
                corrective = True
                last_error = "plan lacked a strategy ladder for state milestones"
                user_prompt = (
                    f"Goal: {instruction}\n\n"
                    "Your previous plan was rejected: the milestones below change "
                    "device state but declare fewer than two `- path` lines, so the "
                    "executor has no fallback if this device skin lacks that "
                    "control. Rewrite the WHOLE plan, adding an ordered "
                    "`- path N [cost=K]:` ladder (cheapest first, 2–5 paths, "
                    "including a settings-radio path and a semantically equivalent "
                    "fallback) as siblings of each milestone below:\n"
                    f"{titles}\n\n"
                    "Keep the existing `- verify:` acceptance line as the shared "
                    "test for every path. Output ONLY the corrected plan."
                )
                continue
            return {
                "plan_text": plan_text,
                "plan_snapshot": snapshot,
                "raw_response": raw,
                "success": True,
                "error": "",
                "elapsed_s": _time.monotonic() - started,
            }

        last_error = f"Plan validation failed (attempt {attempt}): {error_msg}"

    # All retries exhausted
    return {
        "plan_text": "",
        "plan_snapshot": PlanSnapshot(),
        "raw_response": "",
        "success": False,
        "error": last_error,
        "elapsed_s": _time.monotonic() - started,
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

    # ── Normalize VLM output ────────────────────────────────────────────
    # VLMs sometimes return lists like ["text"] or dicts like {"text": "..."}
    # instead of plain strings.  Normalize everything up front.
    def _normalize(val):
        """Normalize a plan_update target to a plain string."""
        if isinstance(val, str):
            return val.strip()
        if isinstance(val, list) and val:
            # Take first element if it's a string, or join strings
            first = val[0]
            if isinstance(first, str):
                return first.strip()
        if isinstance(val, dict):
            return (val.get("text") or val.get("title") or "").strip()
        return ""

    changed = False
    lines = plan_text.splitlines()
    snapshot = parse_plan(plan_text)

    # --- set_active ---
    target_active = _normalize(plan_updates.get("set_active", ""))
    if target_active:
        lines, snapshot, delta = _set_status(
            lines, snapshot, target_active, STATUS_ACTIVE
        )
        changed = changed or delta

    # --- mark_done ---
    target_done = _normalize(plan_updates.get("mark_done", ""))
    if target_done:
        lines, snapshot, delta = _set_status(
            lines, snapshot, target_done, STATUS_DONE
        )
        changed = changed or delta

    # --- mark_blocked ---
    blocked = plan_updates.get("mark_blocked")
    if blocked:
        if isinstance(blocked, dict):
            target_text = _normalize(blocked.get("text", ""))
            reason = blocked.get("reason", "")
            lines, snapshot, delta = _set_status(
                lines, snapshot, target_text, STATUS_BLOCKED, suffix=f" — {reason}" if reason else ""
            )
            changed = changed or delta
        elif isinstance(blocked, list):
            # VLM may return mark_blocked as a list of dicts or strings
            for item in blocked:
                if isinstance(item, dict):
                    target_text = _normalize(item)
                    reason = item.get("reason", "")
                    lines, snapshot, delta = _set_status(
                        lines, snapshot, target_text, STATUS_BLOCKED,
                        suffix=f" — {reason}" if reason else "",
                    )
                    changed = changed or delta
                elif isinstance(item, str):
                    lines, snapshot, delta = _set_status(
                        lines, snapshot, item, STATUS_BLOCKED,
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