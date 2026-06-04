from __future__ import annotations

import json

from .models import ActionCandidate, DecisionInput, DecisionOutput
from .retriever import top_matches
from .store import JsonlMemoryStore

# Actions that are passive, terminal, or user-dependent.  These must never be
# replayed from cache — they are logged for telemetry only.  Kept here (not
# just in the runner) so stale records written before the write-time filter
# existed are still blocked at read time.
NON_CACHEABLE_ACTIONS: frozenset[str] = frozenset({
    "wait",
    "answer",
    "terminate",
    "interact",
    "call_user",
    "calluser",
})


def record_replay_sig(state_key: str, action_type: str, action_args: dict) -> str:
    """Build a deterministic signature for a cached record, used to track
    which records have already been replayed in the current run."""
    return f"{state_key}|{action_type}|{json.dumps(action_args, ensure_ascii=False, sort_keys=True)}"


class MemoryPolicy:
    """Decision policy placeholder.

    Not wired into runtime yet; safe for incremental adoption.
    """

    def __init__(self, store: JsonlMemoryStore, min_score: float = 0.7):
        self.store = store
        self.min_score = min_score

    def decide(
        self,
        state_key: str,
        intent_key: str,
        dinput: DecisionInput,
        exclude_sigs: set[str] | None = None,
    ) -> DecisionOutput:
        records = self.store.load()
        ranked = top_matches(records, state_key=state_key, intent_key=intent_key, limit=5)

        # Filter out non-cacheable action types at read time.  Forbidden
        # records are kept so they can still block known-bad actions.
        ranked = [
            r for r in ranked
            if r.record.forbidden or r.record.action_type not in NON_CACHEABLE_ACTIONS
        ]

        if not ranked:
            return DecisionOutput(use_cached_action=False, reason="no_memory_match")

        # Iterate through ranked records, skipping already-replayed ones.
        # This allows the fastpath to return the next best unused cached action
        # when the top match was already replayed earlier in the same run.
        for candidate in ranked:
            sig = record_replay_sig(
                state_key, candidate.record.action_type, candidate.record.action_args,
            )
            if exclude_sigs and sig in exclude_sigs:
                continue

            if candidate.record.forbidden:
                return DecisionOutput(
                    use_cached_action=False,
                    blocked=True,
                    reason="action_blocked_by_negative_memory",
                    diagnostics={"score": candidate.score},
                )

            if candidate.score < self.min_score:
                return DecisionOutput(
                    use_cached_action=False,
                    reason="score_below_threshold",
                    diagnostics={"score": candidate.score},
                )

            action = ActionCandidate(
                action_type=candidate.record.action_type,
                arguments=candidate.record.action_args,
                confidence=max(min(candidate.score, 1.0), 0.0),
                source="memory",
            )
            return DecisionOutput(
                use_cached_action=True,
                action=action,
                reason="memory_hit",
                diagnostics={"score": candidate.score},
            )

        # All ranked records were excluded (already replayed this run).
        return DecisionOutput(
            use_cached_action=False,
            reason="all_candidates_already_replayed",
        )
