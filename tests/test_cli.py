import json
import sys
import time
from pathlib import Path

from tokenalarm import cli, paths
from tokenalarm.config import load_config
from tokenalarm.tasks import load_tasks

FAKE = str(Path(__file__).with_name("fake_claude.py"))


def test_init_creates_examples_and_they_load(capsys):
    assert cli.main(["init"]) == 0
    cfg = load_config(paths.config_file())
    assert cfg.weekly_fill_rules[0].fill_to == 30
    tasks = load_tasks(paths.tasks_file(), cfg)
    assert {t.id for t in tasks} >= {"weekly-ai-news", "myrepo-tests"}
    assert cli.main(["init"]) == 0  # idempotent


def test_status_and_run_with_fake_claude(tmp_path, monkeypatch, capsys):
    cli.main(["init"])
    usage = tmp_path / "usage.json"
    usage.write_text(json.dumps({"seven_day": 18.0, "resets_at": time.time() + 60 * 3600}))
    state = tmp_path / "fake"
    state.mkdir()
    monkeypatch.setenv("FAKE_USAGE_FILE", str(usage))
    monkeypatch.setenv("FAKE_STATE_DIR", str(state))
    monkeypatch.setenv("FAKE_USAGE_TEXT", "1")  # fake /usage prints "Current week (all models) NN% used"
    monkeypatch.setenv("FAKE_TASK_TURNS", "2")
    cfg_text = paths.config_file().read_text(encoding="utf-8").replace(
        "claude_cmd: null", "claude_cmd: " + json.dumps([sys.executable, FAKE]))
    cfg_text = cfg_text.replace("desktop: true", "desktop: false")
    paths.config_file().write_text(cfg_text, encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["status"]) == 0  # free /usage probe: % known, reset time not yet
    out = capsys.readouterr().out
    assert "주간 18%" in out and "리셋 시각" in out
    assert cli.main(["status", "--refresh"]) == 0  # allows the tiny ping → rate_limit_event has resetsAt
    out = capsys.readouterr().out
    assert "30%까지 채우기" in out, out
    assert cli.main(["tasks"]) == 0
    assert "weekly-ai-news" in capsys.readouterr().out
    assert cli.main(["run", "--until", "30", "--task", "weekly-ai-news"]) == 0
    out = capsys.readouterr().out
    assert "weekly-ai-news: done" in out
    assert cli.main(["history"]) == 0
    assert "weekly-ai-news" in capsys.readouterr().out
    assert cli.main(["daemon", "--dry-run"]) == 0


def test_pause_resume(capsys):
    assert cli.main(["pause", "--for", "2h"]) == 0
    assert cli.main(["resume"]) == 0
    assert cli.main(["pause"]) == 0
    assert paths.pause_file().exists()
    assert cli.main(["resume"]) == 0
    assert not paths.pause_file().exists()
