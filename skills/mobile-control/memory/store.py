from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .models import MemoryRecord


class JsonlMemoryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    def load(self) -> list[MemoryRecord]:
        records: list[MemoryRecord] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                records.append(MemoryRecord.from_dict(obj))
            except Exception:
                continue
        return records

    def append(self, record: MemoryRecord) -> None:
        record.updated_at = time.time()
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")

    def purge_actions(self, action_types: frozenset[str] | set[str]) -> int:
        """Remove all records whose action_type is in *action_types*.

        Returns the number of records removed.  Uses an atomic
        write-to-temp-then-rename strategy so a crash mid-write never
        corrupts the store.
        """
        records = self.load()
        kept = [r for r in records if r.action_type not in action_types]
        removed = len(records) - len(kept)
        if removed > 0:
            tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            try:
                with tmp_path.open("w", encoding="utf-8") as f:
                    for record in kept:
                        f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
                os.replace(tmp_path, self.path)   # atomic on POSIX
            except Exception:
                # Don't leave a partial temp file behind.
                tmp_path.unlink(missing_ok=True)
                raise
        return removed
