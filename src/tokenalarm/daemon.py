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
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Callable

from . import i18n, paths
from .activity import Activity
from .config import Config, format_duration
from .fill import FillController, describe_report
from .forecast import WindowForecast, forecast_window
from .i18n import L
from .ingest import ingest_spool
from .notify import Notifier
from .policy import Decision, decide, in_quiet_hours, next_checkpoint, reason_code, run_guard
from .store import Store
from .usage.base import FIVE_HOUR, SEVEN_DAY, Snapshot
from .usage.chain import UsageService

log = logging.getLogger(__name__)

Reloader = Callable[[], "Config | None"]


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
                 dry_run: bool = False, reloader: Reloader | None = None):
        self.cfg = cfg
        self.store = store
        self.usage = usage
        self.filler_factory = filler_factory
        self.activity = activity
        self.notifier = notifier
        self.now = now
        self.dry_run = dry_run
        self.reloader = reloader

    # --------------------------------------------------------------------------------------------
    def apply_config(self, cfg: Config) -> None:
        """Hot reload: swap the config everywhere it is held."""
        self.cfg = cfg
        self.usage.cfg = cfg
        self.activity.guards = cfg.guards
        self.notifier.cfg = cfg.notify
        i18n.set_lang(cfg.language)

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
            return L("일시정지 (PAUSE 파일)", "paused (PAUSE file)")
        pu = self.store.get("pause_until")
        if pu and now < float(pu):
            left = format_duration(float(pu) - now)
            return L(f"일시정지 중 ({left} 남음)", f"paused ({left} left)")
        if in_quiet_hours(self.cfg.guards.quiet_hours, now):
            q = self.cfg.guards.quiet_hours
            return L(f"자동 실행 금지 시간대 ({q})", f"quiet hours ({q})")
        st = self._state()
        if st.get("state") in ("COOLDOWN", "WAIT_5H") and now < float(st.get("until", 0)):
            left = format_duration(float(st["until"]) - now)
            why = st.get("why") or L("대기", "waiting")
            return L(f"{why} ({left} 남음)", f"{why} ({left} left)")
        guard = run_guard(snap, self.cfg.guards, d.target)
        if guard:
            return guard
        ok, why = self.activity.can_start(now)
        return None if ok else why

    # --------------------------------------------------------------------------------------------
    def tick(self) -> TickResult:
        now = self.now()
        if self.reloader is not None:
            new_cfg = self.reloader()
            if new_cfg is not None:
                self.apply_config(new_cfg)
                log.info("config reloaded")
        ingest_spool(self.store)
        if self._state().get("state") == "FILLING":
            self._set_state(state="MONITOR")  # fills run inside tick(); a persisted FILLING is from a crash
        self._maintenance(now)
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
        action = L("감시 중", "monitoring")
        if about_to_fill:
            warn = self.cfg.guards.pre_run_warning
            if self.dry_run:
                pre = ""
                if warn > 0:
                    w = format_duration(warn)
                    pre = L(f"사전 알림 → {w} 후 시작 예정 · ", f"pre-run notice → would start in {w} · ")
                action = f"[dry-run] {pre}{self._fill(d, now)}"
            elif state.get("state") != "WARNED" and warn > 0:
                self._set_state(state="WARNED", warned_at=now, target=d.target)
                w = format_duration(warn)
                self.notifier.send(
                    L("곧 남은 사용량으로 자동 작업 실행", "Autorun starting soon with leftover quota"),
                    L(f"{d.reason}\n{w} 후 시작합니다. 취소: tokenalarm pause",
                      f"{d.reason}\nStarting in {w}. Cancel: tokenalarm pause"),
                    key=f"warn:{d.resets_at}:{d.target}", dedupe_seconds=6 * 3600, now=now)
                action = L(f"사전 알림 발송, {w} 후 시작", f"pre-run notice sent, starting in {w}")
            elif warn <= 0 or now - float(state.get("warned_at", now)) >= warn:
                action = self._fill(d, now)
            else:
                left = format_duration(warn - (now - float(state.get("warned_at", now))))
                action = L(f"{left} 후 자동 실행 시작", f"autorun starts in {left}")
        else:
            if state.get("state") == "WARNED":
                self._set_state(state="MONITOR")
            if d.wants_fill and blocked:
                action = L(f"자동 실행 보류: {blocked}", f"autorun on hold: {blocked}")

        result = TickResult(self._state().get("state", "MONITOR"), d.reason, d.target, d.wants_fill, blocked,
                            action, week_fc, five_fc)
        if not self.dry_run:
            self._write_state_file(result, now)
        return result

    def _fill(self, d: Decision, now: float) -> str:
        assert d.target is not None
        if self.dry_run:
            report = self.filler_factory().fill(d.target, dry_run=True, label=L("자동", "auto"))
            return report.stop_reason or "[dry-run]"
        self._set_state(state="FILLING", started_at=now, target=d.target)
        self._write_state_file(TickResult("FILLING", d.reason, d.target, True, None,
                                          L("자동 실행 중", "autorun in progress")), now)
        try:
            report = self.filler_factory().fill(d.target, label=L(f"자동 ({d.reason})", f"auto ({d.reason})"))
        except Exception as e:  # keep the daemon alive
            log.exception("fill failed")
            self._set_state(state="COOLDOWN", until=now + 1800, why=L(f"오류: {e}", f"error: {e}"))
            self.notifier.send(L("자동 실행 오류", "Autorun error"), str(e), key="fill-error", dedupe_seconds=3600)
            return L(f"오류: {e}", f"error: {e}")
        end = self.now()
        code = reason_code(report.stop_reason)
        snap = self.usage.current(end)
        five = snap.get(FIVE_HOUR) if snap else None
        if code == "five_hour" and five and five.resets_at:
            self._set_state(state="WAIT_5H", until=five.resets_at + 60,
                            why=L("5시간 창 상한 — 리셋 대기", "5-hour ceiling — waiting for reset"))
        elif code in ("user_back", "input"):
            self._set_state(state="COOLDOWN", until=end + self.cfg.guards.require_idle_minutes * 60,
                            why=L("사용자 복귀", "you are back"))
        elif not report.outcomes:
            self._set_state(state="COOLDOWN", until=end + 3600,
                            why=str(report.stop_reason or L("작업 없음", "no tasks")))
        else:
            self._set_state(state="MONITOR")
        if report.outcomes or not self.store.notified_recently(f"fill-nothing:{d.resets_at}", 12 * 3600, end):
            self.notifier.send(L("자동 실행 종료", "Autorun finished"), describe_report(report),
                               key=None if report.outcomes else f"fill-nothing:{d.resets_at}",
                               dedupe_seconds=None if report.outcomes else 12 * 3600, now=end)
        return describe_report(report)

    # --------------------------------------------------------------------------------------------
    def _maintenance(self, now: float) -> None:
        last = self.store.get("maintenance_at", 0) or 0
        if now - float(last) < 86400 or self.dry_run:
            return
        self.store.set("maintenance_at", now)
        try:
            self.store.prune()
        except Exception as e:  # pragma: no cover
            log.warning("prune failed: %s", e)

    def _alerts(self, snap: Snapshot | None, d: Decision, week: WindowForecast | None,
                five: WindowForecast | None, now: float) -> None:
        n = self.cfg.notify
        if five and five.resets_at and five.pct >= n.five_hour_warn_pct:
            eta = five.eta_100_recent or five.eta_100_avg
            left = format_duration(five.seconds_left)
            eta_s = ""
            if eta:
                hm = f"{datetime.fromtimestamp(eta):%H:%M}"
                eta_s = L(f", 지금 속도면 {hm}에 한도", f", limit at {hm} at this pace")
            self.notifier.send(L("5시간 사용량 경고", "5-hour usage warning"),
                               L(f"5시간 창 {five.pct:.0f}% 사용 (리셋 {left} 후){eta_s}",
                                 f"5-hour window at {five.pct:.0f}% (resets in {left}){eta_s}"),
                               key=f"5h:{int(five.resets_at)}", dedupe_seconds=5 * 3600, now=now, priority="high")
        if week and week.resets_at and week.projected_waste is not None and week.seconds_left is not None:
            if week.seconds_left <= n.waste_warn_before and week.projected_waste >= n.waste_warn_pct:
                day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
                left = format_duration(week.seconds_left)
                self.notifier.send(
                    L("토큰이 남을 것 같아요", "You're likely to leave quota unused"),
                    L(f"주간 {week.pct:.0f}% 사용, 리셋 {left} 전. 지금 페이스면 리셋 때 약 "
                      f"{week.projected_at_reset:.0f}% → {week.projected_waste:.0f}% 낭비 예상.\n{d.reason}",
                      f"Weekly {week.pct:.0f}% used, {left} before reset. At this pace you'll end near "
                      f"{week.projected_at_reset:.0f}% → ~{week.projected_waste:.0f}% unused.\n{d.reason}"),
                    key=f"waste:{int(week.resets_at)}:{day}", dedupe_seconds=20 * 3600, now=now)
        if week and week.resets_at:
            last_reset = self.store.get("last_week_reset")
            if last_reset and abs(float(last_reset) - week.resets_at) > 3600:
                final = self.store.get("last_week_pct")
                if final is not None:
                    self.notifier.send(L("주간 사용량 리셋", "Weekly usage reset"),
                                       L(f"지난주 최종 사용률 약 {float(final):.0f}%. 새 주 시작!",
                                         f"Last week ended at about {float(final):.0f}%. New week started!"),
                                       key=f"reset:{int(week.resets_at)}", dedupe_seconds=86400, now=now)
            self.store.set("last_week_reset", week.resets_at)
            self.store.set("last_week_pct", week.pct)
        if n.daily_summary_hour is not None and week:
            t = datetime.fromtimestamp(now)
            if t.hour >= n.daily_summary_hour:
                self.notifier.send(L("오늘의 Claude 사용량", "Today's Claude usage"), summary_text(week, five, d),
                                   key=f"daily:{t:%Y-%m-%d}", dedupe_seconds=20 * 3600, now=now, priority="low")

    def _write_state_file(self, r: TickResult, now: float) -> None:
        data = {
            "updated_at": now, "state": r.state, "decision": r.decision, "target": r.target,
            "wants_fill": r.wants_fill, "blocked_by": r.blocked_by, "action": r.action,
            "week": _fc_dict(r.week), "five": _fc_dict(r.five), "lang": i18n.lang(),
        }
        tmp = paths.state_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, paths.state_file())

    # --------------------------------------------------------------------------------------------
    def run_forever(self, stop_event: threading.Event | None = None, stop_after: float | None = None) -> None:
        """Loop until ``stop_event`` is set, a STOP file appears (``tokenalarm stop``), or ``stop_after``."""
        stop_event = stop_event or threading.Event()
        start = time.time()
        while not stop_event.is_set():
            if consume_stop_request():
                log.info("stop requested")
                break
            try:
                r = self.tick()
                log.info("[%s] %s — %s", r.state, r.decision, r.action)
            except Exception:
                log.exception("tick failed")
            if stop_after and time.time() - start > stop_after:
                return
            deadline = time.monotonic() + max(5.0, self.cfg.daemon.tick_seconds)
            while not stop_event.is_set() and time.monotonic() < deadline:
                if paths.stop_file().exists():
                    break
                stop_event.wait(2.0)


def consume_stop_request() -> bool:
    f = paths.stop_file()
    if f.exists():
        try:
            f.unlink()
        except OSError:
            pass
        return True
    return False


def _fc_dict(fc: WindowForecast | None) -> dict | None:
    if fc is None:
        return None
    d = asdict(fc)
    d["projected"] = d.pop("projected_at_reset")
    return d


def summary_text(week: WindowForecast | None, five: WindowForecast | None, d: Decision | None) -> str:
    lines = []
    if week:
        left = format_duration(week.seconds_left)
        even = week.even_pace_pct or 0
        lines.append(L(f"주간 {week.pct:.0f}% (리셋 {left} 후, 균등 페이스 {even:.0f}%)",
                       f"Weekly {week.pct:.0f}% (resets in {left}, even pace {even:.0f}%)"))
        if week.avg_rate_h is not None:
            per_day = week.avg_rate_h * 24
            lines.append(L(f"평균 소모 {per_day:.1f}%/일 → 리셋 때 예상 {week.projected_at_reset:.0f}%"
                           f" (낭비 예상 {week.projected_waste:.0f}%)",
                           f"Average {per_day:.1f}%/day → ~{week.projected_at_reset:.0f}% at reset"
                           f" (~{week.projected_waste:.0f}% unused)"))
        if week.autorun_pct:
            lines.append(L(f"이번 주 자동 실행 사용분 {week.autorun_pct:.1f}%",
                           f"Autorun used {week.autorun_pct:.1f}% this week"))
        eta = week.eta_100_recent or week.eta_100_avg
        if eta:
            when = f"{datetime.fromtimestamp(eta):%m/%d %H:%M}"
            lines.append(L(f"⚠ 지금 속도면 {when}에 주간 한도 도달", f"⚠ At this pace you hit the weekly limit at {when}"))
    if five:
        left = format_duration(five.seconds_left)
        lines.append(L(f"5시간 창 {five.pct:.0f}% (리셋 {left} 후)", f"5-hour window {five.pct:.0f}% (resets in {left})"))
    if d:
        lines.append(d.reason)
    return "\n".join(lines)


class SingleInstance:
    """OS-level exclusive lock so the logon task, the tray and a manual run never start two daemons."""

    def __init__(self, path):
        self.path = path
        self.fh = None

    def acquire(self, write_pid: bool = True) -> bool:
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
        if write_pid:  # separate file: a locked region cannot be read by others on Windows
            try:
                paths.pid_file().write_text(str(os.getpid()), encoding="utf-8")
            except OSError:
                pass
        return True

    def release(self) -> None:
        if self.fh:
            try:
                self.fh.close()
            finally:
                self.fh = None


def is_running() -> tuple[bool, int | None]:
    """(daemon/tray holds the lock?, pid written in the lock file)."""
    probe = SingleInstance(paths.lock_file())
    if probe.acquire(write_pid=False):
        probe.release()
        return False, None
    pid = None
    try:
        pid = int(paths.pid_file().read_text(encoding="utf-8").strip() or 0) or None
    except (OSError, ValueError):
        pass
    return True, pid
