import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "tokenalarm-home"
    monkeypatch.setenv("TOKENALARM_HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    monkeypatch.delenv("TOKENALARM_AUTORUN", raising=False)
    monkeypatch.setenv("TOKENALARM_LANG", "ko")  # existing assertions are Korean; English has its own tests
    from tokenalarm import i18n

    i18n.reset()
    return home


@pytest.fixture
def store(tmp_path):
    from tokenalarm.store import Store

    s = Store(tmp_path / "t.db")
    yield s
    s.close()
