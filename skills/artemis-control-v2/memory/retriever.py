from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .models import MemoryRecord
from .signature import UiComponents, ui_similarity


@dataclass
class RankedRecord:
    record: MemoryRecord
    score: float


def score_record(
    record: MemoryRecord,
    state_key: str,
    intent_key: str,
    current_ui: UiComponents | None = None,
) -> float:
    # Scoring weights:
    #   intent match (same task instruction hash)        : 0.5
    #   state match (exact, or fuzzy component overlap)  : 0.5
    #       → combined exact match = 1.0
    #       → intent-only match    = 0.5  (below min_score=0.7 → needs history)
    # success_ratio scales the score so untested records don't blindly fire.
    # A record with 1 success and 0 failures = ratio 1.0 (full trust).
    # A record with 1 success and 1 failure  = ratio 0.5 (needs both matches).
    #
    # Fuzzy fallback: when the exact state_key missed, compare the stored UI
    # components (foreground pkg + resource-ids + labels) against the current
    # screen.  This keeps repeated tasks hittable when a small ad / feed /
    # badge change altered the exact hash.
    intent_match = record.intent_key == intent_key
    state_match = record.state_key == state_key

    state_component = 0.0
    if state_match:
        state_component = 0.5
    elif (
        intent_match
        and current_ui is not None
        and record.resource_ids is not None
        and record.texts is not None
    ):
        stored = UiComponents(
            foreground_pkg=record.foreground_pkg or "",
            resource_ids=frozenset(record.resource_ids),
            labels=frozenset(record.texts),
        )
        state_component = 0.5 * ui_similarity(stored, current_ui)

    score = (0.5 if intent_match else 0.0) + state_component
    attempts = max(record.success_count + record.fail_count, 1)
    success_ratio = record.success_count / attempts
    score *= success_ratio

    if record.forbidden:
        score -= 1.0

    return score


def top_matches(
    records: Iterable[MemoryRecord],
    state_key: str,
    intent_key: str,
    limit: int = 5,
    current_ui: UiComponents | None = None,
) -> list[RankedRecord]:
    ranked: list[RankedRecord] = []
    for rec in records:
        ranked.append(
            RankedRecord(
                record=rec,
                score=score_record(rec, state_key, intent_key, current_ui),
            )
        )
    ranked.sort(key=lambda x: x.score, reverse=True)
    return ranked[:limit]
