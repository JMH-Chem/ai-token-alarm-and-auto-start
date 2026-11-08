"""Linear pace forecasting (v1).

* average rate  = usage consumed by *you* (autorun excluded) since the window started / hours elapsed
* recent rate   = same, over the last few hours
* projected     = current % + average rate × hours left       (capped at 100)
* waste         = 100 − projected
* ETA to 100 %  = when the limit would be hit at the recent (or average) rate
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .store import RunRow, Store
from .usage.base import WINDOW_SECONDS, Window


@dataclass
class WindowForecast:
    kind: str
    pct: float
    resets_at: float | None
    seconds_left: float | None
    elapsed: float | None
    autorun_pct: float
    avg_rate_h: float | None  # %/hour, your own usage
    recent_rate_h: float | None
    projected_at_reset: float | None
    projected_waste: float | None
    even_pace_pct: float | None
    eta_100_recent: float | None  # epoch
    eta_100_avg: float | None


def autorun_consumed(runs: Iterable[RunRow], since: float) -> float:
    total = 0.0
    for r in runs:
        if r.started_at >= since and r.pct_start is not None and r.pct_end is not None:
            total += max(0.0, r.pct_end - r.pct_start)
    return total


def forecast_window(store: Store, window: Window, now: float, recent_hours: float = 6.0) -> WindowForecast:
    base = window.kind.split(":", 1)[0]
    length = float(WINDOW_SECONDS.get(base, 7 * 86400))
    pct = window.pct
    if window.resets_at is None:
        return WindowForecast(window.kind, pct, None, None, None, 0.0, None, None, None, None, None, None, None)
    start = window.resets_at - length
    left = max(0.0, window.resets_at - now)
    elapsed = max(0.0, now - start)
    runs = store.runs(limit=500, since=start)
    auto = autorun_consumed(runs, start)
    own = max(0.0, pct - auto)

    avg_rate = own / (elapsed / 3600) if elapsed >= 1800 else None

    recent_rate = None
    since = max(start, now - recent_hours * 3600)
    series = [s for s in store.window_series(window.kind, since, now)
              if s[2] is None or abs(s[2] - window.resets_at) < 3600]
    if len(series) >= 2 and series[-1][0] - series[0][0] >= 1800:
        span_h = (series[-1][0] - series[0][0]) / 3600
        delta = series[-1][1] - series[0][1] - autorun_consumed(runs, series[0][0])
        recent_rate = max(0.0, delta) / span_h

    projected = None
    if avg_rate is not None:
        projected = min(100.0, pct + avg_rate * left / 3600)
    elif elapsed > 0:
        projected = pct  # too early to extrapolate
    waste = None if projected is None else max(0.0, 100.0 - projected)

    def eta(rate: float | None) -> float | None:
        if not rate or rate <= 0 or pct >= 100:
            return None
        t = now + (100.0 - pct) / rate * 3600
        return t if t < window.resets_at else None

    return WindowForecast(
        kind=window.kind,
        pct=pct,
        resets_at=window.resets_at,
        seconds_left=left,
        elapsed=elapsed,
        autorun_pct=auto,
        avg_rate_h=avg_rate,
        recent_rate_h=recent_rate,
        projected_at_reset=projected,
        projected_waste=waste,
        even_pace_pct=min(100.0, elapsed / length * 100.0),
        eta_100_recent=eta(recent_rate),
        eta_100_avg=eta(avg_rate),
    )


def daily_usage(store: Store, kind: str, now: float, days: int = 14) -> list[tuple[str, float]]:
    """Percentage points consumed per local calendar day (handles resets)."""
    series = store.window_series(kind, now - days * 86400, now)
    buckets: dict[str, float] = {}
    prev = None
    for ts, pct, resets_at in series:
        day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        buckets.setdefault(day, 0.0)
        if prev is not None:
            p_ts, p_pct, p_reset = prev
            same_window = (p_reset is None or resets_at is None) or abs(p_reset - resets_at) < 3600
            if same_window:
                buckets[day] += max(0.0, pct - p_pct)
            else:
                buckets[day] += max(0.0, pct)  # new window started from 0
        prev = (ts, pct, resets_at)
    return sorted(buckets.items())


def calibrate_pct_per_usd(runs: Iterable[RunRow], min_cost: float = 0.05, sample: int = 10) -> float | None:
    """Median weekly-% per (client-side estimated) USD over recent autoruns."""
    ratios = []
    for r in runs:
        if r.cost_usd and r.cost_usd >= min_cost and r.pct_start is not None and r.pct_end is not None:
            d = r.pct_end - r.pct_start
            if d > 0:
                ratios.append(d / r.cost_usd)
        if len(ratios) >= sample:
            break
    return statistics.median(ratios) if ratios else None
