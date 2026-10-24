"""Execution incident tracking — structured failure context.

Ported from Artemis core:
  - artemis/agents/validator/incidents.py

Incidents persist in the agent's context across turns: a blocked or failed
action opens an incident that stays active until a subsequent action succeeds.
The Operator (VLM) receives the incident in its next prompt and self-heals
without a separate repair agent.

Consecutive failures increment; a successful action clears the incident.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any


#: The safety net refused to execute the action.
KIND_SAFETY_NET = "safety_net"
#: The action was dispatched but the device/executor reported a failure.
KIND_EXEC_ERROR = "exec_error"


@dataclass
class ExecutionIncident:
    """One blocked or failed turn-ending action, reported to the VLM."""

    kind: str                              # safety_net | exec_error
    category: str                          # Failure category from ValidationErrorCategory
    reason: str                            # Human-readable reason
    action: str                            # Action type that failed
    action_args: dict[str, Any] = field(default_factory=dict)
    action_description: str = ""
    action_index: int = 0                  # Position within a burst
    burst_size: int = 1                    # How many actions were in the burst
    step_number: int | None = None         # The step when it happened
    consecutive_failures: int = 1          # How many consecutive failures
    evidence: dict[str, Any] = field(default_factory=dict)
    opened_at: float = field(default_factory=time.time)
    recovery_hints: list[str] = field(default_factory=list)

    @property
    def is_burst(self) -> bool:
        """Was this incident from a multi-action burst?"""
        return self.burst_size > 1

    @property
    def age_seconds(self) -> float:
        """How long ago this incident was opened."""
        return time.time() - self.opened_at

    def render_for_prompt(self) -> str:
        """Render the incident as a prompt block for the next VLM call."""
        lines = [
            "--- Previous Action Was Blocked ---",
            f"Category: {self.category}",
            f"Reason: {self.reason}",
        ]
        if self.consecutive_failures > 1:
            lines.append(f"Consecutive failures: {self.consecutive_failures}")
        if self.recovery_hints:
            lines.append("Recovery hints:")
            for hint in self.recovery_hints:
                lines.append(f"  • {hint}")
        if self.action_description:
            lines.append(f"Failed action: {self.action_description}")
        lines.append("Please try a different approach to achieve the goal.")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON emission."""
        return {
            "kind": self.kind,
            "category": self.category,
            "reason": self.reason,
            "action": self.action,
            "action_description": self.action_description,
            "step_number": self.step_number,
            "consecutive_failures": self.consecutive_failures,
            "age_seconds": round(self.age_seconds, 1),
        }


class IncidentTracker:
    """Tracks execution incidents across turns.

    A blocked action opens an incident; it stays open until a later action
    succeeds. Consecutive failures increment the count. The active incident
    is rendered into the next VLM prompt so the agent can self-heal.
    """

    def __init__(self):
        self._active_incident: ExecutionIncident | None = None
        self._history: list[ExecutionIncident] = []
        self._consecutive_failures: int = 0

    @property
    def has_active_incident(self) -> bool:
        return self._active_incident is not None

    @property
    def active_incident(self) -> ExecutionIncident | None:
        return self._active_incident

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def count(self) -> int:
        return len(self._history)

    def add_incident(
        self,
        category: str,
        reason: str,
        action: str,
        step: int,
        action_args: dict[str, Any] | None = None,
        action_description: str = "",
        kind: str = KIND_SAFETY_NET,
        evidence: dict[str, Any] | None = None,
    ) -> ExecutionIncident:
        """Record a new incident. Updates consecutive failure count."""
        self._consecutive_failures += 1

        incident = ExecutionIncident(
            kind=kind,
            category=category,
            reason=reason,
            action=action,
            action_args=action_args or {},
            action_description=action_description or f"{action} ({category})",
            step_number=step,
            consecutive_failures=self._consecutive_failures,
            evidence=evidence or {},
        )

        self._active_incident = incident
        self._history.append(incident)
        return incident

    def add_recovery_hint(self, hint: str) -> None:
        """Add a recovery hint to the active incident."""
        if self._active_incident:
            self._active_incident.recovery_hints.append(hint)

    def clear_on_success(self) -> None:
        """Clear the active incident after a successful action."""
        self._active_incident = None
        self._consecutive_failures = 0

    def render_for_prompt(self) -> str:
        """Render the active incident for the next VLM prompt."""
        if not self._active_incident:
            return ""
        return self._active_incident.render_for_prompt()

    def get_recent(self, n: int = 5) -> list[ExecutionIncident]:
        """Get the N most recent incidents."""
        return self._history[-n:]

    def reset(self) -> None:
        """Reset all incident state."""
        self._active_incident = None
        self._history.clear()
        self._consecutive_failures = 0