"""Background loop: collect usage, forecast, alert, and fill leftover quota near the weekly reset.

State machine (persisted in the kv table):

    MONITOR ──(target + idle + guards OK)──▶ WARNED ──(pre_run_warning elapsed)──▶ FILLING
       ▲                                       │ (user came back / guard)                │
       └───────────────────────────────────────┴────────────── COOLDOWN / WAIT_5H ◀──────┘
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Callable

from . import paths
from .activity import Activity
from .config import Config, format_duration
from .fill import FillController, describe_report
from .forecast import WindowForecast, forecast_window
from .ingest import ingest_spool
from .notify import Notifier
from .policy import Decision, decide, in_quiet_hours, next_checkpoint, run_guard
from .store import Store
from .usage.base import FIVE_HOUR, SEVEN_DAY, Snapshot
from .usage.chain import UsageService

log = logging.getLogger(__name__)


@dataclass
class TickResult:
    state: str
    decision: str
    target: float | None
    wants_fill: bool
    blocked_by: str | None
    action: str
    week: WindowForecast | None = None
    five: WindowForecast | None = None


class Daemon:
    def __init__(self, cfg: Config, store: Store, usage: UsageService, filler_factory: Callable[[], FillController],
                 activity: Activity, notifier: Notifier, now: Callable[[], float] = time.time,
                 dry_run: bool = False):
        self.cfg = cfg
        self.store = store
        self.usage = usage
        self.filler_factory = filler_factory
        self.activity = activity
        self.notifier = notifier
        self.now = now
        self.dry_run = dry_run

    # --------------------------------------------------------------------------------------------
    def _state(self) -> dict:
        return self.store.get("daemon_state") or {"state": "MONITOR"}

    def _set_state(self, **kw) -> None:
        if not self.dry_run:
            self.store.set("daemon_state", kw)

    def decide(self, snap: Snapshot | None, now: float) -> Decision:
        sticky = self.store.get("sticky_fill")
        d = decide(snap, self.cfg.weekly_fill_rules, self.cfg.guards, now, sticky)
        if d.resets_at and sticky and abs(float(sticky.get("resets_at", 0)) - d.resets_at) >= 3600:
            self.store.set("sticky_fill", None)
        if d.wants_fill and d.resets_at and not self.dry_run:
            self.store.set("sticky_fill", {"resets_at": d.resets_at, "target": d.target})
        return d

    def blocked_reason(self, snap: Snapshot | None, d: Decision, now: float) -> str | None:
        if paths.pause_file().exists():
            return "일시정지 (PAUSE 파일)"
        pu = self.store.get("pause_until")
        if pu and now < float(pu):
            return f"일시정지 중 ({format_duration(float(pu) - now)} 남음)"
        if in_quiet_hours(self.cfg.guards.quiet_hours, now):
            return f"자동 실행 금지 시간대 ({self.cfg.guards.quiet_hours})"
        st = self._state()
        if st.get("state") in ("COOLDOWN", "WAIT_5H") and now < float(st.get("until", 0)):
            return f"{st.get('why', '대기')} ({format_duration(float(st['until']) - now)} 남음)"
        guard = run_guard(snap, self.cfg.guards, d.target)
        if guard:
            return guard
        ok, why = self.activity.can_start(now)
        return None if ok else why

    # --------------------------------------------------------------------------------------------
    def tick(self) -> TickResult:
        now = self.now()
        ingest_spool(self.store)
        if self._state().get("state") == "FILLING":
            self._set_state(state="MONITOR")  # fills run inside tick(); a persisted FILLING is from a crash
        snap = self.usage.current(now)
        d = self.decide(snap, now)
        blocked = self.blocked_reason(snap, d, now) if d.wants_fill else None
        about_to_fill = d.wants_fill and blocked is None
        nxt = next_checkpoint(self.cfg.weekly_fill_rules, d.seconds_left) if d.seconds_left is not None else None
        near = nxt is not None and nxt <= 3600
        fresh = self.usage.maybe_refresh(now, near_checkpoint=near or d.wants_fill, about_to_fill=about_to_fill)
        if fresh is not None and (snap is None or fresh.observed_at != snap.observed_at):
            snap = fresh
            d = self.decide(snap, now)
            blocked = self.blocked_reason(snap, d, now) if d.wants_fill else None
            about_to_fill = d.wants_fill and blocked is None

        week_fc = five_fc = None
        if snap:
            if snap.get(SEVEN_DAY):
                week_fc = forecast_window(self.store, snap.get(SEVEN_DAY), now)
            if snap.get(FIVE_HOUR):
                five_fc = forecast_window(self.store, snap.get(FIVE_HOUR), now, recent_hours=1)
        if not self.dry_run:
            self._alerts(snap, d, week_fc, five_fc, now)

        state = self._state()
        action = "감시 중"
        if about_to_fill:
            warn = self.cfg.guards.pre_run_warning
            if self.dry_run:
                pre = f"사전 알림 → {format_duration(warn)} 후 시작 예정 · " if warn > 0 else ""
                action = f"[dry-run] {pre}{self._fill(d, now)}"
            elif state.get("state") != "WARNED" and warn > 0:
                self._set_state(state="WARNED", warned_at=now, target=d.target)
                if not self.dry_run:
                    self.notifier.send(
                        "곧 남은 사용량으로 자동 작업 실행",
                        f"{d.reason}\n{format_duration(warn)} 후 시작합니다. 취소: tokenalarm pause",
                        key=f"warn:{d.resets_at}:{d.target}", dedupe_seconds=6 * 3600, now=now)
                action = f"사전 알림 발송, {format_duration(warn)} 후 시작"
            elif warn <= 0 or now - float(state.get("warned_at", now)) >= warn:
                action = self._fill(d, now)
            else:
                left = warn - (now - float(state.get("warned_at", now)))
                action = f"{format_duration(left)} 후 자동 실행 시작"
        else:
            if state.get("state") == "WARNED":
                self._set_state(state="MONITOR")
                if d.wants_fill and blocked:
                    action = f"자동 실행 보류: {blocked}"
            elif d.wants_fill and blocked:
                action = f"자동 실행 보류: {blocked}"

        result = TickResult(self._state().get("state", "MONITOR"), d.reason, d.target, d.wants_fill, blocked,
                            action, week_fc, five_fc)
        if not self.dry_run:
            self._write_state_file(result, now)
        return result

    def _fill(self, d: Decision, now: float) -> str:
        assert d.target is not None
        if self.dry_run:
            report = self.filler_factory().fill(d.target, dry_run=True, label="자동")
            return report.stop_reason or "[dry-run]"
        self._set_state(state="FILLING", started_at=now, target=d.target)
        self._write_state_file(TickResult("FILLING", d.reason, d.target, True, None, "자동 실행 중"), now)
        try:
            report = self.filler_factory().fill(d.target, label=f"자동 ({d.reason})")
        except Exception as e:  # keep the daemon alive
            log.exception("fill failed")
            self._set_state(state="COOLDOWN", until=now + 1800, why=f"오류: {e}")
            self.notifier.send("자동 실행 오류", str(e), key="fill-error", dedupe_seconds=3600)
            return f"오류: {e}"
        end = self.now()
        reason = report.stop_reason or ""
        snap = self.usage.current(end)
        five = snap.get(FIVE_HOUR) if snap else None
        if "5시간" in reason and five and five.resets_at:
            self._set_state(state="WAIT_5H", until=five.resets_at + 60, why="5시간 창 상한 — 리셋 대기")
        elif "사용자" in reason or "입력" in reason:
            self._set_state(state="COOLDOWN", until=end + self.cfg.guards.require_idle_minutes * 60,
                            why="사용자 복귀")
        elif not report.outcomes:
            self._set_state(state="COOLDOWN", until=end + 3600, why=reason or "작업 없음")
        else:
            self._set_state(state="MONITOR")
        if report.outcomes or not self.store.notified_recently(f"fill-nothing:{d.resets_at}", 12 * 3600, end):
            self.notifier.send("자동 실행 종료", describe_report(report),
                               key=None if report.outcomes else f"fill-nothing:{d.resets_at}",
                               dedupe_seconds=None if report.outcomes else 12 * 3600, now=end)
        return describe_report(report)

    # --------------------------------------------------------------------------------------------
    def _alerts(self, snap: Snapshot | None, d: Decision, week: WindowForecast | None,
                five: WindowForecast | None, now: float) -> None:
        n = self.cfg.notify
        if five and five.resets_at and five.pct >= n.five_hour_warn_pct:
            eta = five.eta_100_recent or five.eta_100_avg
            eta_s = f", 지금 속도면 {datetime.fromtimestamp(eta):%H:%M}에 한도" if eta else ""
            self.notifier.send("5시간 사용량 경고", f"5시간 창 {five.pct:.0f}% 사용 (리셋 "
                               f"{format_duration(five.seconds_left)} 후){eta_s}",
                               key=f"5h:{int(five.resets_at)}", dedupe_seconds=5 * 3600, now=now, priority="high")
        if week and week.resets_at and week.projected_waste is not None and week.seconds_left is not None:
            if week.seconds_left <= n.waste_warn_before and week.projected_waste >= n.waste_warn_pct:
                day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
                self.notifier.send(
                    "토큰이 남을 것 같아요",
                    f"주간 {week.pct:.0f}% 사용, 리셋 {format_duration(week.seconds_left)} 전. 지금 페이스면 "
                    f"리셋 때 약 {week.projected_at_reset:.0f}% → {week.projected_waste:.0f}% 낭비 예상.\n{d.reason}",
                    key=f"waste:{int(week.resets_at)}:{day}", dedupe_seconds=20 * 3600, now=now)
        # weekly reset detection
        if week and week.resets_at:
            last_reset = self.store.get("last_week_reset")
            if last_reset and abs(float(last_reset) - week.resets_at) > 3600:
                final = self.store.get("last_week_pct")
                if final is not None:
                    self.notifier.send("주간 사용량 리셋", f"지난주 최종 사용률 약 {float(final):.0f}%. 새 주 시작!",
                                       key=f"reset:{int(week.resets_at)}", dedupe_seconds=86400, now=now)
            self.store.set("last_week_reset", week.resets_at)
            self.store.set("last_week_pct", week.pct)
        if n.daily_summary_hour is not None and week:
            t = datetime.fromtimestamp(now)
            if t.hour >= n.daily_summary_hour:
                self.notifier.send("오늘의 Claude 사용량", summary_text(week, five, d),
                                   key=f"daily:{t:%Y-%m-%d}", dedupe_seconds=20 * 3600, now=now, priority="low")

    def _write_state_file(self, r: TickResult, now: float) -> None:
        data = {
            "updated_at": now, "state": r.state, "decision": r.decision, "target": r.target,
            "wants_fill": r.wants_fill, "blocked_by": r.blocked_by, "action": r.action,
            "week": _fc_dict(r.week), "five": _fc_dict(r.five),
        }
        tmp = paths.state_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, paths.state_file())

    # --------------------------------------------------------------------------------------------
    def run_forever(self, stop_after: float | None = None) -> None:
        start = time.time()
        while True:
            try:
                r = self.tick()
                log.info("[%s] %s — %s", r.state, r.decision, r.action)
            except Exception:
                log.exception("tick failed")
            if stop_after and time.time() - start > stop_after:
                return
            time.sleep(max(5.0, self.cfg.daemon.tick_seconds))


def _fc_dict(fc: WindowForecast | None) -> dict | None:
    if fc is None:
        return None
    d = asdict(fc)
    d["projected"] = d.pop("projected_at_reset")
    return d


def summary_text(week: WindowForecast | None, five: WindowForecast | None, d: Decision | None) -> str:
    lines = []
    if week:
        lines.append(f"주간 {week.pct:.0f}% (리셋 {format_duration(week.seconds_left)} 후, 균등 페이스 "
                     f"{week.even_pace_pct or 0:.0f}%)")
        if week.avg_rate_h is not None:
            lines.append(f"평균 소모 {week.avg_rate_h * 24:.1f}%/일 → 리셋 때 예상 {week.projected_at_reset:.0f}%"
                         f" (낭비 예상 {week.projected_waste:.0f}%)")
        if week.autorun_pct:
            lines.append(f"이번 주 자동 실행 사용분 {week.autorun_pct:.1f}%")
        eta = week.eta_100_recent or week.eta_100_avg
        if eta:
            lines.append(f"⚠ 지금 속도면 {datetime.fromtimestamp(eta):%m/%d %H:%M}에 주간 한도 도달")
    if five:
        lines.append(f"5시간 창 {five.pct:.0f}% (리셋 {format_duration(five.seconds_left)} 후)")
    if d:
        lines.append(d.reason)
    return "\n".join(lines)


class SingleInstance:
    """OS-level exclusive lock so the logon task and a manual run never start two daemons."""

    def __init__(self, path):
        self.path = path
        self.fh = None

    def acquire(self) -> bool:
        self.fh = open(self.path, "a+")
        try:
            if sys.platform == "win32":
                import msvcrt

                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            self.fh = None
            return False
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(str(os.getpid()))
        self.fh.flush()
        return True

    def release(self) -> None:
        if self.fh:
            try:
                self.fh.close()
            finally:
                self.fh = None
