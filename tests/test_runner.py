import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tokenalarm.config import Config
from helpers import read_usage
from tokenalarm.fill import FillController
from tokenalarm.notify import Notifier
from tokenalarm.runner import Runner
from tokenalarm.tasks import Task
from tokenalarm.usage.base import Snapshot, Window
from tokenalarm.usage.chain import Source, UsageService

FAKE = str(Path(__file__).with_name("fake_claude.py"))
RESET = time.time() + 70 * 3600


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    usage = tmp_path / "usage.json"
    usage.write_text(json.dumps({"seven_day": 18.0, "resets_at": RESET}))
    state = tmp_path / "fake-state"
    state.mkdir()
    monkeypatch.setenv("FAKE_USAGE_FILE", str(usage))
    monkeypatch.setenv("FAKE_STATE_DIR", str(state))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-removed")
    return usage, state


def make_cfg(**runner):
    cfg = Config()
    cfg.runner.claude_cmd = [sys.executable, FAKE]
    cfg.runner.kill_grace_seconds = 3
    for k, v in runner.items():
        setattr(cfg.runner, k, v)
    cfg.guards.stop_margin = 2
    return cfg


def research_task(tmp_path, **kw):
    return Task(id=kw.pop("id", "r1"), kind="research", prompt="조사해줘", output_dir=str(tmp_path / "out"), **kw)


def invocations(state):
    return [json.loads(line) for line in (state / "invocations.jsonl").read_text().splitlines()]


def test_task_completes_and_env_is_safe(store, tmp_path, fake_env, monkeypatch):
    usage, state = fake_env
    monkeypatch.setenv("FAKE_TASK_TURNS", "3")
    cfg = make_cfg()
    runner = Runner(cfg, store, cfg.runner.claude_cmd)
    out = runner.run_task(research_task(tmp_path), stop_fn=lambda: None, budget_fn=lambda: 1.0)
    assert out.status == "done", out.reason
    assert out.turns == 3 and out.chunks == 1
    inv = invocations(state)[0]
    assert inv["autorun"] == "1"
    assert inv["api_key"] is None  # billing env var stripped
    argv = inv["argv"]
    assert "--input-format" in argv and "--max-budget-usd" in argv and "--allowedTools" in argv
    assert Path(out.workdir).is_dir() and str(tmp_path / "out") in out.workdir
    assert store.task_state("r1")["last_status"] == "done"


def test_chunks_resume_same_session(store, tmp_path, fake_env, monkeypatch):
    usage, state = fake_env
    monkeypatch.setenv("FAKE_TASK_TURNS", "5")
    cfg = make_cfg(chunk_turns=2)
    runner = Runner(cfg, store, cfg.runner.claude_cmd)
    out = runner.run_task(research_task(tmp_path), stop_fn=lambda: None, budget_fn=lambda: 1.0)
    assert out.status == "done"
    assert out.chunks == 3
    inv = invocations(state)
    assert "--resume" not in inv[0]["argv"]
    sid = inv[1]["argv"][inv[1]["argv"].index("--resume") + 1]
    assert sid == out.session_id
    assert all(i["cwd"] == inv[0]["cwd"] for i in inv)


def test_interrupt_then_resume_next_time(store, tmp_path, fake_env, monkeypatch):
    usage, state = fake_env
    monkeypatch.setenv("FAKE_TURN_SECONDS", "0.1")
    cfg = make_cfg(chunk_turns=1000)
    runner = Runner(cfg, store, cfg.runner.claude_cmd)
    t0 = time.time()
    out = runner.run_task(research_task(tmp_path), stop_fn=lambda: "테스트 중단" if time.time() - t0 > 1 else None,
                          budget_fn=lambda: 100.0)
    assert out.status == "partial" and out.reason == "테스트 중단"
    st = store.task_state("r1")
    assert st["last_status"] == "partial" and st["session_id"] == out.session_id
    monkeypatch.setenv("FAKE_TASK_TURNS", "1")
    out2 = runner.run_task(research_task(tmp_path), stop_fn=lambda: None, budget_fn=lambda: 1.0)
    assert out2.workdir == out.workdir
    argv = invocations(state)[-1]["argv"]
    assert argv[argv.index("--resume") + 1] == out.session_id


def test_api_key_source_blocks(store, tmp_path, fake_env, monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY_SOURCE", "ANTHROPIC_API_KEY")
    cfg = make_cfg()
    runner = Runner(cfg, store, cfg.runner.claude_cmd)
    out = runner.run_task(research_task(tmp_path), stop_fn=lambda: None, budget_fn=lambda: 1.0)
    assert out.status == "blocked"
    assert "API" in out.reason


def test_kill_when_interrupt_ignored(store, tmp_path, fake_env, monkeypatch):
    monkeypatch.setenv("FAKE_IGNORE_INTERRUPT", "1")
    monkeypatch.setenv("FAKE_TURN_SECONDS", "0.05")
    cfg = make_cfg(chunk_turns=100000, kill_grace_seconds=1)
    runner = Runner(cfg, store, cfg.runner.claude_cmd)
    t0 = time.time()
    out = runner.run_task(research_task(tmp_path), stop_fn=lambda: "stop" if time.time() - t0 > 0.5 else None,
                          budget_fn=lambda: 1000.0)
    assert out.status == "partial"
    assert time.time() - t0 < 15


def test_repo_task_uses_worktree(store, tmp_path, fake_env, monkeypatch):
    monkeypatch.setenv("FAKE_TASK_TURNS", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"],
                   check=True)
    cfg = make_cfg()
    runner = Runner(cfg, store, cfg.runner.claude_cmd)
    task = Task(id="repo1", kind="repo", prompt="테스트 추가", repo=str(repo), allowed_tools=["Read"],
                disallowed_tools=["Bash(git push:*)"])
    out = runner.run_task(task, stop_fn=lambda: None, budget_fn=lambda: 1.0)
    assert out.status == "done"
    assert out.branch.startswith("tokenalarm/repo1/")
    branches = subprocess.run([*git, "branch", "--list", "tokenalarm/*"], capture_output=True, text=True).stdout
    assert out.branch in branches
    assert Path(out.workdir).is_dir() and Path(out.workdir) != repo


def file_source(usage_path):
    def read():
        u = read_usage(usage_path)
        return Snapshot(time.time(), "file", {"seven_day": Window("seven_day", u["seven_day"], u["resets_at"])})
    return Source("file", read)


@pytest.mark.parametrize("lag,every_turn", [(1, "1"), (3, "1"), (0, "0")])
def test_fill_stops_near_target(store, tmp_path, fake_env, monkeypatch, lag, every_turn):
    """18 % → target 30 %, with rounded + lagging reports. Must stop at >= 28 and not overshoot much."""
    usage, state = fake_env
    monkeypatch.setenv("FAKE_PCT_PER_TURN", "0.4")
    monkeypatch.setenv("FAKE_COST_PER_TURN", "0.05")
    monkeypatch.setenv("FAKE_REPORT_LAG_TURNS", str(lag))
    monkeypatch.setenv("FAKE_EVENT_EVERY_TURN", every_turn)
    cfg = make_cfg(chunk_turns=6, chunk_budget_usd_default=0.3, max_chunks_per_task=50)
    cfg.usage.statusline_fresh_minutes = 0
    usage_svc = UsageService(cfg, store, [file_source(usage)])
    runner = Runner(cfg, store, cfg.runner.claude_cmd)
    tasks = [research_task(tmp_path, id="a", repeat="always", priority=1)]
    fc = FillController(cfg, store, usage_svc, runner, tasks, Notifier(cfg.notify, store, enabled=False))
    report = fc.fill(30)
    real = read_usage(usage)["seven_day"]
    assert report.outcomes, report.stop_reason
    assert "목표" in (report.stop_reason or ""), report.stop_reason
    assert real >= 28 - 0.5, real
    # overshoot bounded by roughly one chunk (6 turns × 0.4 %) plus reporting lag
    assert real <= 30 + 6 * 0.4 + lag * 0.4 + 0.5, real
    assert store.get("pct_per_usd")  # calibrated from the runs


def test_fill_dry_run_does_not_execute(store, tmp_path, fake_env):
    usage, state = fake_env
    cfg = make_cfg()
    usage_svc = UsageService(cfg, store, [file_source(usage)])
    fc = FillController(cfg, store, usage_svc, Runner(cfg, store, cfg.runner.claude_cmd),
                        [research_task(tmp_path)], Notifier(cfg.notify, store, enabled=False))
    report = fc.fill(30, dry_run=True)
    assert "dry-run" in report.stop_reason
    assert not (state / "invocations.jsonl").exists()


def test_fill_respects_pause_file(store, tmp_path, fake_env):
    from tokenalarm import paths

    usage, state = fake_env
    paths.pause_file().write_text("x")
    cfg = make_cfg()
    fc = FillController(cfg, store, UsageService(cfg, store, [file_source(usage)]),
                        Runner(cfg, store, cfg.runner.claude_cmd), [research_task(tmp_path)],
                        Notifier(cfg.notify, store, enabled=False))
    report = fc.fill(30)
    assert "일시정지" in report.stop_reason and not report.outcomes
