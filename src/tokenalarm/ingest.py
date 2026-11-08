"""Move hook / status line records from the spool into SQLite (called by the daemon and during autoruns)."""
from __future__ import annotations

import time

from . import paths, spool
from .store import Store
from .usage.parse import parse_statusline


def ingest_spool(store: Store) -> int:
    offset = int(store.get("spool_offset", 0) or 0)
    records, new_offset = spool.read_new(paths.spool_dir(), offset)
    for rec in records:
        ts = float(rec.get("ts") or time.time())
        if rec.get("kind") == "statusline":
            snap = parse_statusline({"rate_limits": rec.get("rate_limits")}, ts)
            if snap:
                store.add_snapshot(snap)
            if rec.get("activity"):
                store.add_activity(ts, "statusline", rec.get("session_id"))
        elif rec.get("kind") == "hook":
            store.add_activity(ts, str(rec.get("event")), rec.get("session_id"))
    store.set("spool_offset", new_offset)
    return len(records)
