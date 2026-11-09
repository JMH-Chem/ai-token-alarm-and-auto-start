import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tokenalarm import cli, i18n, paths
from tokenalarm.config import PRESETS, ConfigError, load_config, minimal_config_text
from tokenalarm.tasks import TaskError, add_task, load_tasks, read_raw, remove_task, set_enabled

FAKE = str(Path(__file__).with_name("fake_claude.py"))


# --- i18n ----------------------------------------------------------------------------------------

def test_english_messages(monkeypatch):
    from tokenalarm.config import DEFAULT_RULES, Guards
    from tokenalarm.policy import decide
    from tokenalarm.usage.base import Snapshot, Window

    monkeypatch.setenv("TOKENALARM_LANG", "en")
    now = 1_800_000_000.0
    snap = Snapshot(now, "t", {"seven_day": Window("seven_day", 18, now + 70 * 3600)})
    d = decide(snap, DEFAULT_RULES, Guards(), now)
    assert "filling to 30%" in d.reason and "before reset" in d.reason
    monkeypatch.setenv("TOKENALARM_LANG", "ko")
    assert "채우기" in decide(snap, DEFAULT_RULES, Guards(), now).reason


def test_language_detection_env(monkeypatch):
    monkeypatch.delenv("TOKENALARM_LANG")
    for var in ("LC_ALL", "LC_MESSAGES", "LANGUAGE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANG", "ko_KR.UTF-8")
    i18n.reset()
    if sys.platform != "win32":
        assert i18n.lang() == "ko"
    i18n.set_lang("en")
    assert i18n.lang() == "en"


# --- config --------------------------------------------------------------------------------------

def test_presets_and_minimal_config(tmp_path):
    for name in PRESETS:
        p = tmp_path / f"{name}.yaml"
        p.write_text(minimal_config_text("en", name, True, "tokenalarm-x", [{"type": "slack", "url": "https://h"}],
                                         "09:00-18:00"), encoding="utf-8")
        cfg = load_config(p)
        assert cfg.preset == name and cfg.weekly_fill_rules == PRESETS[name]
        assert cfg.guards.quiet_hours == "09:00-18:00" and cfg.notify.ntfy.topic == "tokenalarm-x"
        assert all(r.fill_to <= cfg.guards.weekly_hard_cap for r in cfg.weekly_fill_rules)


def test_unknown_key_suggestion(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("guards: {weekly_hardcap: 90}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="weekly_hard_cap"):
        load_config(p)


# --- tasks editing -------------------------------------------------------------------------------

def test_add_remove_enable_roundtrip(tmp_path):
    tf = tmp_path / "tasks.yaml"
    tid = add_task(tf, "research", "Summarize this week's AI news", repeat="weekly", priority=3)
    assert tid.startswith("research-summarize")
    tid2 = add_task(tf, "study", "열역학 정리")  # no ASCII words → plain kind id
    assert tid2 == "study"
    assert {t.id for t in load_tasks(tf)} == {tid, tid2}
    set_enabled(tf, tid, False)
    assert not next(t for t in load_tasks(tf) if t.id == tid).enabled
    set_enabled(tf, tid, True)
    remove_task(tf, tid2)
    assert [t.id for t in load_tasks(tf)] == [tid]
    assert (tmp_path / "tasks.yaml.bak").exists()
    assert tf.read_text(encoding="utf-8").startswith("#")
    with pytest.raises(TaskError):
        remove_task(tf, "nope")


def test_add_repo_task_validates_git(tmp_path):
    tf = tmp_path / "tasks.yaml"
    with pytest.raises(TaskError):
        add_task(tf, "repo", "x", repo=str(tmp_path))
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    tid = add_task(tf, "repo", "add tests", repo=str(repo), allow=["Bash(npm test:*)"])
    t = load_tasks(tf)[0]
    assert t.id == tid and "Bash(npm test:*)" in t.allowed_tools and "Read" in t.allowed_tools


def test_cli_add_and_tasks(capsys):
    assert cli.main(["add", "research", "Weekly", "LLM", "papers", "--repeat", "daily"]) == 0
    assert cli.main(["tasks"]) == 0
    out = capsys.readouterr().out
    assert "research-weekly-llm" in out and "daily" in out
    raw = read_raw(paths.tasks_file())
    assert raw["tasks"][0]["prompt"] == "Weekly LLM papers"


# --- store migration -----------------------------------------------------------------------------

def test_store_user_version(tmp_path):
    from tokenalarm.store import SCHEMA_VERSION, Store

    s = Store(tmp_path / "x.db")
    assert s.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    s.close()
    s = Store(tmp_path / "x.db")  # reopen is idempotent
    assert s.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    s.close()


# --- doctor --------------------------------------------------------------------------------------

def test_doctor_json_redacts(monkeypatch, capsys, tmp_path):
    cfg = paths.config_file()
    cfg.write_text(minimal_config_text("en", "standard", True, "tokenalarm-secrettopic",
                                       [{"type": "discord", "url": "https://discord.com/api/webhooks/123/SECRET"}]),
                   encoding="utf-8")
    monkeypatch.setenv("TOKENALARM_LANG", "en")
    cli.main(["doctor", "--json", "--no-claude"])
    out = capsys.readouterr().out
    data = json.loads(out)
    ids = {c["id"] for c in data["checks"]}
    assert {"python", "config", "tasks", "claude", "hooks", "usage", "daemon", "notify"} <= ids
    assert "SECRET" not in out and "secrettopic" not in out


def test_doctor_text_has_fixes(capsys, monkeypatch):
    monkeypatch.setenv("TOKENALARM_LANG", "en")
    cli.main(["doctor", "--no-claude"])
    out = capsys.readouterr().out
    assert "→ tokenalarm setup" in out or "→ tokenalarm" in out


# --- setup wizard (non-interactive) ----------------------------------------------------------------

def test_setup_yes_with_fake_claude(monkeypatch, capsys, tmp_path):
    usage = tmp_path / "usage.json"
    usage.write_text(json.dumps({"seven_day": 10.0, "resets_at": time.time() + 99999}))
    state = tmp_path / "fake"
    state.mkdir()
    monkeypatch.setenv("FAKE_USAGE_FILE", str(usage))
    monkeypatch.setenv("FAKE_STATE_DIR", str(state))
    monkeypatch.setattr("tokenalarm.claude_cli.resolve_claude_cmd", lambda configured=None: [sys.executable, FAKE])
    monkeypatch.delenv("TOKENALARM_LANG")  # let --lang decide
    rc = cli.main(["setup", "--yes", "--lang", "en", "--preset", "conservative", "--ntfy", "none",
                   "--autostart", "none", "--no-test-notify"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Logged in with a subscription" in out
    cfg = load_config(paths.config_file())
    assert cfg.language == "en" and cfg.preset == "conservative"
    settings = json.loads((paths.claude_home() / "settings.json").read_text(encoding="utf-8"))
    assert "UserPromptSubmit" in settings["hooks"] and "statusLine" in settings
    assert (paths.home() / "prompts" / "research_weekly.md").exists()
    # second run keeps the config unless --force, and --repair works
    assert cli.main(["setup", "--yes", "--lang", "en", "--autostart", "none", "--no-test-notify"]) == 0
    assert load_config(paths.config_file()).preset == "conservative"
    assert cli.main(["setup", "--repair"]) == 0


# --- stop / daemon loop --------------------------------------------------------------------------

def test_run_forever_honours_stop_file_and_event(store):
    from tokenalarm.daemon import Daemon, consume_stop_request

    calls = {"n": 0}

    class D(Daemon):
        def tick(self):
            calls["n"] += 1
            return type("R", (), {"state": "MONITOR", "decision": "", "action": ""})()

    from tokenalarm.config import Config

    cfg = Config()
    cfg.daemon.tick_seconds = 5
    d = D(cfg, store, None, None, None, None)
    paths.stop_file().write_text("x")
    d.run_forever()  # returns immediately because of STOP
    assert calls["n"] == 0 and not paths.stop_file().exists()
    ev = threading.Event()
    t = threading.Thread(target=d.run_forever, args=(ev,), daemon=True)
    t.start()
    time.sleep(0.3)
    ev.set()
    t.join(5)
    assert not t.is_alive() and calls["n"] >= 1
    assert consume_stop_request() is False


def test_stop_without_daemon(capsys):
    assert cli.main(["stop"]) == 0


# --- config hot reload ---------------------------------------------------------------------------

def test_ctx_reload_if_changed(capsys):
    paths.config_file().write_text(minimal_config_text("ko", "standard"), encoding="utf-8")
    args = cli.build_parser().parse_args(["status"])
    ctx = cli.Ctx(args)
    assert ctx.reload_if_changed() is None
    time.sleep(0.01)
    paths.config_file().write_text(minimal_config_text("ko", "aggressive") + "\n", encoding="utf-8")
    new = ctx.reload_if_changed()
    assert new is not None and new.preset == "aggressive"
    paths.config_file().write_text("guards: [broken\n", encoding="utf-8")
    assert ctx.reload_if_changed() is None  # invalid → keep previous
    assert ctx.cfg.preset == "aggressive"
    ctx.store.close()


# --- clean / uninstall ---------------------------------------------------------------------------

def _repo_with_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    g = ["git", "-C", str(repo)]
    subprocess.run([*g, "init", "-q"], check=True)
    subprocess.run([*g, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "i"],
                   check=True)
    wt = paths.worktrees_dir() / "job-1"
    subprocess.run([*g, "worktree", "add", "-q", "-b", "tokenalarm/job/1", str(wt)], check=True)
    return repo, wt


def test_clean_removes_worktree_keeps_branch(tmp_path, store):
    import os

    from tokenalarm.maintenance import clean

    repo, wt = _repo_with_worktree(tmp_path)
    old = time.time() - 30 * 86400
    os.utime(wt, (old, old))
    msgs = clean(store, days=14)
    assert not wt.exists(), msgs
    branches = subprocess.run(["git", "-C", str(repo), "branch", "--list", "tokenalarm/*"], capture_output=True,
                              text=True).stdout
    assert "tokenalarm/job/1" in branches


def test_uninstall_all_restores_settings_and_deletes_data(tmp_path):
    from tokenalarm import claude_setup
    from tokenalarm.maintenance import uninstall

    settings = paths.claude_home() / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "mine"}}), encoding="utf-8")
    claude_setup.install(settings, force_statusline=True)
    home = paths.home()
    msgs = uninstall(remove_data=True)
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data == {"statusLine": {"type": "command", "command": "mine"}}, msgs
    assert not home.exists()


# --- tray (pure parts) ---------------------------------------------------------------------------

def test_tray_tooltip_and_colors(monkeypatch):
    from tokenalarm import tray

    monkeypatch.setenv("TOKENALARM_LANG", "en")
    st = {"week": {"pct": 29.4, "projected": 47.0, "seconds_left": 3600 * 30}, "five": {"pct": 12},
          "state": "MONITOR", "wants_fill": True, "target": 50}
    tip = tray.tooltip(st)
    assert "wk 29%" in tip and "~47%" in tip and "pending 50%" in tip and len(tip) <= tray.TOOLTIP_MAX
    assert len(tray.tooltip({"week": {"pct": 1, "projected": 2}, "decision": "x" * 500})) <= tray.TOOLTIP_MAX
    assert tray.color_for(None) == tray.GRAY and tray.color_for(10) == tray.GREEN
    assert tray.color_for(70) == tray.YELLOW and tray.color_for(90) == tray.RED
    assert "paused" in tray.tooltip(st, paused=True)


def test_tray_icon_render():
    pytest.importorskip("PIL")
    from tokenalarm import tray

    img = tray.render_icon(42.0, filling=True)
    assert img.size == (64, 64)
    assert img.getpixel((10, 32))[:3] == tray.GREEN[:3] or img.getpixel((8, 32))[3] > 0
    assert tray.render_icon(None).getpixel((32, 6))[:3] == tray.GRAY[:3]


def test_stop_file_interrupts_fill(store, tmp_path):
    from tokenalarm.config import Config
    from tokenalarm.fill import FillController, LiveUsage

    fc = FillController(Config(), store, None, None, [], None)
    paths.stop_file().write_text("x")
    r = fc.stop_reason(30, LiveUsage(None), time.time(), None)
    assert getattr(r, "code", None) == "stop_request"


def test_redact_keeps_json_valid_and_docs_links():
    from tokenalarm.doctor import Check, to_json

    checks = [Check("claude", "fail", "Claude Code CLI not found", "x", "https://code.claude.com/docs/en/setup"),
              Check("n", "ok", "hook", "https://discord.com/api/webhooks/1/TOKEN \"quoted\"", "")]
    data = json.loads(to_json(checks, "topic-abc"))
    assert data["checks"][0]["fix"] == "https://code.claude.com/docs/en/setup"
    assert "TOKEN" not in data["checks"][1]["detail"] and "discord.com/<redacted>" in data["checks"][1]["detail"]
