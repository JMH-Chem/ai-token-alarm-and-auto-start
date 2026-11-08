"""The fill "ladder": decide how far to fill the weekly window before it resets.

Rule semantics (config ``weekly_fill_rules``):

* A rule applies once the time left until the weekly reset is <= ``before_reset``.
  When several apply, the one with the smallest ``before_reset`` (the latest checkpoint) wins.
* If usage is below ``if_below`` at that point, the target becomes ``fill_to``.
* Targets are *sticky* within one weekly window: once a fill to 30 % was triggered (because usage was
  18 % < 20 %), it stays 30 % even after usage passes 20 %, until it is reached. A later checkpoint can
  only raise the target.
* The target never exceeds ``guards.weekly_hard_cap``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .config import FillRule, Guards, format_duration
from .usage.base import FIVE_HOUR, SEVEN_DAY, Snapshot


@dataclass
class Decision:
    target: float | None
    pct: float | None
    seconds_left: float | None
    rule: FillRule | None
    reason: str
    resets_at: float | None = None
    margin: float = 0.0  # guards.stop_margin: filling stops at target - margin

    @property
    def wants_fill(self) -> bool:
        return self.target is not None and self.pct is not None and self.pct < self.target - self.margin


def active_rule(rules: list[FillRule], seconds_left: float) -> FillRule | None:
    applicable = [r for r in rules if seconds_left <= r.before_reset]
    return min(applicable, key=lambda r: r.before_reset) if applicable else None


def next_checkpoint(rules: list[FillRule], seconds_left: float) -> float | None:
    """Seconds until the next rule starts to apply (None if none left)."""
    upcoming = [seconds_left - r.before_reset for r in rules if r.before_reset < seconds_left]
    return min(upcoming) if upcoming else None


def decide(snapshot: Snapshot | None, rules: list[FillRule], guards: Guards, now: float,
           sticky: dict | None = None) -> Decision:
    """Compute the fill target. ``sticky`` is ``{"resets_at": float, "target": float}`` from earlier."""
    week = snapshot.get(SEVEN_DAY) if snapshot else None
    if week is None:
        return Decision(None, None, None, None, "주간 사용량 정보 없음")
    if week.resets_at is None:
        return Decision(None, week.pct, None, None, "주간 리셋 시각을 모름", None)
    left = week.resets_at - now
    pct = week.pct
    rule = active_rule(rules, left)

    sticky_target = None
    if sticky and sticky.get("resets_at") and abs(float(sticky["resets_at"]) - week.resets_at) < 3600:
        sticky_target = float(sticky["target"])

    new_target = None
    if rule is not None and pct < rule.if_below:
        new_target = rule.fill_to

    target = max(t for t in (sticky_target, new_target, -1.0) if t is not None)
    if target < 0:
        target = None
    if target is not None:
        target = min(target, guards.weekly_hard_cap)

    when = f"리셋 {format_duration(left)} 전"
    if target is None:
        if rule is None:
            nxt = next_checkpoint(rules, left)
            extra = f", 다음 체크포인트까지 {format_duration(nxt)}" if nxt is not None else ""
            reason = f"{when}, {pct:.0f}% — 아직 적용되는 규칙 없음{extra}"
        else:
            reason = f"{when}, {pct:.0f}% ≥ {rule.if_below:g}% — 충분히 사용 중 (자동 실행 불필요)"
    elif pct >= target - guards.stop_margin:
        reason = f"{when}, {pct:.0f}% — 목표 {target:g}% 도달"
    else:
        basis = rule.label() if new_target is not None and rule else f"이전에 정한 목표 {sticky_target:g}% 유지"
        reason = f"{when}, {pct:.0f}% → {target:g}%까지 채우기 ({basis})"
    return Decision(target, pct, left, rule, reason, week.resets_at, guards.stop_margin)


def run_guard(snapshot: Snapshot | None, guards: Guards, target: float | None) -> str | None:
    """Return a reason to *not* spend now (None = OK). Pure usage-based guards."""
    if snapshot is None:
        return "사용량 정보 없음"
    week = snapshot.get(SEVEN_DAY)
    if week is not None and week.pct >= guards.weekly_hard_cap:
        return f"주간 하드캡 {guards.weekly_hard_cap:g}% 도달"
    if target is not None and week is not None and week.pct >= target - guards.stop_margin:
        return f"목표 {target:g}% 도달 (여유 {guards.stop_margin:g}%)"
    five = snapshot.get(FIVE_HOUR)
    if five is not None and five.pct >= guards.five_hour_ceiling:
        return f"5시간 창 {five.pct:.0f}% ≥ 상한 {guards.five_hour_ceiling:g}% — 5시간 리셋 대기"
    for kind, w in snapshot.windows.items():
        if kind.startswith(SEVEN_DAY + ":") and w.pct >= guards.weekly_hard_cap:
            return f"{kind} 한도 {w.pct:.0f}% — 하드캡 도달"
    return None


def in_quiet_hours(spec: str | None, now: float) -> bool:
    """``spec`` like "09:00-18:00" (local time); ranges may wrap midnight ("23:00-07:00")."""
    if not spec:
        return False
    try:
        start_s, end_s = spec.split("-")
        sh, sm = (int(x) for x in start_s.strip().split(":"))
        eh, em = (int(x) for x in end_s.strip().split(":"))
    except ValueError:
        return False
    t = datetime.fromtimestamp(now)
    cur = t.hour * 60 + t.minute
    start, end = sh * 60 + sm, eh * 60 + em
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end
