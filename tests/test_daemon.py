import json
import sys
import time
from pathlib import Path

import pytest

from tokenalarm import paths, spool
from tokenalarm.activity import Activity
from tokenalarm.config import Config
from helpers import read_usage
from tokenalarm.daemon import Daemon, SingleInstance
from tokenalarm.ingest import ingest_spool
from tokenalarm.fill import FillController
from tokenalarm.notify import Notifier
from tokenalarm.runner import Runner
from tokenalarm.tasks import Task
from tokenalarm.usage.base import Snapshot, Window
from tokenalarm.usage.chain import Source, UsageService

FAKE = str(Path(__file__).with_name("fake_claude.py"))
H = 3600.0


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def world(tmp_path, monkeypatch, store):
    usage = tmp_path / "usage.json"
    reset = time.time() + 70 * H  # 2d22h before the weekly reset → the 72h rule applies
    usage.write_text(json.dumps({"seven_day": 18.0, "resets_at": reset}))
    state = tmp_path / "fake-state"
    state.mkdir()
    monkeypatch.setenv("FAKE_USAGE_FILE", str(usage))
    monkeypatch.setenv("FAKE_STATE_DIR", str(state))
    monkeypatch.setenv("FAKE_PCT_PER_TURN", "0.5")
    monkeypatch.setenv("FAKE_COST_PER_TURN", "0.05")

    cfg = Config()
    cfg.runner.claude_cmd = [sys.executable, FAKE]
    cfg.runner.chunk_turns = 5
    cfg.runner.max_chunks_per_task = 100
    cfg.runner.kill_grace_seconds = 3
    cfg.guards.pre_run_warning = 600
    cfg.guards.require_input_idle_minutes = 0

    def read():
        u = read_usage(usage)
        return Snapshot(time.time(), "file", {"seven_day": Window("seven_day", u["seven_day"], u["resets_at"])})

    usage_svc = UsageService(cfg, store, [Source("file", read)])
    clock = Clock(time.time())
    notifier = Notifier(cfg.notify, store, enabled=False)
    activity = Activity(store, cfg.guards, input_idle_fn=lambda: None)
    tasks = [Task(id="r", kind="research", prompt="x", output_dir=str(tmp_path / "out"), repeat="always")]

    def filler():
        return FillController(cfg, store, usage_svc, Runner(cfg, store, cfg.runner.claude_cmd), tasks, notifier,
                              activity)

    d = Daemon(cfg, store, usage_svc, filler, activity, notifier, now=clock)
    return d, clock, usage, notifier, store, cfg


def test_full_cycle_warn_then_fill_to_target(world):
    d, clock, usage, notifier, store, cfg = world
    r = d.tick()
    assert r.wants_fill and r.target == 30
    assert r.state == "WARNED"
    assert any("곧" in t for t, _ in notifier.sent)
    clock.t += 300
    r = d.tick()
    assert "후 자동 실행" in r.action  # still waiting for the warning period
    clock.t += 400
    r = d.tick()
    real = read_usage(usage)["seven_day"]
    assert 28 <= real <= 32, real
    assert r.state == "MONITOR"
    assert any("종료" in t for t, _ in notifier.sent)
    # target reached → next tick does nothing (sticky target satisfied)
    clock.t += 60
    r = d.tick()
    assert not r.wants_fill
    state = json.loads(paths.state_file().read_text(encoding="utf-8"))
    assert state["week"]["pct"] >= 28


def test_user_activity_blocks_start(world):
    d, clock, usage, notifier, store, cfg = world
    spool.append(paths.spool_dir(), {"kind": "hook", "event": "prompt", "session_id": "s1", "ts": clock.t - 60})
    r = d.tick()
    assert r.wants_fill and r.blocked_by and "Claude Code" in r.blocked_by
    assert r.state == "MONITOR"


def test_pause_blocks(world):
    d, clock, usage, notifier, store, cfg = world
    paths.pause_file().write_text("x")
    r = d.tick()
    assert "일시정지" in r.blocked_by


def test_dry_run_changes_nothing(world):
    d, clock, usage, notifier, store, cfg = world
    d.dry_run = True
    cfg.guards.pre_run_warning = 0
    r = d.tick()
    assert "dry-run" in r.action
    assert json.loads(usage.read_text())["seven_day"] == 18.0
    assert not paths.state_file().exists()


def test_no_rule_yet(world, monkeypatch):
    d, clock, usage, notifier, store, cfg = world
    u = json.loads(usage.read_text())
    u["resets_at"] = time.time() + 5 * 86400
    usage.write_text(json.dumps(u))
    r = d.tick()
    assert not r.wants_fill and "체크포인트" in r.decision


def test_ingest_spool_statusline_and_hooks(store):
    now = time.time()
    spool.append(paths.spool_dir(), {"kind": "statusline", "ts": now, "session_id": "a", "activity": True,
                                     "rate_limits": {"seven_day": {"used_percentage": 12, "resets_at": now + 9999}}})
    spool.append(paths.spool_dir(), {"kind": "hook", "event": "stop", "session_id": "a", "ts": now})
    assert ingest_spool(store) == 2
    assert ingest_spool(store) == 0  # offset remembered
    assert store.latest_window("seven_day", now + 1)[0].pct == 12
    assert len(store.activity_since(now - 1)) == 2


def test_single_instance(tmp_path):
    a = SingleInstance(tmp_path / "x.lock")
    b = SingleInstance(tmp_path / "x.lock")
    assert a.acquire()
    assert not b.acquire()
    a.release()
    assert b.acquire()
    b.release()


def test_user_return_during_fill_stops_it(world, monkeypatch):
    import threading

    d, clock, usage, notifier, store, cfg = world
    monkeypatch.setenv("FAKE_TURN_SECONDS", "0.2")
    monkeypatch.setenv("FAKE_PCT_PER_TURN", "0.05")
    cfg.guards.pre_run_warning = 0

    def user_comes_back():
        time.sleep(1.5)
        spool.append(paths.spool_dir(), {"kind": "hook", "event": "prompt", "session_id": "me"})

    threading.Thread(target=user_comes_back, daemon=True).start()
    t0 = time.time()
    r = d.tick()
    assert time.time() - t0 < 30
    assert "사용자" in r.action, r.action
    assert store.get("daemon_state")["state"] == "COOLDOWN"
    assert read_usage(usage)["seven_day"] < 25


def test_stale_filling_state_is_reset(world):
    d, clock, usage, notifier, store, cfg = world
    store.set("daemon_state", {"state": "FILLING"})
    cfg.guards.pre_run_warning = 600
    r = d.tick()
    assert r.state in ("WARNED", "MONITOR")
