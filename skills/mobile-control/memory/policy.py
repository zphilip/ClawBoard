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
        current_run_id: str = "",
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

        # Enhanced replay logic: 
        # 1. If we have high-confidence matches (>0.9), be more aggressive about reuse
        # 2. Only skip replayed actions when we have multiple good alternatives
        # 3. Allow single high-confidence actions to be replayed if no alternatives exist
        
        high_confidence_threshold = 0.9
        high_confidence_candidates = [r for r in ranked if r.score >= high_confidence_threshold]
        
        # Check if we have multiple candidates above threshold
        valid_candidates = [r for r in ranked if r.score >= self.min_score]
        has_multiple_options = len(valid_candidates) > 1

        for candidate in ranked:
            sig = record_replay_sig(
                state_key, candidate.record.action_type, candidate.record.action_args,
            )
            
            # Skip already replayed actions only if we have multiple good options
            should_skip_replayed = (
                exclude_sigs and sig in exclude_sigs and has_multiple_options
            )
            
            if should_skip_replayed:
                continue

            if candidate.record.forbidden:
                return DecisionOutput(
                    use_cached_action=False,
                    blocked=True,
                    reason="action_blocked_by_negative_memory",
                    diagnostics={"score": candidate.score},
                )

            if candidate.score < self.min_score:
                # If this is the only candidate and it's been replayed, still consider it
                # if we have no other options and it's reasonably confident
                if (not has_multiple_options and 
                    exclude_sigs and sig in exclude_sigs and 
                    candidate.score >= 0.6):
                    # Allow lower confidence replay when no alternatives exist
                    pass
                else:
                    continue

            action = ActionCandidate(
                action_type=candidate.record.action_type,
                arguments=candidate.record.action_args,
                confidence=max(min(candidate.score, 1.0), 0.0),
                source="memory",
                action_description=getattr(candidate.record, 'action_description', ''),
            )
            return DecisionOutput(
                use_cached_action=True,
                action=action,
                reason="memory_hit",
                diagnostics={"score": candidate.score},
            )

        # If we get here, no suitable candidate was found
        if exclude_sigs and any(record_replay_sig(state_key, r.record.action_type, r.record.action_args) in exclude_sigs for r in ranked):
            return DecisionOutput(
                use_cached_action=False,
                reason="all_candidates_excluded_or_below_threshold",
            )
        return DecisionOutput(
            use_cached_action=False,
            reason="all_candidates_below_threshold",
        )
