"""Artemis-control runner package.

Components ported from Google Research's Artemis core:
  - safety_net    — Pre-execution XML + pixel precondition validation
  - incidents     — Structured execution failure tracking with self-healing
  - explorer      — UI element indexer (accessibility tree + OCR fusion)
  - context       — History store with keyword search and budget tracking
"""

from runner.safety_net import SafetyNet, SafetyNetConfig, ValidationErrorCategory
from runner.incidents import (
    ExecutionIncident,
    IncidentTracker,
    KIND_EXEC_ERROR,
    KIND_SAFETY_NET,
)
from runner.explorer import ElementIndexer, ScreenElement
from runner.context import BudgetWarning, ContextBudget, HistoryStore, StepRecord

__all__ = [
    "BudgetWarning",
    "ContextBudget",
    "ElementIndexer",
    "ExecutionIncident",
    "HistoryStore",
    "IncidentTracker",
    "KIND_EXEC_ERROR",
    "KIND_SAFETY_NET",
    "SafetyNet",
    "SafetyNetConfig",
    "ScreenElement",
    "StepRecord",
    "ValidationErrorCategory",
]