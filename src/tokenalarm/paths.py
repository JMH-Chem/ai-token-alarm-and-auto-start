"""Filesystem locations.

Everything tokenalarm owns lives in one folder (``TOKENALARM_HOME`` or the platform data dir,
e.g. ``%LOCALAPPDATA%\\tokenalarm`` on Windows) so it is easy to inspect, back up, or delete.
"""
from __future__ import annotations

import os
from pathlib import Path

APP = "tokenalarm"


def home() -> Path:
    env = os.environ.get("TOKENALARM_HOME")
    if env:
        p = Path(env).expanduser()
    else:
        from platformdirs import user_data_dir

        p = Path(user_data_dir(APP, appauthor=False, roaming=False))
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_file() -> Path:
    return home() / "config.yaml"


def tasks_file() -> Path:
    return home() / "tasks.yaml"


def db_file() -> Path:
    return home() / "tokenalarm.db"


def spool_dir() -> Path:
    p = home() / "spool"
    p.mkdir(parents=True, exist_ok=True)
    return p


def logs_dir() -> Path:
    p = home() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def runs_dir() -> Path:
    p = home() / "runs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def worktrees_dir() -> Path:
    p = home() / "worktrees"
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_file() -> Path:
    """Small JSON summary written by the daemon, read by the status line (cheap)."""
    return home() / "state.json"


def pause_file() -> Path:
    """Kill switch: if this file exists, no automatic task is started (running ones stop)."""
    return home() / "PAUSE"


def lock_file() -> Path:
    return home() / "daemon.lock"


def claude_home() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def examples_dir() -> Path:
    return Path(__file__).resolve().parent / "examples"
