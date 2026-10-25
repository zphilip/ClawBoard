"""Artemis-control runner package.

Components ported from Google Research's Artemis core:
  - safety_net    — Pre-execution XML + pixel precondition validation
  - incidents     — Structured execution failure tracking with self-healing
  - explorer      — UI element indexer (accessibility tree + OCR fusion)
  - context       — History store with keyword search and budget tracking
  - planner       — Task planner: milestone grammar, plan generation, plan-guided exec
  - validator     — Action validator: independent VLM judges Operator's proposed action
  - checker       — Task checker: final audit of goal achievement
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
from runner.planner import (
    CONTINUOUS_LOOP_TAG,
    LOOP_TAG,
    PlanItem,
    PlanSnapshot,
    apply_plan_updates,
    build_operator_plan_system_prompt,
    build_planner_system_prompt,
    generate_plan,
    is_task_complete,
    parse_plan,
    render_plan_grammar_spec,
    STATUS_CHARS,
    STATUS_DONE,
    validate_plan_format,
)
from runner.validator import call_validator, build_validator_system_prompt
from runner.checker import call_checker, build_checker_system_prompt
from runner.notes import (
    NoteStore,
    append_note_content,
    get_note_file_path,
    get_notes_dir,
    list_notes_info,
    list_notes_keys,
    read_note_content,
    save_note_content,
    update_note_content,
)
from runner.diagnoser import (
    build_diagnoser_system_prompt,
    build_diagnoser_user_prompt,
    call_diagnoser,
    deterministic_diagnosis,
)
from runner.post_run_report import build_post_run_report, write_report

__all__ = [
    "BudgetWarning",
    "CONTINUOUS_LOOP_TAG",
    "ContextBudget",
    "ElementIndexer",
    "ExecutionIncident",
    "HistoryStore",
    "IncidentTracker",
    "KIND_EXEC_ERROR",
    "KIND_SAFETY_NET",
    "LOOP_TAG",
    "PlanItem",
    "PlanSnapshot",
    "SafetyNet",
    "SafetyNetConfig",
    "ScreenElement",
    "STATUS_CHARS",
    "STATUS_DONE",
    "StepRecord",
    "ValidationErrorCategory",
    "apply_plan_updates",
    "build_checker_system_prompt",
    "build_operator_plan_system_prompt",
    "build_planner_system_prompt",
    "build_validator_system_prompt",
    "call_checker",
    "call_validator",
    "generate_plan",
    "is_task_complete",
    "parse_plan",
    "render_plan_grammar_spec",
    "validate_plan_format",
]