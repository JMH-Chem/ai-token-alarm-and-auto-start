"""Entry points called by Claude Code itself: the status line and hooks.

These run very often, so they must be quick, print nothing unexpected, and never fail loudly.
They only append to the spool (see ``spool.py``); the daemon does the rest.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from . import paths, spool
from .config import format_duration
from .usage.parse import parse_statusline

HOOK_EVENTS = {
    "UserPromptSubmit": "prompt",
    "Stop": "stop",
    "SessionStart": "session_start",
    "SessionEnd": "session_end",
    "Notification": "idle_prompt",
}
HEARTBEAT_SECONDS = 300


def _read_stdin_json() -> dict:
    try:
        data = sys.stdin.buffer.read().decode("utf-8-sig", errors="replace")
        return json.loads(data) if data.strip() else {}
    except Exception:
        return {}


def hook_main(event: str) -> int:
    if os.environ.get("TOKENALARM_AUTORUN") == "1":
        return 0  # our own autorun sessions are not "user activity"
    payload = _read_stdin_json()
    name = HOOK_EVENTS.get(event)
    if name is None:
        return 0
    if event == "Notification":
        ntype = payload.get("notification_type") or payload.get("type")
        if ntype and ntype != "idle_prompt":
            return 0
    try:
        spool.append(paths.spool_dir(), {"kind": "hook", "event": name, "session_id": payload.get("session_id")})
    except Exception:
        pass
    return 0  # never print: UserPromptSubmit stdout would be added to the conversation


def _color(pct: float) -> str:
    if pct >= 85:
        return "\033[31m"
    if pct >= 60:
        return "\033[33m"
    return "\033[32m"


RESET = "\033[0m"


def render_statusline(payload: dict, state: dict | None, now: float) -> str:
    snap = parse_statusline(payload, now)
    parts = []
    if snap:
        five = snap.get("five_hour")
        week = snap.get("seven_day")
        if five:
            parts.append(f"5h {_color(five.pct)}{five.pct:.0f}%{RESET} ({format_duration(five.seconds_left(now))})")
        if week:
            text = f"주 {_color(week.pct)}{week.pct:.0f}%{RESET} ({format_duration(week.seconds_left(now))})"
            proj = (state or {}).get("week", {}).get("projected")
            if proj is not None:
                text += f" → 예상 {proj:.0f}%"
            parts.append(text)
    else:
        parts.append("사용량: 첫 응답 후 표시")
    if state:
        if state.get("state") == "FILLING":
            parts.append("⚡자동실행 중")
        elif state.get("target") is not None and state.get("wants_fill"):
            parts.append(f"⚡대기: {state['target']:g}%까지")
    return " │ ".join(parts)


def statusline_main() -> int:
    now = time.time()
    payload = _read_stdin_json()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # cp949 consoles would garble │ ⚡
    except Exception:
        pass
    try:
        _spool_statusline(payload, now)
    except Exception:
        pass
    state = None
    try:
        sf = paths.state_file()
        if sf.exists() and now - sf.stat().st_mtime < 3 * 3600:
            state = json.loads(sf.read_text(encoding="utf-8"))
    except Exception:
        state = None
    try:
        sys.stdout.write(render_statusline(payload, state, now))
    except Exception:
        sys.stdout.write("tokenalarm")
    return 0


def _spool_statusline(payload: dict, now: float) -> None:
    rl = payload.get("rate_limits")
    cost = payload.get("cost") or {}
    activity_key = [payload.get("session_id"), cost.get("total_cost_usd"), cost.get("total_api_duration_ms")]
    data_key = json.dumps([rl, activity_key], sort_keys=True, default=str)
    marker = Path(paths.spool_dir()) / "statusline.last.json"
    last = {}
    try:
        last = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        pass
    changed_activity = last.get("activity") != activity_key
    if last.get("key") == data_key and now - float(last.get("ts", 0)) < HEARTBEAT_SECONDS:
        return
    spool.append(paths.spool_dir(), {
        "kind": "statusline", "ts": now, "session_id": payload.get("session_id"), "rate_limits": rl,
        "activity": bool(changed_activity and last),  # first sighting is not proof of activity
    })
    tmp = marker.with_suffix(".tmp")
    tmp.write_text(json.dumps({"key": data_key, "ts": now, "activity": activity_key}), encoding="utf-8")
    os.replace(tmp, marker)
