"""Fill controller: run queued tasks until weekly usage reaches a target %, then stop.

Used by ``tokenalarm run --until N`` (manual) and by the daemon (automatic, from the rule ladder).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from . import paths
from .activity import Activity
from .config import Config, format_duration
from .forecast import calibrate_pct_per_usd
from .ingest import ingest_spool
from .notify import Notifier
from .i18n import L
from .policy import Reason, run_guard
from .runner import RunOutcome, Runner
from .store import Store
from .tasks import Task, select_task
from .usage.base import SEVEN_DAY, Snapshot, Window
from .usage.chain import UsageService

log = logging.getLogger(__name__)


@dataclass
class FillReport:
    target: float
    pct_start: float | None
    pct_end: float | None = None
    stop_reason: str | None = None
    outcomes: list[RunOutcome] = field(default_factory=list)

    @property
    def cost(self) -> float:
        return sum(o.cost for o in self.outcomes)


class LiveUsage:
    """Latest known windows + an estimate of what has been spent since the last observation."""

    def __init__(self, snapshot: Snapshot | None):
        self.windows: dict[str, Window] = dict(snapshot.windows) if snapshot else {}
        self.observed_at = snapshot.observed_at if snapshot else 0.0
        self.unobserved_cost = 0.0

    def update(self, snap: Snapshot) -> None:
        self.windows.update(snap.windows)
        self.observed_at = snap.observed_at
        self.unobserved_cost = 0.0

    def add_cost(self, usd: float) -> None:
        self.unobserved_cost += max(0.0, usd)

    def snapshot(self) -> Snapshot:
        return Snapshot(self.observed_at, "live", dict(self.windows))

    def week_pct(self, pct_per_usd: float | None) -> float | None:
        w = self.windows.get(SEVEN_DAY)
        if w is None:
            return None
        extra = self.unobserved_cost * pct_per_usd if pct_per_usd else 0.0
        return w.pct + extra


class FillController:
    def __init__(self, cfg: Config, store: Store, usage: UsageService, runner: Runner, tasks: list[Task],
                 notifier: Notifier, activity: Activity | None = None, now: Callable[[], float] = time.time):
        self.cfg = cfg
        self.store = store
        self.usage = usage
        self.runner = runner
        self.tasks = tasks
        self.notifier = notifier
        self.activity = activity
        self.now = now
        self._last_ingest = 0.0

    def pct_per_usd(self) -> float | None:
        v = self.store.get("pct_per_usd")
        return float(v) if v else None

    def chunk_budget(self, target: float, live: LiveUsage) -> float:
        r = self.cfg.runner
        ppu = self.pct_per_usd()
        pct = live.week_pct(ppu)
        if not ppu or pct is None:
            return r.chunk_budget_usd_default
        gap = max(0.0, target - self.cfg.guards.stop_margin - pct)
        return max(r.chunk_budget_usd_min, min(r.chunk_budget_usd_max, gap / ppu * r.chunk_budget_fraction))

    def stop_reason(self, target: float, live: LiveUsage, started_at: float, deadline: float | None) -> str | None:
        if self.activity is not None and time.monotonic() - self._last_ingest >= 5:
            # Hooks keep spooling while we run: pick up "user is back" and fresh status line data.
            self._last_ingest = time.monotonic()
            try:
                ingest_spool(self.store)
            except Exception as e:  # pragma: no cover
                log.warning("spool ingest failed: %s", e)
        if paths.pause_file().exists():
            return Reason("pause", L("일시정지 (PAUSE 파일)", "paused (PAUSE file)"))
        pause_until = self.store.get("pause_until")
        if pause_until and self.now() < float(pause_until):
            return Reason("pause", L("일시정지 중", "paused"))
        if deadline and self.now() >= deadline:
            return Reason("deadline", L("시간 제한 도달", "time limit reached"))
        snap = live.snapshot()
        week = snap.windows.get(SEVEN_DAY)
        if week is not None:
            est = live.week_pct(self.pct_per_usd())
            snap.windows[SEVEN_DAY] = Window(SEVEN_DAY, est if est is not None else week.pct, week.resets_at)
        guard = run_guard(snap, self.cfg.guards, target)
        if guard:
            return guard
        if self.activity is not None:
            return self.activity.should_stop(self.now(), started_at)
        return None

    def fill(self, target: float, only_task: str | None = None, dry_run: bool = False,
             max_seconds: float | None = None, label: str | None = None) -> FillReport:
        label = label or L("수동", "manual")
        now = self.now()
        snap = self.usage.current(now)
        if snap is None or snap.get(SEVEN_DAY) is None or not self.usage.is_fresh(now):
            fresh = self.usage.refresh(now, allow_spend=not dry_run)
            snap = fresh if fresh is not None and fresh.get(SEVEN_DAY) else self.usage.current(now)
        live = LiveUsage(snap)
        report = FillReport(target, live.week_pct(None))
        if live.week_pct(None) is None:
            report.stop_reason = Reason("no_data", L("주간 사용량을 읽을 수 없음 (tokenalarm doctor 로 확인)",
                                                    "cannot read weekly usage (run tokenalarm doctor)"))
            return report
        deadline = now + max_seconds if max_seconds else None

        def on_snapshot(s: Snapshot) -> None:
            live.update(s)
            self.store.add_snapshot(Snapshot(s.observed_at, "autorun", s.windows))

        week = snap.get(SEVEN_DAY) if snap else None
        week_start = (week.resets_at - 7 * 86400) if week and week.resets_at else None
        tried: set[str] = set()
        started_at = now
        while True:
            reason = self.stop_reason(target, live, started_at, deadline)
            if reason:
                report.stop_reason = reason
                break
            task = select_task(self.tasks, self.store, self.now(), week_start, exclude=tried, only=only_task)
            if task is None:
                report.stop_reason = Reason("queue_empty", L("실행할 작업이 없음 (작업 큐 소진)", "nothing to run (task queue empty)"))
                break
            tried.add(task.id)
            if dry_run:
                b = self.chunk_budget(target, live)
                report.stop_reason = Reason("dry_run", L(f"[dry-run] 다음 작업: {task.id} (예산 ${b:.2f}/청크)",
                                                         f"[dry-run] next task: {task.id} (budget ${b:.2f}/chunk)"))
                break
            pct_now = live.week_pct(self.pct_per_usd())
            self.notifier.send(L(f"자동 실행 시작: {task.id}", f"Autorun started: {task.id}"),
                               L(f"{label} · 주간 {pct_now:.0f}% → 목표 {target:g}% · {task.kind}",
                                 f"{label} · weekly {pct_now:.0f}% → target {target:g}% · {task.kind}"))
            outcome = self.runner.run_task(
                task,
                stop_fn=lambda: self.stop_reason(target, live, started_at, deadline),
                budget_fn=lambda: self.chunk_budget(target, live),
                on_snapshot=on_snapshot,
                on_chunk_cost=live.add_cost,
                pct_fn=lambda: (live.windows.get(SEVEN_DAY).pct if live.windows.get(SEVEN_DAY) else None),
                target=target,
            )
            report.outcomes.append(outcome)
            # The last rate_limit_event came at the start of the last chunk: re-read real usage so the
            # run's Δ% (and the %/USD calibration) includes everything that was spent.
            fresh = self.usage.refresh(self.now(), allow_spend=True)
            if fresh is not None and fresh.get(SEVEN_DAY) is not None:
                live.update(fresh)
                outcome.pct_end = fresh.get(SEVEN_DAY).pct
                if outcome.run_id is not None:
                    self.store.update_run(outcome.run_id, pct_end=outcome.pct_end)
            self._recalibrate()
            self.notifier.send(L(f"자동 실행 {self._status_text(outcome.status)}: {task.id}",
                                 f"Autorun {self._status_text(outcome.status)}: {task.id}"), self._describe(outcome))
            if outcome.status == "blocked":
                report.stop_reason = outcome.reason
                break
            if only_task and outcome.status in ("done", "failed"):
                report.stop_reason = Reason("task_" + outcome.status, L(f"지정 작업 {self._status_text(outcome.status)}",
                                                                     f"selected task {self._status_text(outcome.status)}"))
                break
        report.pct_end = live.week_pct(None)
        return report

    def _recalibrate(self) -> None:
        v = calibrate_pct_per_usd(self.store.runs(limit=30))
        if v:
            self.store.set("pct_per_usd", v)

    @staticmethod
    def _status_text(status: str) -> str:
        ko = {"done": "완료", "partial": "중단(이어하기 가능)", "failed": "실패", "blocked": "차단"}
        en = {"done": "done", "partial": "paused (will resume)", "failed": "failed", "blocked": "blocked"}
        return L(ko.get(status, status), en.get(status, status))

    @staticmethod
    def _describe(o: RunOutcome) -> str:
        lines = []
        if o.pct_start is not None and o.pct_end is not None:
            d = o.pct_end - o.pct_start
            lines.append(L(f"주간 {o.pct_start:.0f}% → {o.pct_end:.0f}% (+{d:.1f}%)",
                           f"weekly {o.pct_start:.0f}% → {o.pct_end:.0f}% (+{d:.1f}%)"))
        lines.append(L(f"비용(추정) ${o.cost:.2f} · {o.turns}턴 · {o.chunks}청크",
                       f"est. cost ${o.cost:.2f} · {o.turns} turns · {o.chunks} chunks"))
        if o.reason:
            lines.append(L(f"사유: {o.reason}", f"reason: {o.reason}"))
        if o.branch:
            lines.append(L(f"브랜치: {o.branch}", f"branch: {o.branch}"))
        if o.workdir:
            lines.append(L(f"위치: {o.workdir}", f"location: {o.workdir}"))
        return "\n".join(lines)


def describe_report(r: FillReport) -> str:
    parts = [L(f"목표 {r.target:g}%", f"target {r.target:g}%")]
    if r.pct_start is not None:
        parts.append(L(f"시작 {r.pct_start:.0f}%", f"start {r.pct_start:.0f}%"))
    if r.pct_end is not None:
        parts.append(L(f"종료 {r.pct_end:.0f}%", f"end {r.pct_end:.0f}%"))
    parts.append(L(f"작업 {len(r.outcomes)}개, 비용(추정) ${r.cost:.2f}", f"{len(r.outcomes)} task(s), est. cost ${r.cost:.2f}"))
    if r.stop_reason:
        parts.append(L(f"종료 사유: {r.stop_reason}", f"stopped: {r.stop_reason}"))
    return " · ".join(parts)


__all__ = ["FillController", "FillReport", "LiveUsage", "describe_report", "format_duration"]
