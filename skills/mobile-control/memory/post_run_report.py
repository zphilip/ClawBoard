#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from analyze_events import main as analyze_main


def _find_latest_source(base_dir: Path) -> Path | None:
    db = base_dir / "events.db"
    jsonl = base_dir / "events.jsonl"
    if db.exists():
        return db
    if jsonl.exists():
        return jsonl
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Print a post-run mobile-control memory summary")
    parser.add_argument(
        "--input",
        type=str,
        default="",
        help="Optional explicit path to events.db or events.jsonl",
    )
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--min-success", type=int, default=3)
    parser.add_argument("--min-success-rate", type=float, default=0.80)
    args = parser.parse_args()

    if args.input:
        source = Path(args.input)
    else:
        source = _find_latest_source(Path(__file__).resolve().parent / ".." / "memory_data")
        if source is None:
            print("No telemetry source found under memory_data/")
            return 1

    # Reuse the unified analyzer CLI by re-execing its argument contract.
    import sys
    old_argv = sys.argv[:]
    try:
        sys.argv = [
            "analyze_events.py",
            "--input",
            str(source),
            "--top",
            str(args.top),
            "--min-success",
            str(args.min_success),
            "--min-success-rate",
            str(args.min_success_rate),
        ]
        return analyze_main()
    finally:
        sys.argv = old_argv


if __name__ == "__main__":
    raise SystemExit(main())
