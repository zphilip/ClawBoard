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
