from tokenalarm.config import DEFAULT_RULES, Guards
from tokenalarm.policy import active_rule, decide, in_quiet_hours, next_checkpoint, run_guard
from tokenalarm.usage.base import Snapshot, Window

H = 3600.0
NOW = 1_800_000_000.0


def snap(pct, hours_left, five=None):
    w = {"seven_day": Window("seven_day", pct, NOW + hours_left * H)}
    if five is not None:
        w["five_hour"] = Window("five_hour", five, NOW + 2 * H)
    return Snapshot(NOW, "test", w)


def test_active_rule_selection():
    assert active_rule(DEFAULT_RULES, 100 * H) is None
    assert active_rule(DEFAULT_RULES, 72 * H).fill_to == 30
    assert active_rule(DEFAULT_RULES, 60 * H).fill_to == 30
    assert active_rule(DEFAULT_RULES, 47 * H).fill_to == 50
    assert active_rule(DEFAULT_RULES, 23 * H).fill_to == 70
    assert active_rule(DEFAULT_RULES, 6 * H).fill_to == 90
    assert abs(next_checkpoint(DEFAULT_RULES, 100 * H) - 28 * H) < 1


# The user's own examples: 3 days before reset and < 20 % → fill to 30 %, 2 days before and < 40 % → 50 %, ...
def test_user_example_three_days():
    d = decide(snap(18, 70), DEFAULT_RULES, Guards(), NOW)
    assert d.target == 30 and d.wants_fill


def test_three_days_enough_usage():
    d = decide(snap(25, 70), DEFAULT_RULES, Guards(), NOW)
    assert d.target is None and not d.wants_fill


def test_two_days():
    d = decide(snap(35, 47), DEFAULT_RULES, Guards(), NOW)
    assert d.target == 50


def test_one_day_and_twelve_hours():
    assert decide(snap(55, 20), DEFAULT_RULES, Guards(), NOW).target == 70
    assert decide(snap(79, 11), DEFAULT_RULES, Guards(), NOW).target == 90


def test_too_early():
    d = decide(snap(5, 120), DEFAULT_RULES, Guards(), NOW)
    assert d.target is None
    assert "체크포인트" in d.reason


def test_sticky_target_survives_passing_if_below():
    resets = NOW + 70 * H
    d = decide(snap(24, 70), DEFAULT_RULES, Guards(), NOW, sticky={"resets_at": resets, "target": 30})
    assert d.target == 30 and d.wants_fill


def test_sticky_ignored_for_other_window():
    d = decide(snap(24, 70), DEFAULT_RULES, Guards(), NOW, sticky={"resets_at": NOW - 100 * H, "target": 30})
    assert d.target is None


def test_hard_cap():
    g = Guards(weekly_hard_cap=85)
    assert decide(snap(79, 11), DEFAULT_RULES, g, NOW).target == 85


def test_no_data():
    assert decide(None, DEFAULT_RULES, Guards(), NOW).target is None


def test_run_guard():
    g = Guards(five_hour_ceiling=85, weekly_hard_cap=95, stop_margin=2)
    assert run_guard(snap(18, 70, five=10), g, 30) is None
    assert "5시간" in run_guard(snap(18, 70, five=90), g, 30)
    assert "목표" in run_guard(snap(28.5, 70), g, 30)
    assert "하드캡" in run_guard(snap(96, 70), g, None)


def test_quiet_hours():
    import datetime as dt
    t = dt.datetime(2026, 11, 9, 10, 0).timestamp()
    assert in_quiet_hours("09:00-18:00", t)
    assert not in_quiet_hours("18:00-23:00", t)
    late = dt.datetime(2026, 11, 9, 2, 0).timestamp()
    assert in_quiet_hours("23:00-07:00", late)
    assert not in_quiet_hours(None, t)
