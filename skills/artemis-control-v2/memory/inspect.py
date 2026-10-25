"""Inspect the cross-run memory stores for artemis-control.

Usage (from the skill dir, or pass paths):
    python3 memory/inspect.py
    python3 memory/inspect.py --dir /path/to/memory/data
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running directly as a script (sys.path[0] is memory/).
SKILL_DIR = Path(__file__).resolve().parent.parent
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

from memory import ArtemisPlanStore, JsonlMemoryStore, plan_is_healthy  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", default="", help="memory/data directory (default: <skill>/memory/data)")
    args = ap.parse_args()

    data_dir = Path(args.dir) if args.dir else SKILL_DIR / "memory" / "data"

    # ── Action-level records ──────────────────────────────────────────
    mem_path = data_dir / "memory.jsonl"
    print(f"=== action store: {mem_path} ===")
    if not mem_path.exists():
        print("  (missing — no action memory written yet)")
    else:
        store = JsonlMemoryStore(mem_path)
        records = store.load()
        print(f"  records={len(records)}")
        by_intent: dict[str, int] = {}
        for r in records:
            by_intent[r.intent_key] = by_intent.get(r.intent_key, 0) + 1
        for ik, n in sorted(by_intent.items(), key=lambda kv: -kv[1]):
            print(f"  intent={ik[:24]:24} actions={n}")
        for r in records[-5:]:
            print(f"    - {r.action_type:14} args={r.action_args} ok={r.success_count} bad={r.fail_count} fg={r.foreground_pkg}")

    # ── Task plans ────────────────────────────────────────────────────
    plan_path = data_dir / "plans.jsonl"
    print(f"\n=== plan store: {plan_path} ===")
    if not plan_path.exists():
        print("  (missing — no task plan written yet)")
    else:
        ps = ArtemisPlanStore(plan_path)
        plans = ps.load()
        print(f"  plans={len(plans)}")
        for p in plans:
            healthy = plan_is_healthy(p)
            print(
                f"  intent={p.intent_key[:24]:24} success={p.success_count} fail={p.fail_count} "
                f"healthy={healthy} steps={len(p.steps)} replayable={p.success_count >= 1 and healthy}"
            )
            print(f"    instruction: {p.instruction_sample[:80]}")
            for i, s in enumerate(p.steps):
                print(
                    f"    [{i}] {s.action_type:14} rid={s.target_rid or '-':24} "
                    f"post={s.post_pkg or '-'} ok={s.success_count} bad={s.fail_count}"
                )


if __name__ == "__main__":
    main()