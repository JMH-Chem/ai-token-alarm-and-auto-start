"""Append-only JSONL spool written by hooks and the status line.

Writers (hooks, status line) run on every prompt/refresh, so they must be fast, never block Claude Code
and never fight over a SQLite lock. They only append one line here; the daemon ingests the file into
SQLite and remembers its byte offset. Keep imports minimal in this module.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

SPOOL_NAME = "events.jsonl"
ROTATE_BYTES = 8 * 1024 * 1024


def spool_path(spool_dir: Path) -> Path:
    return Path(spool_dir) / SPOOL_NAME


def append(spool_dir: Path, record: dict) -> None:
    record.setdefault("ts", time.time())
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    path = spool_path(spool_dir)
    for _ in range(3):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
            return
        except PermissionError:  # Windows: file briefly locked during rotation
            time.sleep(0.05)


def read_new(spool_dir: Path, offset: int) -> tuple[list[dict], int]:
    """Return records after ``offset`` and the new offset. Handles truncation and rotation."""
    path = spool_path(spool_dir)
    if not path.exists():
        return [], 0
    size = path.stat().st_size
    if size < offset:
        offset = 0
    records: list[dict] = []
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    # Only consume complete lines; a writer may be mid-line.
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    for raw in data[: end + 1].splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            records.append(json.loads(raw.decode("utf-8", errors="replace")))
        except ValueError:
            continue
    new_offset = offset + end + 1
    if new_offset >= ROTATE_BYTES and new_offset == size:
        try:
            os.replace(path, path.with_suffix(".old"))
            new_offset = 0
        except OSError:
            pass
    return records, new_offset
