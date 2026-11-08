"""Active usage probes using the official CLI.

* ``probe_usage_command`` — ``claude -p /usage``: local command, no model call, no quota.
* ``probe_ping``          — one-word Haiku request; reads ``rate_limit_event``. Costs a tiny amount and
  starts a 5-hour window, so the daemon only uses it right before it is going to spend anyway.
"""
from __future__ import annotations

import logging
import time

from ..claude_cli import autorun_env, run_stream, user_message
from .base import Snapshot
from .parse import snapshot_from_stream

log = logging.getLogger(__name__)


def probe_usage_command(claude_cmd: list[str], scale: str = "fraction", timeout: float = 60) -> Snapshot | None:
    cmd = [*claude_cmd, "-p", "/usage", "--output-format", "stream-json", "--verbose",
           "--no-session-persistence"]
    events = run_stream(cmd, None, autorun_env(), timeout)
    return snapshot_from_stream(events, time.time(), scale, source="usage_command")


def probe_ping(claude_cmd: list[str], model: str = "haiku", scale: str = "fraction", timeout: float = 90,
               allowed_key_sources: list[str] | None = None) -> Snapshot | None:
    cmd = [*claude_cmd, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
           "--model", model, "--max-turns", "1", "--no-session-persistence", "--tools", ""]
    events = run_stream(cmd, user_message("Reply with exactly: ok"), autorun_env(), timeout)
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), None)
    allowed = allowed_key_sources or ["none"]
    if init is not None and init.get("apiKeySource") not in allowed:
        log.warning("ping probe used apiKeySource=%s (not a subscription login)", init.get("apiKeySource"))
        return None
    return snapshot_from_stream(events, time.time(), scale, source="ping")
