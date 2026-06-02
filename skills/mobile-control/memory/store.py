from __future__ import annotations

import json
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
                records.append(MemoryRecord(**obj))
            except Exception:
                continue
        return records

    def append(self, record: MemoryRecord) -> None:
        record.updated_at = time.time()
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record.__dict__, ensure_ascii=False) + "\n")
