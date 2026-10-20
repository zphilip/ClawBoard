"""Context management: history compression, search, and visual summaries.

Ported from Artemis core:
  - artemis/memory/transcript.py        — session message ledger
  - artemis/memory/context_policy.py    — per-agent rendering policies
  - artemis/agents/flash/summarizer.py  — async visual step summarization
  - artemis/tools/history/search.py     — keyword/step-range history lookup
  - artemis/memory/step_memory.py       — background summary service

Provides:
  1. History lookup (search_history) — no vector DB, deterministic keyword search
  2. Visual step summaries — replace old screenshots with text (async, background)
  3. Context budget tracking — warn when approaching model context limits
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Step record (what we store per turn)
# ---------------------------------------------------------------------------

@dataclass
class StepRecord:
    """One recorded step in the execution history."""
    step: int
    timestamp: float = field(default_factory=time.time)
    action: str = ""
    action_type: str = ""
    action_args: dict[str, Any] = field(default_factory=dict)
    screenshot_path: str = ""
    ui_elements_count: int = 0
    foreground_app: str = ""
    safety_check: str = ""         # xml_passed | pixel_passed | skipped | failed
    summary: str = ""              # async-generated visual summary
    error: str = ""                # execution error, if any
    thinking: str = ""             # VLM reasoning, if captured

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp

    def key_text(self) -> str:
        """Text used for search indexing."""
        parts = [self.action, self.foreground_app, self.safety_check]
        if self.error:
            parts.append(self.error)
        return " ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# History store
# ---------------------------------------------------------------------------

class HistoryStore:
    """In-memory store of step records with keyword search.

    For long runs, old screenshots are replaced with async-generated visual
    summaries (text only) to stay within context limits. No vector database:
    search is deterministic keyword matching against step records.
    """

    MAX_RESULTS = 5
    EXCERPT_CHARS = 240

    def __init__(self, max_stored_screenshots: int = 6):
        self._steps: list[StepRecord] = []
        self._max_screenshots = max_stored_screenshots
        self._total_tokens_estimate: int = 0

    def add(self, record: StepRecord) -> int:
        """Add a step record. Returns the step index."""
        self._steps.append(record)
        # Estimate token usage (rough: 1 token ≈ 4 chars)
        self._total_tokens_estimate += len(record.key_text()) // 4
        # Prune old screenshots if over budget
        self._prune_screenshots()
        return len(self._steps) - 1

    def get(self, step: int) -> Optional[StepRecord]:
        """Get a step record by index."""
        if 0 <= step < len(self._steps):
            return self._steps[step]
        return None

    def get_recent(self, n: int = 5) -> list[StepRecord]:
        """Get the N most recent steps."""
        return self._steps[-n:]

    def search(
        self,
        query: str,
        step_range: Optional[str] = None,
        top_k: int = 5,
    ) -> list[StepRecord]:
        """Search history by keyword and optional step range.

        Args:
            query: Space-separated keywords (all must match)
            step_range: Optional "N-M" or "N-" or "-M" step range filter
            top_k: Maximum results to return

        Returns:
            Matching step records, most recent first
        """
        keywords = [k.lower() for k in query.split() if k]
        if not keywords:
            return self.get_recent(top_k)

        # Parse step range
        min_step, max_step = 0, len(self._steps) - 1
        if step_range:
            m = re.match(r'^(\d*)-(\d*)$', step_range)
            if m:
                if m.group(1):
                    min_step = int(m.group(1))
                if m.group(2):
                    max_step = int(m.group(2))

        matches = []
        for i in range(max_step, min_step - 1, -1):
            if i >= len(self._steps):
                continue
            record = self._steps[i]
            haystack = record.key_text().lower()
            # All keywords must match somewhere in the record
            if all(kw in haystack for kw in keywords):
                matches.append(record)
            if len(matches) >= top_k:
                break

        return matches

    def search_excerpt(self, record: StepRecord, query: str) -> str:
        """Create a search result excerpt around the matching keywords."""
        haystack = record.key_text()
        keywords = [k for k in query.lower().split() if k]
        if not keywords:
            return haystack[:self.EXCERPT_CHARS]

        # Find the earliest keyword position
        pos = len(haystack)
        for kw in keywords:
            idx = haystack.lower().find(kw)
            if idx >= 0 and idx < pos:
                pos = idx

        start = max(0, pos - 40)
        end = min(len(haystack), start + self.EXCERPT_CHARS)
        excerpt = haystack[start:end]
        if start > 0:
            excerpt = "…" + excerpt
        if end < len(haystack):
            excerpt = excerpt + "…"
        return excerpt

    @property
    def step_count(self) -> int:
        return len(self._steps)

    @property
    def estimated_tokens(self) -> int:
        return self._total_tokens_estimate

    def _prune_screenshots(self) -> None:
        """Drop screenshot paths from old records, keeping only recent ones."""
        prune_before = len(self._steps) - self._max_screenshots
        if prune_before <= 0:
            return
        for i in range(prune_before):
            if self._steps[i].screenshot_path:
                self._steps[i].screenshot_path = "[pruned]"

    def render_for_prompt(
        self,
        last_n_detailed: int = 3,
        max_older_summaries: int = 5,
    ) -> str:
        """Render history as a prompt block.

        Recent steps are shown in detail; older steps are aggregated.
        """
        if not self._steps:
            return ""

        lines = ["--- Task History ---"]

        n = len(self._steps)
        detail_start = max(0, n - last_n_detailed)

        # Older steps: aggregated summary
        if detail_start > 0:
            older = self._steps[:detail_start]
            shown = older[-max_older_summaries:]
            if len(older) > max_older_summaries:
                lines.append(
                    f"Steps 1–{detail_start - max_older_summaries}: "
                    f"{len(older) - max_older_summaries} steps (use search_history for details)"
                )
            for rec in shown:
                summary = rec.summary or rec.action
                lines.append(f"  Step {rec.step + 1}: {summary}")

        # Recent steps: full detail
        for rec in self._steps[detail_start:]:
            lines.append(
                f"  Step {rec.step + 1}: {rec.action}"
                + (f" [{rec.foreground_app}]" if rec.foreground_app else "")
                + (f" (safety: {rec.safety_check})" if rec.safety_check else "")
                + (f" ERROR: {rec.error}" if rec.error else "")
            )

        return "\n".join(lines)

    def reset(self) -> None:
        """Clear all history."""
        self._steps.clear()
        self._total_tokens_estimate = 0


# ---------------------------------------------------------------------------
# Context budget tracker
# ---------------------------------------------------------------------------

@dataclass
class BudgetWarning:
    """Warning when approaching context limits."""
    used_tokens: int
    limit_tokens: int
    usage_pct: float
    message: str


class ContextBudget:
    """Tracks estimated token usage and warns before context overflow."""

    WARN_AT_PCT = 0.75
    CRITICAL_AT_PCT = 0.90

    def __init__(self, limit_tokens: int = 32768):
        self._limit = limit_tokens

    def check(self, history: HistoryStore, extra_tokens: int = 0) -> Optional[BudgetWarning]:
        """Check if we're approaching the context limit."""
        used = history.estimated_tokens + extra_tokens
        pct = used / self._limit if self._limit > 0 else 0.0

        if pct >= self.CRITICAL_AT_PCT:
            return BudgetWarning(
                used_tokens=used,
                limit_tokens=self._limit,
                usage_pct=pct,
                message=(
                    f"Context budget critical: {pct:.0%} used ({used}/{self._limit} tokens). "
                    "Consider compressing older history or reducing screenshot count."
                ),
            )
        elif pct >= self.WARN_AT_PCT:
            return BudgetWarning(
                used_tokens=used,
                limit_tokens=self._limit,
                usage_pct=pct,
                message=(
                    f"Context budget warning: {pct:.0%} used ({used}/{self._limit} tokens)."
                ),
            )
        return None