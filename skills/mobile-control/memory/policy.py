from __future__ import annotations

from .models import ActionCandidate, DecisionInput, DecisionOutput
from .retriever import top_matches
from .store import JsonlMemoryStore


class MemoryPolicy:
    """Decision policy placeholder.

    Not wired into runtime yet; safe for incremental adoption.
    """

    def __init__(self, store: JsonlMemoryStore, min_score: float = 0.7):
        self.store = store
        self.min_score = min_score

    def decide(self, state_key: str, intent_key: str, dinput: DecisionInput) -> DecisionOutput:
        records = self.store.load()
        ranked = top_matches(records, state_key=state_key, intent_key=intent_key, limit=5)

        if not ranked:
            return DecisionOutput(use_cached_action=False, reason="no_memory_match")

        best = ranked[0]
        if best.record.forbidden:
            return DecisionOutput(
                use_cached_action=False,
                blocked=True,
                reason="action_blocked_by_negative_memory",
                diagnostics={"score": best.score},
            )

        if best.score < self.min_score:
            return DecisionOutput(
                use_cached_action=False,
                reason="score_below_threshold",
                diagnostics={"score": best.score},
            )

        action = ActionCandidate(
            action_type=best.record.action_type,
            arguments=best.record.action_args,
            confidence=max(min(best.score, 1.0), 0.0),
            source="memory",
        )
        return DecisionOutput(
            use_cached_action=True,
            action=action,
            reason="memory_hit",
            diagnostics={"score": best.score},
        )
