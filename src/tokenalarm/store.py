"""SQLite persistence. Only the daemon / CLI write here; hooks and the status line use the spool."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .usage.base import Snapshot, Window

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  observed_at REAL NOT NULL,
  source TEXT NOT NULL,
  kind TEXT NOT NULL,
  pct REAL NOT NULL,
  resets_at REAL
);
CREATE INDEX IF NOT EXISTS ix_snap_kind_time ON snapshots(kind, observed_at);
CREATE TABLE IF NOT EXISTS activity (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  event TEXT NOT NULL,
  session_id TEXT,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_activity_ts ON activity(ts);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  started_at REAL NOT NULL,
  ended_at REAL,
  status TEXT,
  reason TEXT,
  target REAL,
  pct_start REAL,
  pct_end REAL,
  cost_usd REAL DEFAULT 0,
  turns INTEGER DEFAULT 0,
  chunks INTEGER DEFAULT 0,
  session_id TEXT,
  workdir TEXT,
  branch TEXT,
  log_dir TEXT,
  summary TEXT
);
CREATE TABLE IF NOT EXISTS task_state (
  task_id TEXT PRIMARY KEY,
  last_started_at REAL,
  last_finished_at REAL,
  last_status TEXT,
  last_success_at REAL,
  fail_count INTEGER DEFAULT 0,
  cooldown_until REAL,
  session_id TEXT,
  workdir TEXT,
  branch TEXT
);
CREATE TABLE IF NOT EXISTS notifications (
  key TEXT PRIMARY KEY,
  ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


@dataclass
class RunRow:
    id: int
    task_id: str
    started_at: float
    ended_at: float | None
    status: str | None
    reason: str | None
    target: float | None
    pct_start: float | None
    pct_end: float | None
    cost_usd: float
    turns: int
    chunks: int
    session_id: str | None
    workdir: str | None
    branch: str | None
    log_dir: str | None
    summary: str | None


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # --- kv --------------------------------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    def set(self, key: str, value: Any) -> None:
        if value is None:
            self.conn.execute("DELETE FROM kv WHERE key=?", (key,))
        else:
            self.conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)", (key, json.dumps(value)))

    # --- snapshots -------------------------------------------------------------------------------
    def add_snapshot(self, snap: Snapshot) -> None:
        """Store all windows. A window without a reset time inherits the last known one (if still ahead)."""
        for kind, w in list(snap.windows.items()):
            if w.resets_at is None:
                prev = self.conn.execute(
                    "SELECT resets_at FROM snapshots WHERE kind=? AND resets_at > ? ORDER BY observed_at DESC "
                    "LIMIT 1", (kind, snap.observed_at)).fetchone()
                if prev is not None:
                    snap.windows[kind] = Window(kind, w.pct, prev["resets_at"])
        with self.tx() as c:
            for w in snap.windows.values():
                c.execute(
                    "INSERT INTO snapshots(observed_at, source, kind, pct, resets_at) VALUES (?,?,?,?,?)",
                    (snap.observed_at, snap.source, w.kind, float(w.pct), w.resets_at),
                )

    def latest_window(self, kind: str, now: float | None = None) -> tuple[Window, float, str] | None:
        """Latest observation of ``kind`` whose reset time has not passed: (window, observed_at, source)."""
        now = time.time() if now is None else now
        row = self.conn.execute(
            "SELECT * FROM snapshots WHERE kind=? AND (resets_at IS NULL OR resets_at > ?) "
            "ORDER BY observed_at DESC, id DESC LIMIT 1",
            (kind, now),
        ).fetchone()
        if row is None:
            return None
        return Window(kind, row["pct"], row["resets_at"]), row["observed_at"], row["source"]

    def latest_snapshot(self, now: float | None = None) -> Snapshot | None:
        now = time.time() if now is None else now
        kinds = [r[0] for r in self.conn.execute("SELECT DISTINCT kind FROM snapshots")]
        windows: dict[str, Window] = {}
        newest = 0.0
        source = "store"
        for k in kinds:
            got = self.latest_window(k, now)
            if got:
                w, obs, src = got
                windows[k] = w
                if obs > newest:
                    newest, source = obs, src
        if not windows:
            return None
        return Snapshot(newest, source, windows)

    def window_series(self, kind: str, since: float, until: float | None = None) -> list[tuple[float, float, float | None]]:
        until = time.time() if until is None else until
        rows = self.conn.execute(
            "SELECT observed_at, pct, resets_at FROM snapshots WHERE kind=? AND observed_at BETWEEN ? AND ? "
            "ORDER BY observed_at, id",
            (kind, since, until),
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def prune(self, older_than_days: float = 120) -> None:
        cutoff = time.time() - older_than_days * 86400
        self.conn.execute("DELETE FROM snapshots WHERE observed_at < ?", (cutoff,))
        self.conn.execute("DELETE FROM activity WHERE ts < ?", (cutoff,))

    # --- activity --------------------------------------------------------------------------------
    def add_activity(self, ts: float, event: str, session_id: str | None, detail: str | None = None) -> None:
        self.conn.execute("INSERT INTO activity(ts, event, session_id, detail) VALUES (?,?,?,?)",
                          (ts, event, session_id, detail))

    def activity_since(self, since: float) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM activity WHERE ts >= ? ORDER BY ts, id", (since,)).fetchall()

    # --- runs ------------------------------------------------------------------------------------
    def start_run(self, task_id: str, started_at: float, target: float | None, pct_start: float | None,
                  workdir: str | None, branch: str | None, log_dir: str | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs(task_id, started_at, status, target, pct_start, workdir, branch, log_dir) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (task_id, started_at, "running", target, pct_start, workdir, branch, log_dir),
        )
        return int(cur.lastrowid)

    def update_run(self, run_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))

    def runs(self, limit: int = 20, since: float | None = None) -> list[RunRow]:
        if since is None:
            rows = self.conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM runs WHERE started_at >= ? ORDER BY started_at DESC LIMIT ?",
                                     (since, limit)).fetchall()
        return [RunRow(**dict(r)) for r in rows]

    # --- task state ------------------------------------------------------------------------------
    def task_state(self, task_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM task_state WHERE task_id=?", (task_id,)).fetchone()
        return dict(row) if row else {"task_id": task_id, "fail_count": 0}

    def save_task_state(self, task_id: str, **fields: Any) -> None:
        state = self.task_state(task_id)
        state.update(fields)
        state["task_id"] = task_id
        cols = list(state.keys())
        self.conn.execute(
            f"INSERT OR REPLACE INTO task_state({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            tuple(state[c] for c in cols),
        )

    # --- notifications dedupe ----------------------------------------------------------------------
    def notified_recently(self, key: str, within_seconds: float, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        row = self.conn.execute("SELECT ts FROM notifications WHERE key=?", (key,)).fetchone()
        return row is not None and now - row["ts"] < within_seconds

    def mark_notified(self, key: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.conn.execute("INSERT OR REPLACE INTO notifications(key, ts) VALUES (?,?)", (key, now))
