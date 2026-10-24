"""Cross-run memory for artemis-control (Phase 0 port).

Ports the tested action-level memory from ``mobile-control`` plus an
artemis-native task-plan store, so repeated daily tasks can skip the VLM.

Layers:
  - ``signature``  — intent / UI-fingerprint / fuzzy component builder
  - ``models``     — StateSignature, MemoryRecord, Decision{Input,Output}
  - ``store``      — JsonlMemoryStore (action-level records)
  - ``retriever``  — score_record / top_matches (exact + fuzzy)
  - ``policy``     — MemoryPolicy decision (min_score gate, negative memory)
  - ``plan``       — ArtemisPlanStore + plan_is_healthy (task-level replay)
"""
from .signature import (
    UiComponents,
    build_canonical_intent_key,
    build_intent_signature,
    build_state_key,
    build_ui_components,
    build_ui_fingerprint,
    ui_similarity,
)
from .models import (
    ActionCandidate,
    DecisionInput,
    DecisionOutput,
    MemoryRecord,
    StateSignature,
)
from .store import JsonlMemoryStore
from .retriever import score_record, top_matches
from .policy import MemoryPolicy, NON_CACHEABLE_ACTIONS
from .plan import (
    ArtemisPlan,
    ArtemisPlanStep,
    ArtemisPlanStore,
    plan_is_healthy,
)

__all__ = [
    "UiComponents",
    "build_canonical_intent_key",
    "build_intent_signature",
    "build_state_key",
    "build_ui_components",
    "build_ui_fingerprint",
    "ui_similarity",
    "ActionCandidate",
    "DecisionInput",
    "DecisionOutput",
    "MemoryRecord",
    "StateSignature",
    "JsonlMemoryStore",
    "score_record",
    "top_matches",
    "MemoryPolicy",
    "NON_CACHEABLE_ACTIONS",
    "ArtemisPlan",
    "ArtemisPlanStep",
    "ArtemisPlanStore",
    "plan_is_healthy",
]