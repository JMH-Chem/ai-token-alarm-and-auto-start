"""Is the user working right now?

Signals (best first):
1. Claude Code hooks / status line heartbeats (spooled by ``tokenalarm hook`` / ``tokenalarm statusline``).
   Autorun sessions are excluded because their hooks see ``TOKENALARM_AUTORUN=1``.
2. Windows keyboard/mouse idle time (``GetLastInputInfo``). Only meaningful in the interactive user
   session — register the scheduled task as "run only when user is logged on".
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

from .config import Guards
from .store import Store

ACTIVE_EVENTS = {"prompt", "stop", "statusline", "session_start"}
END_EVENTS = {"stop", "idle_prompt", "session_end"}


def input_idle_seconds() -> float | None:
    """Seconds since the last keyboard/mouse input (Windows only, else None)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        tick = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
        return ((tick - info.dwTime) & 0xFFFFFFFF) / 1000.0  # handles the 49.7-day wrap-around
    except Exception:
        return None


@dataclass
class ActivityState:
    claude_busy: bool
    reason: str
    last_activity: float | None
    input_idle: float | None


class Activity:
    def __init__(self, store: Store, guards: Guards, input_idle_fn=input_idle_seconds):
        self.store = store
        self.guards = guards
        self.input_idle_fn = input_idle_fn

    def state(self, now: float) -> ActivityState:
        g = self.guards
        lookback = max(g.session_heartbeat_timeout, g.require_idle_minutes * 60) + 60
        rows = self.store.activity_since(now - lookback)
        last_active = None
        sessions: dict[str, tuple[str, float]] = {}
        for r in rows:
            if r["event"] in ACTIVE_EVENTS:
                last_active = r["ts"] if last_active is None else max(last_active, r["ts"])
            if r["session_id"]:
                sessions[r["session_id"]] = (r["event"], r["ts"])
        input_idle = self.input_idle_fn()
        for event, ts in sessions.values():
            if event == "prompt" and now - ts < g.session_heartbeat_timeout:
                return ActivityState(True, "Claude Code가 응답 생성 중 (대화형 세션)", last_active, input_idle)
        if last_active is not None and now - last_active < g.require_idle_minutes * 60:
            mins = int((now - last_active) // 60)
            return ActivityState(True, f"최근 {mins}분 전 Claude Code 사용", last_active, input_idle)
        return ActivityState(False, "Claude Code 유휴", last_active, input_idle)

    def can_start(self, now: float) -> tuple[bool, str]:
        st = self.state(now)
        if st.claude_busy:
            return False, st.reason
        need = self.guards.require_input_idle_minutes * 60
        if need > 0 and st.input_idle is not None and st.input_idle < need:
            return False, f"PC 사용 중 (입력 {int(st.input_idle // 60)}분 전)"
        return True, "유휴"

    def should_stop(self, now: float, started_at: float) -> str | None:
        """Called while autorun is filling: stop as soon as the user comes back."""
        rows = self.store.activity_since(started_at)
        if any(r["event"] in ("prompt", "session_start") for r in rows):
            return "사용자가 Claude Code를 다시 사용하기 시작함"
        if self.guards.stop_on_input:
            idle = self.input_idle_fn()
            if idle is not None and idle < 60 and now - started_at > 60:
                return "키보드/마우스 입력 감지"
        return None
