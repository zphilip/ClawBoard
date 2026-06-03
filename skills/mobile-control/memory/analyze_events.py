#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from analyze_events_jsonl import summarize as summarize_jsonl, _load_jsonl
from analyze_events_sqlite import summarize as summarize_sqlite, _load_rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze mobile-control memory events")
    parser.add_argument(
        "--input",
        type=str,
        default="memory_data/events.db",
        help="Path to SQLite DB or JSONL events file",
    )
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--min-success", type=int, default=3)
    parser.add_argument("--min-success-rate", type=float, default=0.80)
    args = parser.parse_args()

    path = Path(args.input)
    if path.suffix.lower() == ".db" and path.exists():
        rows = _load_rows(path)
        print(summarize_sqlite(rows, args.top, args.min_success, args.min_success_rate))
    else:
        rows = _load_jsonl(path)
        print(summarize_jsonl(rows, args.top, args.min_success, args.min_success_rate))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
