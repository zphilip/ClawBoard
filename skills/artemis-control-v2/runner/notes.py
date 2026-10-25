"""Persistent notes store for artemis-control (Phase 1).

A stdlib-only port of upstream ``artemis/utils/notes.py`` changed in exactly
one way: notes live under ``memory/notes/`` inside the skill dir instead of a
session-scoped data-engine directory, so they survive across runs.

Five operations mirror the upstream note tools:

- ``save``    — overwrite a note (create the file if missing)
- ``read``    — full read, or a 1-indexed inclusive line range
                (``start_line`` / ``end_line``)
- ``append``  — append content (newline-safe)
- ``update``  — replace an exact target substring once
- ``list``    — note keys with their line counts

The special key ``task_plan`` maps to ``task_plan.md``; any trailing ``.md`` on
a key is stripped to avoid double extensions.
"""
from __future__ import annotations

import os
from pathlib import Path


def get_notes_dir(base_dir) -> Path:
    """Absolute path to the notes directory under *base_dir*.

    *base_dir* is the memory root (e.g. ``<skill>/memory``) so the notes live
    at ``<skill>/memory/notes/``.
    """
    return Path(base_dir) / "notes"


def get_note_file_path(base_dir, key: str) -> Path:
    """Path of a note file, mapping ``task_plan`` → ``task_plan.md`` and
    stripping any trailing ``.md`` from *key*."""
    notes_dir = get_notes_dir(base_dir)
    clean_key = key[:-3] if key.lower().endswith(".md") else key
    if clean_key == "task_plan":
        return notes_dir / "task_plan.md"
    return notes_dir / f"{clean_key}.md"


def list_notes_keys(base_dir) -> list[str]:
    """All note keys currently stored (file stems of ``*.md``)."""
    notes_dir = get_notes_dir(base_dir)
    if not notes_dir.exists():
        return []
    return [f.stem for f in notes_dir.glob("*.md")]


def list_notes_info(base_dir) -> dict[str, int]:
    """{note_key: line_count} for every stored note."""
    notes_dir = get_notes_dir(base_dir)
    if not notes_dir.exists():
        return {}
    info = {}
    for f in notes_dir.glob("*.md"):
        try:
            info[f.stem] = len(f.read_text(encoding="utf-8").splitlines())
        except Exception:
            info[f.stem] = -1
    return info


def read_note_content(
    base_dir,
    key: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """Read a note, optionally restricted to a 1-indexed inclusive line range."""
    file_path = get_note_file_path(base_dir, key)
    if not file_path.exists():
        raise FileNotFoundError(f"Note '{key}' not found.")
    content = file_path.read_text(encoding="utf-8")

    if start_line is not None or end_line is not None:
        lines = content.splitlines()
        start = (start_line - 1) if start_line else 0
        end = end_line if end_line else len(lines)
        return "\n".join(lines[start:end])

    return content


def save_note_content(base_dir, key: str, content: str) -> None:
    """Create or overwrite a note."""
    notes_dir = get_notes_dir(base_dir)
    notes_dir.mkdir(parents=True, exist_ok=True)
    get_note_file_path(base_dir, key).write_text(content, encoding="utf-8")


def append_note_content(base_dir, key: str, content: str) -> None:
    """Append content, inserting a newline first if the file lacks a trailing one."""
    notes_dir = get_notes_dir(base_dir)
    notes_dir.mkdir(parents=True, exist_ok=True)
    file_path = get_note_file_path(base_dir, key)

    prefix = ""
    if file_path.exists() and file_path.stat().st_size > 0:
        with open(file_path, "rb") as f:
            try:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    prefix = "\n"
            except OSError:
                prefix = "\n"

    with file_path.open("a", encoding="utf-8") as f:
        f.write(prefix + content)


def update_note_content(base_dir, key: str, target: str, replacement: str) -> bool:
    """Replace the first occurrence of *target* in a note.

    Returns True if the replacement happened, False if *target* was not found.
    Raises FileNotFoundError if the note does not exist.
    """
    file_path = get_note_file_path(base_dir, key)
    if not file_path.exists():
        raise FileNotFoundError(f"Note '{key}' not found.")
    content = file_path.read_text(encoding="utf-8")
    if target not in content:
        return False
    file_path.write_text(content.replace(target, replacement, 1), encoding="utf-8")
    return True


class NoteStore:
    """Convenience wrapper binding the five ops to a single memory base dir."""

    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)

    def save(self, key: str, content: str) -> None:
        save_note_content(self.base_dir, key, content)

    def read(self, key: str, start_line: int | None = None,
             end_line: int | None = None) -> str:
        return read_note_content(self.base_dir, key, start_line, end_line)

    def append(self, key: str, content: str) -> None:
        append_note_content(self.base_dir, key, content)

    def update(self, key: str, target: str, replacement: str) -> bool:
        return update_note_content(self.base_dir, key, target, replacement)

    def info(self) -> dict[str, int]:
        return list_notes_info(self.base_dir)

    def keys(self) -> list[str]:
        return list_notes_keys(self.base_dir)