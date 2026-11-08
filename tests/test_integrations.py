import io
import json
import sys
import time

from tokenalarm import claude_setup, paths, spool
from tokenalarm.integrations import hook_main, render_statusline, statusline_main


def _stdin(monkeypatch, obj):
    data = json.dumps(obj).encode("utf-8")
    monkeypatch.setattr(sys, "stdin", type("S", (), {"buffer": io.BytesIO(data)})())


def test_hook_writes_spool(monkeypatch):
    _stdin(monkeypatch, {"session_id": "abc", "hook_event_name": "UserPromptSubmit"})
    assert hook_main("UserPromptSubmit") == 0
    recs, _ = spool.read_new(paths.spool_dir(), 0)
    assert recs[0]["event"] == "prompt" and recs[0]["session_id"] == "abc"


def test_hook_ignored_for_autorun(monkeypatch):
    monkeypatch.setenv("TOKENALARM_AUTORUN", "1")
    _stdin(monkeypatch, {"session_id": "abc"})
    hook_main("UserPromptSubmit")
    assert spool.read_new(paths.spool_dir(), 0)[0] == []


def test_notification_other_types_ignored(monkeypatch):
    _stdin(monkeypatch, {"session_id": "abc", "notification_type": "permission_prompt"})
    hook_main("Notification")
    assert spool.read_new(paths.spool_dir(), 0)[0] == []


def test_statusline_render_and_spool(monkeypatch, capsys):
    now = time.time()
    payload = {"session_id": "s", "cost": {"total_cost_usd": 0.1},
               "rate_limits": {"five_hour": {"used_percentage": 42, "resets_at": now + 2 * 3600 + 900},
                               "seven_day": {"used_percentage": 18, "resets_at": now + 3 * 86400 + 4 * 3600 + 60}}}
    _stdin(monkeypatch, payload)
    statusline_main()
    out = capsys.readouterr().out
    assert "5h" in out and "42%" in out and "18%" in out and "3d4h" in out
    recs, _ = spool.read_new(paths.spool_dir(), 0)
    assert recs and recs[0]["kind"] == "statusline"
    # identical payload right away → no duplicate spool line
    _stdin(monkeypatch, payload)
    statusline_main()
    assert len(spool.read_new(paths.spool_dir(), 0)[0]) == 1


def test_statusline_without_rate_limits():
    assert "첫 응답" in render_statusline({}, None, time.time())


def test_setup_claude_merge_idempotent_and_remove(tmp_path):
    settings = tmp_path / "settings.json"
    existing = {
        "model": "sonnet",
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]},
        "statusLine": {"type": "command", "command": "ccstatusline"},
    }
    settings.write_text("\ufeff" + json.dumps(existing), encoding="utf-8")  # BOM like Windows editors
    claude_setup.install(settings)
    claude_setup.install(settings)  # twice → still one entry per event
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["model"] == "sonnet"
    stop = data["hooks"]["Stop"]
    assert len(stop) == 2 and stop[0]["hooks"][0]["command"] == "echo mine"
    assert sum(claude_setup.is_ours(e) for e in stop) == 1
    assert data["hooks"]["Notification"][0]["matcher"] == "idle_prompt"
    assert data["statusLine"]["command"] == "ccstatusline"  # foreign status line kept
    claude_setup.install(settings, force_statusline=True)
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert "-m tokenalarm" in data["statusLine"]["command"]
    claude_setup.remove(settings)
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["hooks"] == {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}
    assert data["statusLine"]["command"] == "ccstatusline"  # restored
    assert list(tmp_path.glob("settings.json.bak-*"))


def test_setup_claude_refuses_invalid_json(tmp_path):
    import pytest

    settings = tmp_path / "settings.json"
    settings.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError):
        claude_setup.install(settings)
    assert settings.read_text(encoding="utf-8") == "{ not json"


def test_task_scheduler_xml():
    from tokenalarm.winsvc import task_xml

    xml = task_xml("PC\\홍길동", "C:\\Python\\pythonw.exe", "-m tokenalarm daemon", "C:\\data")
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml
    assert "<LogonType>InteractiveToken</LogonType>" in xml
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml
    assert "홍길동" in xml
