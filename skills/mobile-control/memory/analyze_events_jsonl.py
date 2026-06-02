#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


BAD_OUTCOMES = {
    "parse_failed",
    "screenshot_failed",
    "open_not_found",
    "wait_recovery_home",
    "wrong_screen_home_recovery",
}

GOOD_OUTCOMES = {
    "completed",
    "answer_confirmed_complete",
    "terminate_action",
}


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") == "step_outcome":
            rows.append(obj)
    return rows


def _state_label(row: dict[str, Any]) -> str:
    state = row.get("state_key") or "<missing_state_key>"
    fg = row.get("foreground_pkg") or ""
    return f"{state} | fg={fg}"


def _action_key(row: dict[str, Any]) -> tuple[str, str]:
    action_type = str(row.get("action_type") or "")
    try:
        action_args = json.dumps(row.get("action_args") or {}, ensure_ascii=False, sort_keys=True)
    except Exception:
        action_args = "{}"
    return action_type, action_args


def summarize(rows: list[dict[str, Any]], top_n: int, min_success: int, min_success_rate: float) -> str:
    out: list[str] = []
    out.append(f"Total step_outcome events: {len(rows)}")

    # 1) Top repeated states
    state_counter = Counter(_state_label(r) for r in rows)
    out.append("\nTop repeated states:")
    if state_counter:
        for state, count in state_counter.most_common(top_n):
            out.append(f"- {count:4d} | {state}")
    else:
        out.append("- (none)")

    # 2) Top bad actions
    bad_counter = Counter()
    for r in rows:
        outcome = str(r.get("outcome") or "")
        if outcome in BAD_OUTCOMES:
            action_type, action_args = _action_key(r)
            label = f"outcome={outcome} | action={action_type} | args={action_args[:120]}"
            bad_counter[label] += 1

    out.append("\nTop bad actions:")
    if bad_counter:
        for label, count in bad_counter.most_common(top_n):
            out.append(f"- {count:4d} | {label}")
    else:
        out.append("- (none)")

    # 3) Candidate cacheable actions (state + action with strong success profile)
    bucket = defaultdict(lambda: {"good": 0, "bad": 0, "total": 0})
    row_example: dict[tuple[str, str, str], dict[str, Any]] = {}

    for r in rows:
        state = str(r.get("state_key") or "")
        action_type, action_args = _action_key(r)
        if not state or not action_type:
            continue
        key = (state, action_type, action_args)
        row_example[key] = r
        outcome = str(r.get("outcome") or "")
        bucket[key]["total"] += 1
        if outcome in GOOD_OUTCOMES:
            bucket[key]["good"] += 1
        if outcome in BAD_OUTCOMES:
            bucket[key]["bad"] += 1

    candidates: list[tuple[float, int, tuple[str, str, str], dict[str, int]]] = []
    for key, stats in bucket.items():
        good = stats["good"]
        total = stats["total"]
        if total <= 0:
            continue
        rate = good / total
        if good >= min_success and rate >= min_success_rate:
            candidates.append((rate, good, key, stats))

    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)

    out.append("\nCandidate cacheable actions:")
    if candidates:
        for rate, good, key, stats in candidates[:top_n]:
            state, action_type, action_args = key
            fg = row_example[key].get("foreground_pkg") or ""
            out.append(
                f"- good={good}, total={stats['total']}, success_rate={rate:.2f} "
                f"| action={action_type} | fg={fg} | state={state} | args={action_args[:120]}"
            )
    else:
        out.append("- (none passing thresholds)")

    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze mobile-control JSONL memory events")
    parser.add_argument(
        "--input",
        type=str,
        default="memory_data/events.jsonl",
        help="Path to JSONL events file (default: memory_data/events.jsonl)",
    )
    parser.add_argument("--top", type=int, default=10, help="Top N rows per section")
    parser.add_argument("--min-success", type=int, default=3, help="Minimum successful count for cache candidates")
    parser.add_argument(
        "--min-success-rate",
        type=float,
        default=0.80,
        help="Minimum success rate for cache candidates",
    )
    args = parser.parse_args()

    path = Path(args.input)
    rows = _load_jsonl(path)
    report = summarize(rows, top_n=args.top, min_success=args.min_success, min_success_rate=args.min_success_rate)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
