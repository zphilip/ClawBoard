from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .models import MemoryRecord


@dataclass
class RankedRecord:
    record: MemoryRecord
    score: float


def score_record(record: MemoryRecord, state_key: str, intent_key: str) -> float:
    score = 0.0
    if record.intent_key == intent_key:
        score += 0.6
    if record.state_key == state_key:
        score += 0.4

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
