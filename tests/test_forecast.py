from tokenalarm.forecast import calibrate_pct_per_usd, daily_usage, forecast_window
from tokenalarm.usage.base import Snapshot, Window

H = 3600.0
RESET = 1_800_000_000.0
START = RESET - 7 * 86400


def add(store, t, pct, resets=RESET):
    store.add_snapshot(Snapshot(t, "test", {"seven_day": Window("seven_day", pct, resets)}))


def test_linear_projection(store):
    # 2 days elapsed, 20 % used → 10 %/day → 5 days left → projected 70 %
    for i in range(0, 49, 6):
        add(store, START + i * H, i / 48 * 20)
    now = START + 48 * H
    f = forecast_window(store, Window("seven_day", 20, RESET), now)
    assert abs(f.avg_rate_h - 20 / 48) < 1e-6
    assert abs(f.projected_at_reset - 70) < 1e-6
    assert abs(f.projected_waste - 30) < 1e-6
    assert f.eta_100_avg is None  # would not hit 100 before reset
    assert abs(f.even_pace_pct - 48 / 168 * 100) < 1e-6


def test_eta_when_burning_fast(store):
    add(store, START, 0)
    add(store, START + 12 * H, 60)
    now = START + 12 * H
    f = forecast_window(store, Window("seven_day", 60, RESET), now)
    assert f.eta_100_avg is not None
    assert abs(f.eta_100_avg - (now + 8 * H)) < 1


def test_autorun_excluded(store):
    add(store, START, 0)
    add(store, START + 24 * H, 30)
    rid = store.start_run("t", START + 20 * H, 30, 10, None, None, None)
    store.update_run(rid, pct_end=30, cost_usd=4.0, status="done")
    f = forecast_window(store, Window("seven_day", 30, RESET), START + 24 * H)
    assert f.autorun_pct == 20
    assert abs(f.avg_rate_h - 10 / 24) < 1e-6


def test_daily_usage_handles_reset(store):
    add(store, START + 1 * H, 5)
    add(store, START + 5 * H, 9)
    add(store, RESET + 1 * H, 3, resets=RESET + 7 * 86400)
    days = dict(daily_usage(store, "seven_day", RESET + 2 * H, days=10))
    assert abs(sum(days.values()) - (4 + 3)) < 1e-6


def test_calibration(store):
    for cost, d in [(1.0, 2.0), (2.0, 4.2), (0.01, 9)]:
        rid = store.start_run("t", START, 50, 10, None, None, None)
        store.update_run(rid, pct_end=10 + d, cost_usd=cost)
    v = calibrate_pct_per_usd(store.runs())
    assert 2.0 <= v <= 2.1
