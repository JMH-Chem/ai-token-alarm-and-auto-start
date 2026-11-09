import pytest

from tokenalarm.config import format_duration, load_config, parse_duration


def test_parse_duration():
    assert parse_duration("72h") == 72 * 3600
    assert parse_duration("1d12h") == 36 * 3600
    assert parse_duration("45m") == 2700
    assert parse_duration(90) == 90
    assert parse_duration("30") == 30
    with pytest.raises(ValueError):
        parse_duration("abc")
    with pytest.raises(ValueError):
        parse_duration("3x")


def test_format_duration():
    assert format_duration(3 * 86400 + 4 * 3600) == "3d4h"
    assert format_duration(2 * 3600 + 13 * 60) == "2h13m"
    assert format_duration(45 * 60) == "45m"
    assert format_duration(None) == "?"


def test_load_config(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "﻿weekly_fill_rules:\n"
        "  - {before_reset: 3d, if_below: 20, fill_to: 30}\n"
        "guards: {weekly_hard_cap: 90, pre_run_warning: 5m}\n"
        "notify:\n  ntfy: {topic: abc}\n  webhooks: [{type: discord, url: 'https://x'}]\n"
        "runner: {claude_cmd: claude}\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.weekly_fill_rules[0].before_reset == 3 * 86400
    assert cfg.guards.weekly_hard_cap == 90
    assert cfg.guards.pre_run_warning == 300
    assert cfg.notify.ntfy.topic == "abc"
    assert cfg.notify.webhooks[0].type == "discord"
    assert cfg.runner.claude_cmd == ["claude"]


def test_unknown_key_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("guards: {nope: 1}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(p)


def test_bad_rule_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("weekly_fill_rules:\n  - {before_reset: 3d, if_below: 40, fill_to: 30}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(p)
