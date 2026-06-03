from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .models import MemoryRecord


@dataclass
class RankedRecord:
    record: MemoryRecord
    score: float


def score_record(record: MemoryRecord, state_key: str, intent_key: str) -> float:
    # Scoring weights:
    #   intent match (same task instruction hash)        : 0.5
    #   state_key match (intent+pkg both match)          : 0.5
    #       → combined exact match = 1.0
    #       → intent-only match    = 0.5  (below min_score=0.7 → needs history)
    #       → state-only (rare)    = 0.5
    # success_ratio scales the score so untested records don't blindly fire.
    # A record with 1 success and 0 failures = ratio 1.0 (full trust).
    # A record with 1 success and 1 failure  = ratio 0.5 (needs both matches).
    score = 0.0
    intent_match = record.intent_key == intent_key
    state_match = record.state_key == state_key

    if intent_match:
        score += 0.5
    if state_match:
        score += 0.5

    attempts = max(record.success_count + record.fail_count, 1)
    success_ratio = record.success_count / attempts
    score *= success_ratio

    if record.forbidden:
        score -= 1.0

    return score


def top_matches(records: Iterable[MemoryRecord], state_key: str, intent_key: str, limit: int = 5) -> list[RankedRecord]:
    ranked: list[RankedRecord] = []
    for rec in records:
        ranked.append(RankedRecord(record=rec, score=score_record(rec, state_key, intent_key)))
    ranked.sort(key=lambda x: x.score, reverse=True)
    return ranked[:limit]
