"""Usage data model shared by every source."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

FIVE_HOUR = "five_hour"
SEVEN_DAY = "seven_day"
WINDOW_SECONDS = {FIVE_HOUR: 5 * 3600, SEVEN_DAY: 7 * 86400}


@dataclass
class Window:
    kind: str  # five_hour | seven_day | seven_day:<scope> (e.g. seven_day:fable)
    pct: float  # 0-100
    resets_at: float | None = None  # epoch seconds

    def seconds_left(self, now: float) -> float | None:
        return None if self.resets_at is None else max(0.0, self.resets_at - now)

    def length(self) -> float:
        base = self.kind.split(":", 1)[0]
        return float(WINDOW_SECONDS.get(base, 7 * 86400))


@dataclass
class Snapshot:
    observed_at: float
    source: str  # statusline | usage_command | ping | autorun | oauth | test
    windows: dict[str, Window] = field(default_factory=dict)

    def get(self, kind: str) -> Window | None:
        w = self.windows.get(kind)
        if w is None or w.resets_at is None:
            return w
        # A window whose reset time has passed is no longer valid (the provider drops it too).
        if w.resets_at <= self.observed_at:
            return None
        return w

    def is_empty(self) -> bool:
        return not self.windows

    @staticmethod
    def now(source: str, windows: list[Window]) -> "Snapshot":
        return Snapshot(time.time(), source, {w.kind: w for w in windows})
