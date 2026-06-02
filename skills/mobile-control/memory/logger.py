from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class MemoryEventLogger:
    """Append-only JSONL logger for read-only memory instrumentation."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()
        self.db_path = self.path.with_suffix(".db")
        self._init_db()

    def _init_db(self) -> None:
        con = sqlite3.connect(self.db_path)
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    run_id TEXT,
                    type TEXT,
                    step INTEGER,
                    outcome TEXT,
                    instruction TEXT,
                    intent_signature TEXT,
                    foreground_pkg TEXT,
                    ui_fingerprint TEXT,
                    state_key TEXT,
                    provider_used TEXT,
                    action_type TEXT,
                    action_args_json TEXT,
                    metrics_json TEXT,
                    payload_json TEXT
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_memory_events_ts ON memory_events(ts)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_memory_events_state ON memory_events(state_key)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_memory_events_outcome ON memory_events(outcome)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_memory_events_action ON memory_events(action_type)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_memory_events_intent ON memory_events(intent_signature)")
            con.commit()
        finally:
            con.close()

    def _log_event_sqlite(self, payload: dict[str, Any]) -> None:
        con = sqlite3.connect(self.db_path)
        try:
            con.execute(
                """
                INSERT INTO memory_events (
                    ts, run_id, type, step, outcome, instruction,
                    intent_signature, foreground_pkg, ui_fingerprint,
                    state_key, provider_used, action_type,
                    action_args_json, metrics_json, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    float(payload.get("ts", time.time())),
                    payload.get("run_id"),
                    payload.get("type"),
                    payload.get("step"),
                    payload.get("outcome"),
                    payload.get("instruction"),
                    payload.get("intent_signature"),
                    payload.get("foreground_pkg"),
                    payload.get("ui_fingerprint"),
                    payload.get("state_key"),
                    payload.get("provider_used"),
                    payload.get("action_type"),
                    json.dumps(payload.get("action_args", {}), ensure_ascii=False),
                    json.dumps(payload.get("metrics", {}), ensure_ascii=False),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            con.commit()
        finally:
            con.close()

    def log_event(self, event: dict[str, Any]) -> None:
        payload = dict(event)
        payload.setdefault("ts", time.time())
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        # Dual-write to SQLite for queryable analytics. This telemetry path is
        # best-effort and must never affect task execution.
        try:
            self._log_event_sqlite(payload)
        except Exception:
            pass
