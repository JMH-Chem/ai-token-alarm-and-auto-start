"""Choose when and how to (re)read usage, cheapest and most official source first."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from ..config import Config
from ..store import Store
from .base import SEVEN_DAY, Snapshot

log = logging.getLogger(__name__)

ProbeFn = Callable[[], "Snapshot | None"]


@dataclass
class Source:
    name: str
    fn: ProbeFn
    spends_quota: bool = False  # ping starts a 5h window / costs a tiny amount
    min_interval: float = 0.0  # seconds between calls


class UsageService:
    def __init__(self, cfg: Config, store: Store, sources: list[Source]):
        self.cfg = cfg
        self.store = store
        self.sources = sources

    @classmethod
    def default(cls, cfg: Config, store: Store, claude_cmd: Callable[[], list[str]]) -> "UsageService":
        from . import oauth, probe

        u = cfg.usage
        sources: list[Source] = []
        if u.probe_usage_command:
            sources.append(Source("usage_command", lambda: probe.probe_usage_command(
                claude_cmd(), u.rate_limit_scale)))
        if u.oauth_endpoint:
            sources.append(Source("oauth", oauth.fetch_oauth_usage, min_interval=u.oauth_min_interval_minutes * 60))
        if u.allow_ping_probe:
            sources.append(Source("ping", lambda: probe.probe_ping(
                claude_cmd(), u.ping_model, u.rate_limit_scale,
                allowed_key_sources=cfg.runner.allowed_api_key_sources), spends_quota=True))
        return cls(cfg, store, sources)

    # --------------------------------------------------------------------------------------------
    def current(self, now: float | None = None) -> Snapshot | None:
        return self.store.latest_snapshot(time.time() if now is None else now)

    def age(self, now: float) -> float | None:
        got = self.store.latest_window(SEVEN_DAY, now)
        return None if got is None else now - got[1]

    def is_fresh(self, now: float) -> bool:
        a = self.age(now)
        return a is not None and a <= self.cfg.usage.statusline_fresh_minutes * 60

    def poll_interval(self, near_checkpoint: bool) -> float:
        u = self.cfg.usage
        return (u.near_checkpoint_poll_minutes if near_checkpoint else u.idle_poll_minutes) * 60

    def refresh(self, now: float, allow_spend: bool = False, force: bool = False) -> Snapshot | None:
        """Try sources in order until one returns complete data (weekly % *and* reset time).

        ``allow_spend`` enables the ping probe. Partial data (e.g. /usage text without a reset time) is
        stored but the next source is still tried.
        """
        best: Snapshot | None = None
        for src in self.sources:
            if src.spends_quota and not allow_spend:
                continue
            if not force and self._in_backoff(src.name, now):
                continue
            last = self.store.get(f"probe_last:{src.name}", 0) or 0
            if not force and src.min_interval and now - last < src.min_interval:
                continue
            self.store.set(f"probe_last:{src.name}", now)
            try:
                snap = src.fn()
            except Exception as e:  # never let a probe crash the daemon
                log.warning("usage source %s failed: %s", src.name, e)
                snap = None
            if snap and not snap.is_empty():
                self.store.set(f"probe_fail:{src.name}", None)
                self.store.add_snapshot(snap)  # may inherit a known reset time
                week = snap.windows.get(SEVEN_DAY)
                if week is not None and week.resets_at is not None:
                    return snap
                best = best or snap
                continue
            self._fail(src.name, now)
        return best

    def maybe_refresh(self, now: float, near_checkpoint: bool, about_to_fill: bool = False) -> Snapshot | None:
        """Adaptive polling used by the daemon while not filling.

        ``about_to_fill``: the daemon would start an autorun now (rule + idle + guards OK); a ping is then
        acceptable because quota is going to be spent anyway.
        """
        if self.is_fresh(now):
            return self.current(now)
        last_any = self.store.get("refresh_last", 0) or 0
        no_data = self.age(now) is None
        if no_data or now - last_any >= self.poll_interval(near_checkpoint or about_to_fill):
            self.store.set("refresh_last", now)
            # Spending a ping is OK only when we are about to spend anyway, or to bootstrap once.
            allow_spend = about_to_fill or (no_data and not self.store.get("bootstrapped"))
            snap = self.refresh(now, allow_spend=allow_spend)
            if snap is not None:
                self.store.set("bootstrapped", True)
        return self.current(now)

    # --------------------------------------------------------------------------------------------
    def _in_backoff(self, name: str, now: float) -> bool:
        info = self.store.get(f"probe_fail:{name}")
        return bool(info) and now < float(info.get("until", 0))

    def _fail(self, name: str, now: float) -> None:
        info = self.store.get(f"probe_fail:{name}") or {"count": 0}
        count = int(info.get("count", 0)) + 1
        delay = min(6 * 3600, 600 * (2 ** (count - 1)))
        self.store.set(f"probe_fail:{name}", {"count": count, "until": now + delay})
