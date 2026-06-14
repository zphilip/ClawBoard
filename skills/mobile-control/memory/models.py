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
    target_element_signature: dict[str, Any] | None = None  # For drift validation
    original_screen_resolution: tuple[int, int] | None = None  # For drift validation

    _TUPLE_FIELDS: tuple[str, ...] = ("original_screen_resolution",)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict with stable field types.

        JSON has no native tuple type — ``__dict__`` would silently convert
        tuples to lists, breaking downstream code that expects tuples on
        reload.  This method is intentionally a shallow copy so that
        ``target_element_signature`` (a nested dict) round-trips correctly.
        """
        d = dict(self.__dict__)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MemoryRecord":
        """Deserialize from a dict, restoring tuple fields from JSON lists."""
        for field_name in cls._TUPLE_FIELDS:
            value = d.get(field_name)
            if isinstance(value, list):
                d[field_name] = tuple(value)
        return cls(**d)


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

    For click actions, ``target_element_signature`` captures the UI
    element that was targeted (resource-id, text, bounds).  On replay
    the executor attempts to locate this element on the current screen
    and tap its centre — coordinates become resilient to layout drift.
    """
    step_index: int
    action_type: str                       # click, type, open, system_button, swipe, key ...
    action_args: dict[str, Any]            # normalised 0-1000 coordinates, text, button ...
    expected_pkg: str = ""                 # foreground package expected AFTER this step
    action_description: str = ""           # human-readable summary (from VLM reasoning)
    pre_action_pkg: str = ""               # foreground package BEFORE this step
    post_action_ui_fp: str = ""            # UI fingerprint AFTER this step
    target_element_signature: dict[str, Any] | None = None  # For element-based targeting
    success_count: int = 0                 # times this step replayed successfully
    fail_count: int = 0                    # times this step failed during replay


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
