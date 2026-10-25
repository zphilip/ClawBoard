"""Deterministic post-run report for artemis-control (Phase 2) — no LLM.

Replaces upstream's three post-mortem LLM agents (``outputter``,
``history_analyzer``, ``log_analyzer``) with a single deterministic Markdown
render built from the run's outcome + memory/checker state.  Written under
``memory/reports/``.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path


def build_post_run_report(
    *,
    profile: str,
    instruction: str,
    status: str,
    end_reason: str,
    steps: int,
    actions: list[str],
    plan_text: str = "",
    validator_stats: dict | None = None,
    incident_count: int = 0,
    checker: dict | None = None,
    memory_replay: bool = False,
    elapsed_s: float = 0.0,
) -> str:
    """Render the run's post-mortem as Markdown."""
    lines: list[str] = []
    lines.append(f"# Post-run report — {profile}")
    lines.append("")
    lines.append(f"- **Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- **Status**: `{status}` (reason: `{end_reason}`)")
    lines.append(f"- **Steps taken**: {steps}")
    lines.append(f"- **Elapsed**: {elapsed_s:.1f}s")
    lines.append(f"- **Memory replay**: {'yes (0 VLM)' if memory_replay else 'no'}")
    lines.append("")
    lines.append(f"## Goal\n{instruction}")
    lines.append("")

    # Plan progress
    if plan_text:
        lines.append("## Final plan state")
        lines.append("```")
        lines.append(plan_text.rstrip())
        lines.append("```")
        lines.append("")

    # Actions
    if actions:
        lines.append(f"## Actions ({len(actions)})")
        for i, a in enumerate(actions[-40:]):
            lines.append(f"{i+1}. {a}")
        if len(actions) > 40:
            lines.append(f"... ({len(actions) - 40} earlier steps omitted)")
        lines.append("")

    # Pro-only sections
    if validator_stats:
        lines.append("## Validator stats")
        lines.append(
            f"- approved: {validator_stats.get('approved', 0)}, "
            f"warned: {validator_stats.get('warned', 0)}, "
            f"rejected: {validator_stats.get('rejected', 0)}"
        )
        lines.append(f"- safety incidents: {incident_count}")
        lines.append("")

    if checker:
        lines.append("## Checker verdict")
        lines.append(f"- goal_achieved: `{checker.get('goal_achieved', True)}`")
        lines.append(f"- confidence: {checker.get('confidence', 0):.0%}")
        if checker.get("summary"):
            lines.append(f"- summary: {checker['summary']}")
        if checker.get("recommendation"):
            lines.append(f"- recommendation: {checker['recommendation']}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_report(skill_dir, profile: str, report_md: str) -> Path:
    """Write the report under ``<skill>/memory/reports/`` and return its path."""
    reports_dir = Path(skill_dir) / "memory" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = reports_dir / f"report_{profile}_{ts}.md"
    path.write_text(report_md, encoding="utf-8")
    return path