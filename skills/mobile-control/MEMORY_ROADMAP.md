# Mobile Control Memory Roadmap (No-Behavior-Change Phase)

This document defines a maintainable path to add memory/caching to mobile-control without changing current runtime behavior.

## Goals

- Reuse proven action paths for repeated tasks.
- Block known bad decisions in similar UI states.
- Reduce LLM/supervisor calls when confidence is high.
- Preserve safety by verifying state transitions after cached actions.

## Current Constraint

Current runtime logic is concentrated in two files. This roadmap introduces module boundaries first, then gradual wiring.

## Proposed Module Layout

- memory/models.py: typed records and signatures.
- memory/signature.py: state/intent fingerprint functions.
- memory/retriever.py: matching and ranking logic.
- memory/policy.py: decision policy (reuse, blocklist, fallback).
- memory/store.py: JSONL-backed persistence and simple stats.

Future non-memory split (later):

- runner/loop.py: step loop orchestration.
- runner/actions.py: action execution and post-check.
- runner/providers.py: VLM/supervisor call wrappers.
- runner/guards.py: rule-based overrides.

## Memory Data Model

1. Success memory
- key: (intent_signature, state_signature)
- value: action + expected transition + success/failure counters

2. Negative memory
- key: (intent_signature, state_signature)
- value: forbidden actions + reason + counters

3. Recovery memory
- key: (failure_pattern_signature)
- value: recovery action + confidence

## Decision Policy (planned)

1. Build signatures from current step.
2. Retrieve top candidates from success memory.
3. If confidence >= threshold and action not forbidden, execute cached action.
4. Verify transition:
- success -> reinforce memory
- fail -> mark negative and fallback to LLM
5. If no high-confidence cached action, use existing LLM path unchanged.

## Rollout Plan

Phase A (this phase)
- Add memory module scaffolding only (unused).

Phase B
- Read-only logging of signatures/outcomes, no decisions.

Optional post-run convenience
- Run `memory/post_run_report.py` after a task to print the latest summary from `memory_data/`.
- This remains offline and does not affect agent execution.

Phase C
- Enable cache for a narrow task subset with strict verification.

Phase D
- Enable negative-memory blocking for known bad actions.

## Metrics

- Cache hit rate
- Cache hit correctness
- Wrong-app incident rate
- LLM calls per task
- Median task completion time
- Recovery success rate
