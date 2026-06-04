from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StateSignature:
    foreground_pkg: str
    ui_fingerprint: str
    intent_signature: str
    device_bucket: str = "default"


@dataclass
class ActionCandidate:
    action_type: str
    arguments: dict[str, Any]
    confidence: float = 0.0
    source: str = "llm"
    action_description: str = ""


@dataclass
class MemoryRecord:
    state_key: str
    intent_key: str
    action_type: str
    action_args: dict[str, Any]
    expected_transition: str = ""
    success_count: int = 0
    fail_count: int = 0
    forbidden: bool = False
    reason: str = ""
    updated_at: float = 0.0
    source_run_id: str = ""
    source_step: int = -1
    action_description: str = ""


@dataclass
class DecisionInput:
    state: StateSignature
    proposed_action: ActionCandidate | None = None


@dataclass
class DecisionOutput:
    use_cached_action: bool
    action: ActionCandidate | None = None
    blocked: bool = False
    reason: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Task-Level Plan models
# ---------------------------------------------------------------------------

@dataclass
class PlanStep:
    """One step in a recorded task plan.

    Stores the action to replay plus a lightweight verification signal
    (expected foreground package after the action) so the runner can
    confirm the step worked without calling the VLM.
    """
    step_index: int
    action_type: str                       # click, type, open, system_button, swipe, key ...
    action_args: dict[str, Any]            # normalised 0-1000 coordinates, text, button ...
    expected_pkg: str = ""                 # foreground package expected AFTER this step
    action_description: str = ""           # human-readable summary (from VLM reasoning)
    pre_action_pkg: str = ""               # foreground package BEFORE this step (for extra validation)


@dataclass
class TaskPlan:
    """A complete, ordered plan for a recurring task.

    Keyed by ``intent_key`` (a canonical, normalised representation of the
    user instruction).  Multiple plans may exist for the same intent_key
    (e.g. different UI paths); the best one is chosen by ``score``.
    """
    intent_key: str                        # canonical intent hash (groups similar instructions)
    instruction_sample: str                # one representative instruction text
    steps: list[PlanStep] = field(default_factory=list)
    success_count: int = 0                 # how many times this plan completed successfully
    fail_count: int = 0                    # how many times replay failed mid-way
    last_verified: float = 0.0             # epoch timestamp of last successful replay
    created_at: float = 0.0                # epoch timestamp of plan creation
    source_run_id: str = ""                # run_id that originally recorded the plan
    device_bucket: str = "default"
