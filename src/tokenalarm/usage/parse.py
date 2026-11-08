"""Parsers for every place Claude Code exposes plan utilization.

All functions are pure (no I/O) so they are easy to test against fixtures.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable

from .base import FIVE_HOUR, SEVEN_DAY, Snapshot, Window


def _epoch(value: Any) -> float | None:
    """Accept epoch seconds / milliseconds or ISO-8601 strings."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v > 1e11 else v
    text = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return _epoch(float(text))
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _scale(value: Any, scale: str) -> float | None:
    """rate_limit_event utilization → percent. ``auto`` treats values <= 1.0 as fractions."""
    if value is None:
        return None
    v = float(value)
    if scale == "percent":
        return v
    if scale == "fraction":
        return v * 100.0
    return v * 100.0 if v <= 1.0 else v


# --- 1. status line stdin JSON (official, v2.1.80+) -------------------------------------------

def parse_statusline(payload: dict[str, Any], observed_at: float) -> Snapshot | None:
    rl = payload.get("rate_limits") or {}
    windows = []
    for kind in (FIVE_HOUR, SEVEN_DAY):
        w = rl.get(kind)
        if isinstance(w, dict) and w.get("used_percentage") is not None:
            windows.append(Window(kind, float(w["used_percentage"]), _epoch(w.get("resets_at"))))
    if not windows:
        return None
    return Snapshot(observed_at, "statusline", {w.kind: w for w in windows})


# --- 2. stream-json rate_limit_event (emitted after model responses) --------------------------

def parse_rate_limit_event(event: dict[str, Any], observed_at: float, scale: str = "auto",
                           source: str = "autorun") -> Snapshot | None:
    info = event.get("rate_limit_info") or {}
    windows: list[Window] = []
    unified = info.get("unifiedWindows") or {}
    for name, w in unified.items():
        if not isinstance(w, dict):
            continue
        pct = _scale(w.get("utilization"), scale)
        if pct is None:
            continue
        windows.append(Window(_normalise_kind(name), pct, _epoch(w.get("resetsAt"))))
    if not windows and info.get("utilization") is not None and info.get("rateLimitType"):
        windows.append(Window(_normalise_kind(info["rateLimitType"]),
                              _scale(info["utilization"], scale), _epoch(info.get("resetsAt"))))
    if not windows:
        return None
    return Snapshot(observed_at, source, {w.kind: w for w in windows})


def _normalise_kind(name: str) -> str:
    n = str(name).strip().lower()
    aliases = {"5h": FIVE_HOUR, "session": FIVE_HOUR, "five_hour": FIVE_HOUR,
               "7d": SEVEN_DAY, "weekly": SEVEN_DAY, "weekly_all": SEVEN_DAY, "seven_day": SEVEN_DAY}
    if n in aliases:
        return aliases[n]
    for prefix in ("seven_day_", "weekly_"):
        if n.startswith(prefix):
            return f"{SEVEN_DAY}:{n[len(prefix):]}"
    return n


# --- 3. /usage → usage_report (Agent SDK / stream-json) ----------------------------------------

def parse_usage_report(report: dict[str, Any], observed_at: float,
                       source: str = "usage_command") -> Snapshot | None:
    rl = report.get("rate_limits") or {}
    windows: list[Window] = []
    for row in rl.get("limits") or []:
        if not isinstance(row, dict) or row.get("percent") is None:
            continue
        kind = row.get("kind")
        if kind == "session":
            k = FIVE_HOUR
        elif kind == "weekly_all":
            k = SEVEN_DAY
        elif kind == "weekly_scoped":
            scope = (((row.get("scope") or {}).get("model") or {}).get("display_name")
                     or row.get("group") or "scoped")
            k = f"{SEVEN_DAY}:{str(scope).strip().lower().replace(' ', '_')}"
        else:
            continue
        windows.append(Window(k, float(row["percent"]), _epoch(row.get("resets_at"))))
    if not windows:
        return None
    return Snapshot(observed_at, source, {w.kind: w for w in windows})


_TEXT_ROW = re.compile(
    r"(?P<label>current session|current week \(all models\)|current week \((?P<scope>[^)]+)\)|"
    r"session|weekly)[^%]{0,80}?(?P<pct>\d{1,3}(?:\.\d+)?)\s*%\s*used", re.I)


def parse_usage_text(text: str, observed_at: float) -> Snapshot | None:
    """Best-effort parse of the human-readable /usage plan bars ("Current session ... 42% used")."""
    windows: dict[str, Window] = {}
    for m in _TEXT_ROW.finditer(text or ""):
        label = m.group("label").lower()
        if label.startswith("current session") or label == "session":
            kind = FIVE_HOUR
        elif "all models" in label or label == "weekly":
            kind = SEVEN_DAY
        else:
            kind = f"{SEVEN_DAY}:{m.group('scope').strip().lower().replace(' ', '_')}"
        windows.setdefault(kind, Window(kind, float(m.group("pct")), None))
    if not windows:
        return None
    return Snapshot(observed_at, "usage_command", windows)


def snapshot_from_stream(events: Iterable[dict[str, Any]], observed_at: float, scale: str = "auto",
                         source: str = "usage_command") -> Snapshot | None:
    """Merge everything useful found in a stream-json transcript (last value wins)."""
    merged: dict[str, Window] = {}
    for ev in events:
        snap = None
        if ev.get("type") == "rate_limit_event":
            snap = parse_rate_limit_event(ev, observed_at, scale, source)
        if snap is None and isinstance(ev.get("usage_report"), dict):
            snap = parse_usage_report(ev["usage_report"], observed_at, source)
        if snap is None and ev.get("type") == "result" and isinstance(ev.get("result"), str):
            snap = parse_usage_text(ev["result"], observed_at)
        if snap:
            merged.update(snap.windows)
    if not merged:
        return None
    return Snapshot(observed_at, source, merged)


# --- 4. undocumented api/oauth/usage (opt-in) --------------------------------------------------

def parse_oauth_usage(data: dict[str, Any], observed_at: float) -> Snapshot | None:
    windows: list[Window] = []
    for key, value in (data or {}).items():
        if not isinstance(value, dict) or value.get("utilization") is None:
            continue
        if key == FIVE_HOUR or key == SEVEN_DAY or key.startswith("seven_day_"):
            kind = _normalise_kind(key)
            windows.append(Window(kind, float(value["utilization"]), _epoch(value.get("resets_at"))))
    if not windows:
        return None
    return Snapshot(observed_at, "oauth", {w.kind: w for w in windows})
