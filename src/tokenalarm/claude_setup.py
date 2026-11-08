"""Safely add/remove tokenalarm's status line and hooks in Claude Code's ``settings.json``.

* backup first, read with utf-8-sig (BOM tolerant), write via temp file + os.replace (retry on Windows locks)
* only *append* to existing hook arrays; our entries are recognised by their command (``-m tokenalarm``),
  so re-running is idempotent and ``--remove`` removes exactly what we added
* an existing, foreign ``statusLine`` is left alone unless ``force_statusline``
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

from . import paths

MARK = "-m tokenalarm"
HOOK_SPECS = [
    ("UserPromptSubmit", None),
    ("Stop", None),
    ("SessionStart", None),
    ("SessionEnd", None),
    ("Notification", "idle_prompt"),
]


def settings_path() -> Path:
    return paths.claude_home() / "settings.json"


def python_for_hooks() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":  # pythonw has no stdout → status line would be empty
        alt = exe.with_name("python.exe")
        if alt.exists():
            exe = alt
    return str(exe)


def _q(path: str) -> str:
    # Forward slashes work in Git Bash, cmd and PowerShell; quotes handle spaces / Korean user names.
    return '"' + path.replace("\\", "/") + '"'


def base_command() -> str:
    cmd = f"{_q(python_for_hooks())} {MARK}"
    home = os.environ.get("TOKENALARM_HOME")
    if home:
        cmd += f" --home {_q(home)}"
    return cmd


def is_ours(entry: dict) -> bool:
    return any(MARK in str(h.get("command", "")) for h in (entry.get("hooks") or []))


def load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        return {}
    data = json.loads(text)  # raises on invalid JSON → caller aborts without touching the file
    if not isinstance(data, dict):
        raise ValueError("settings.json 최상위가 객체가 아닙니다")
    return data


def write_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tokenalarm.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # another process holds the file (Windows sharing violation)
            time.sleep(0.3 * (attempt + 1))
    raise PermissionError(f"{path} 를 쓸 수 없습니다 (다른 프로그램이 사용 중)")


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    dst = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, dst)
    return dst


def install(path: Path | None = None, force_statusline: bool = False) -> list[str]:
    path = path or settings_path()
    data = load_settings(path)
    msgs = []
    b = backup(path)
    if b:
        msgs.append(f"백업: {b}")
    base = base_command()
    hooks = data.setdefault("hooks", {})
    for event, matcher in HOOK_SPECS:
        arr = hooks.setdefault(event, [])
        arr[:] = [e for e in arr if not is_ours(e)]
        entry: dict = {"hooks": [{"type": "command", "command": f"{base} hook {event}", "timeout": 10}]}
        if matcher:
            entry = {"matcher": matcher, **entry}
        arr.append(entry)
    msgs.append(f"hooks 등록: {', '.join(e for e, _ in HOOK_SPECS)}")
    ours = {"type": "command", "command": f"{base} statusline", "padding": 0}
    current = data.get("statusLine")
    if current and MARK not in str(current.get("command", "")) and not force_statusline:
        msgs.append(f"기존 statusLine 유지: {current.get('command')!r} — 교체하려면 --force-statusline")
    else:
        if current and MARK not in str(current.get("command", "")):
            prev_file = paths.home() / "previous_statusline.json"  # restored by --remove
            prev_file.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
            msgs.append(f"기존 statusLine을 {prev_file} 에 저장하고 교체")
        data["statusLine"] = ours
        msgs.append("statusLine 등록")
    write_settings(path, data)
    msgs.append(f"저장: {path}")
    return msgs


def remove(path: Path | None = None) -> list[str]:
    path = path or settings_path()
    if not path.exists():
        return ["settings.json 없음 — 할 일 없음"]
    data = load_settings(path)
    msgs = []
    b = backup(path)
    if b:
        msgs.append(f"백업: {b}")
    hooks = data.get("hooks") or {}
    for event in list(hooks.keys()):
        arr = [e for e in hooks[event] if not is_ours(e)]
        if arr:
            hooks[event] = arr
        else:
            del hooks[event]
    if not hooks:
        data.pop("hooks", None)
    sl = data.get("statusLine")
    if sl and MARK in str(sl.get("command", "")):
        prev_file = paths.home() / "previous_statusline.json"
        if prev_file.exists():
            data["statusLine"] = json.loads(prev_file.read_text(encoding="utf-8"))
            msgs.append("이전 statusLine 복원")
        else:
            data.pop("statusLine", None)
            msgs.append("statusLine 제거")
    write_settings(path, data)
    msgs.append(f"tokenalarm 항목 제거 완료: {path}")
    return msgs
