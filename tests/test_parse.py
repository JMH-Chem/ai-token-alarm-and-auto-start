from tokenalarm.usage.parse import (
    parse_oauth_usage,
    parse_rate_limit_event,
    parse_statusline,
    parse_usage_report,
    parse_usage_text,
    snapshot_from_stream,
)

NOW = 1_794_000_000.0


def test_statusline():
    payload = {"rate_limits": {"five_hour": {"used_percentage": 42, "resets_at": NOW + 3600},
                               "seven_day": {"used_percentage": 18.5, "resets_at": NOW + 3 * 86400}}}
    s = parse_statusline(payload, NOW)
    assert s.get("five_hour").pct == 42
    assert s.get("seven_day").pct == 18.5
    assert s.get("seven_day").resets_at == NOW + 3 * 86400


def test_statusline_missing():
    assert parse_statusline({"model": {}}, NOW) is None


def test_rate_limit_event_fraction():
    # Real payload shape captured in the spike (docs/spike-notes.md)
    ev = {"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "resetsAt": 1794171600, "rateLimitType": "seven_day",
        "unifiedWindows": {"seven_day": {"utilization": 0.27, "resetsAt": 1794171600, "status": "allowed"}}}}
    s = parse_rate_limit_event(ev, NOW)
    w = s.get("seven_day")
    assert abs(w.pct - 27.0) < 1e-9
    assert w.resets_at == 1794171600


def test_rate_limit_event_percent_scale_and_five_hour():
    ev = {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
        "five_hour": {"utilization": 55, "resetsAt": NOW + 100},
        "seven_day_fable": {"utilization": 10, "resetsAt": NOW + 1000}}}}
    s = parse_rate_limit_event(ev, NOW, scale="percent")
    assert s.get("five_hour").pct == 55
    assert s.get("seven_day:fable").pct == 10


def test_usage_report():
    report = {"rate_limits": {"limits": [
        {"kind": "session", "percent": 12, "resets_at": "2026-11-08T22:00:00Z"},
        {"kind": "weekly_all", "percent": 33, "resets_at": "2026-11-11T09:00:00+00:00"},
        {"kind": "weekly_scoped", "percent": 5, "scope": {"model": {"display_name": "Fable"}}},
    ]}}
    s = parse_usage_report(report, NOW)
    assert s.windows["five_hour"].pct == 12
    assert s.windows["seven_day"].pct == 33
    assert s.windows["seven_day:fable"].pct == 5


def test_usage_text():
    text = ("Current session\n  ███▌  42% used\n  Resets 3pm\n"
            "Current week (all models)\n  █  18% used\nCurrent week (Fable)\n 3% used")
    s = parse_usage_text(text, NOW)
    assert s.windows["five_hour"].pct == 42
    assert s.windows["seven_day"].pct == 18
    assert s.windows["seven_day:fable"].pct == 3


def test_stream_without_plan_data_returns_none():
    events = [{"type": "system", "subtype": "init", "apiKeySource": "none"},
              {"type": "result", "subtype": "success", "result": "Total cost: $0.0000\nUsage: 0 input"}]
    assert snapshot_from_stream(events, NOW) is None


def test_oauth():
    data = {"five_hour": {"utilization": 7.0, "resets_at": "2026-11-08T22:00:00Z"},
            "seven_day": {"utilization": 21.0, "resets_at": "2026-11-11T09:00:00Z"},
            "seven_day_opus": None, "extra_usage": {"is_enabled": False}}
    s = parse_oauth_usage(data, NOW)
    assert s.windows["seven_day"].pct == 21.0
    assert s.windows["five_hour"].pct == 7.0
