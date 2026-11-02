"""Training-playbook loader and guardrail checker.

The playbook (``memory/training_playbook.json``) holds device-verified recipes
for recurring tasks. Unlike the action-level plan store (``plans.jsonl``, which
only ever contains what a REAL run recorded), these recipes are hand-authored
but every ``guardrails`` entry is enforced by :func:`validate_plan` — so a recipe
that drifts (and a generated plan that copies it) is rejected instead of being
silently executed.

Why guardrails instead of prose: during mute debugging an LLM-written example
("press VOLUME_UP then tap the bell") became policy and poisoned every plan.
Machine-checkable rules are the only way to keep that from happening twice.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    from runner.planner import parse_plan  # noqa: F401  (validates DSL on import)
    _HAVE_PARSER = True
except Exception:  # pragma: no cover - stand-alone use without runner
    _HAVE_PARSER = False

_PLAYBOOK_PATH = Path(__file__).with_name("training_playbook.json")


@dataclass
class TrainingTask:
    id: str
    intent: str
    aliases: list[str] = field(default_factory=list)
    device_bucket: str = "default"
    status: str = "unverified"
    verified_on: str = ""
    goal: str = ""
    acceptance: str = ""
    probes: list[str] = field(default_factory=list)
    plan: str = ""
    pitfalls: list[str] = field(default_factory=list)
    guardrails: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TrainingTask":
        return cls(
            id=d.get("id", ""),
            intent=d.get("intent", ""),
            aliases=list(d.get("aliases") or []),
            device_bucket=d.get("device_bucket", "default"),
            status=d.get("status", "unverified"),
            verified_on=d.get("verified_on", ""),
            goal=d.get("goal", ""),
            acceptance=d.get("acceptance", ""),
            probes=list(d.get("probes") or []),
            plan=d.get("plan", ""),
            pitfalls=list(d.get("pitfalls") or []),
            guardrails=dict(d.get("guardrails") or {}),
        )


def load_playbook(path: Optional[Path] = None) -> list[TrainingTask]:
    """Load the training corpus. Raises on malformed JSON."""
    data = json.loads((path or _PLAYBOOK_PATH).read_text(encoding="utf-8"))
    if data.get("format") != 1:
        raise ValueError("unsupported playbook format")
    return [TrainingTask.from_dict(t) for t in data.get("tasks", [])]


def find_task(tasks: list[TrainingTask], instruction: str) -> Optional[TrainingTask]:
    """The task whose intent/alias appears in the instruction (best effort)."""
    lowered = (instruction or "").strip().lower()
    if not lowered:
        return None
    best: Optional[TrainingTask] = None
    best_len = 0
    for task in tasks:
        for key in [task.intent, *task.aliases]:
            k = (key or "").strip().lower()
            if k and k in lowered and len(k) > best_len:
                best, best_len = task, len(k)
    return best


def _plan_lines(plan_text: str) -> list[str]:
    return [ln.strip() for ln in (plan_text or "").splitlines() if ln.strip()]


def _path_lines(lines: list[str]) -> list[str]:
    return [ln for ln in lines if ln.startswith("- path")]


def _verify_lines(lines: list[str]) -> list[str]:
    return [ln for ln in lines if ln.startswith("- verify:")]


def validate_plan(task: TrainingTask, plan_text: str) -> list[str]:
    """Return a list of guardrail violations for ``plan_text`` ([] == compliant).

    Enforced, not advisory: a non-empty result means the plan must be rejected.
    """
    violations: list[str] = []
    gr = task.guardrails or {}
    lines = _plan_lines(plan_text)
    paths = _path_lines(lines)

    if gr.get("min_paths"):
        if len(paths) < int(gr["min_paths"]):
            violations.append(
                f"plan needs >= {gr['min_paths']} `- path` lines, found {len(paths)}"
            )

    banned = [str(s).lower() for s in gr.get("no_path_first_action_matching", [])]
    for ln in paths:
        for b in banned:
            if b in ln.lower():
                violations.append(
                    f"path opens toward '{b}' (banned first action): {ln}"
                )

    if gr.get("must_contain_probe"):
        if not _verify_lines(lines):
            violations.append("plan has no `- verify:` line with a probe tag")
        elif not any("[probe:" in v for v in _verify_lines(lines)):
            violations.append("`- verify:` line is missing a `[probe: ...]` tag")

    if _HAVE_PARSER:
        try:
            snapshot = parse_plan(plan_text)
            if snapshot.total_count == 0:
                violations.append("plan does not parse to any milestone")
        except Exception as e:  # pragma: no cover
            violations.append(f"plan fails to parse: {e}")

    return violations


def is_trusted_for_replay(task: TrainingTask) -> bool:
    """Only recipes confirmed on a device are safe to hand to the planner as
    a *chosen* strategy (as opposed to a suggestion)."""
    return task.status == "verified" and bool(task.verified_on)