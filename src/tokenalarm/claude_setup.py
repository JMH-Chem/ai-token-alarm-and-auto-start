"""Safely add/remove tokenalarm's status line and hooks in Claude Code's ``settings.json``.

* backup first, read with utf-8-sig (BOM tolerant), write via temp file + os.replace (retry on Windows locks)
* only *append* to existing hook arrays; our entries are recognised by their command, so re-running is
  idempotent and ``remove`` deletes exactly what we added (v0.1 ``python -m tokenalarm`` entries included)
* hooks use the exec form (``command`` + ``args``) → no shell is involved, so Git Bash / PowerShell / spaces /
  Korean user names do not matter. If an old Claude Code ignores ``args``, ``tokenalarm-hook`` runs with no
  arguments and exits 0 silently.
* the status line has no exec form, so its command is a single shell-safe token (8.3 short path on Windows
  when the real path contains spaces or shell metacharacters)
* an existing, foreign ``statusLine`` is left alone unless ``force_statusline``
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import sysconfig
import time
from pathlib import Path

from . import paths
from .i18n import L

HOOK_SPECS = [
    ("UserPromptSubmit", None),
    ("Stop", None),
    ("SessionStart", None),
    ("SessionEnd", None),
    ("Notification", "idle_prompt"),
]
_OURS = re.compile(r"(-m\s+tokenalarm\b)|(tokenalarm(-hook|-statusline)?(\.exe)?([\"'\s]|$))", re.I)
_UNSAFE = re.compile(r"[\s$&()'\"`;|<>!*?#%^,=\[\]{}~]")


def settings_path() -> Path:
    return paths.claude_home() / "settings.json"


def _exe(name: str) -> str:
    return name + (".exe" if sys.platform == "win32" else "")


def script_path(name: str) -> str | None:
    """Absolute path of one of our console scripts (tokenalarm, tokenalarm-hook, tokenalarm-statusline)."""
    exe = _exe(name)
    dirs = [Path(sys.executable).parent]
    for scheme in (None, f"{os.name}_user"):
        try:
            d = sysconfig.get_path("scripts", scheme) if scheme else sysconfig.get_path("scripts")
            if d:
                dirs.append(Path(d))
        except KeyError:
            pass
    for d in dirs:
        p = d / exe
        if p.exists():
            return str(p)
    found = shutil.which(name)
    return found


def python_for_hooks() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":  # pythonw has no stdout → status line would be empty
        alt = exe.with_name("python.exe")
        if alt.exists():
            exe = alt
    return str(exe)


def short_path(path: str) -> str:
    """Windows 8.3 short path (no spaces/non-ASCII) when available, else the input."""
    if sys.platform != "win32":
        return path
    try:
        import ctypes
        from ctypes import wintypes

        fn = ctypes.windll.kernel32.GetShortPathNameW
        fn.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        buf = ctypes.create_unicode_buffer(1024)
        n = fn(path, buf, 1024)
        return buf.value if 0 < n < 1024 else path
    except Exception:
        return path


def shell_token(path: str) -> str:
    """A path usable as one bare word in bash, cmd and PowerShell (quoted only as a last resort)."""
    p = path.replace("\\", "/")
    if not _UNSAFE.search(p):
        return p
    sp = short_path(path).replace("\\", "/")
    if not _UNSAFE.search(sp):
        return sp
    return '"' + p + '"'  # works in bash/cmd; doctor warns because PowerShell would need `&`


def hook_entry(event: str, matcher: str | None) -> dict:
    hook_exe = script_path("tokenalarm-hook")
    if hook_exe:
        cmd: dict = {"type": "command", "command": hook_exe, "args": [event], "timeout": 10}
    else:
        cmd = {"type": "command", "command": python_for_hooks(), "args": ["-m", "tokenalarm", "hook", event],
               "timeout": 10}
    entry: dict = {"hooks": [cmd]}
    if matcher:
        entry = {"matcher": matcher, **entry}
    return entry


def statusline_command() -> str:
    sl = script_path("tokenalarm-statusline")
    if sl:
        return shell_token(sl)
    return f"{shell_token(python_for_hooks())} -m tokenalarm statusline"


def _cmd_text(h: dict) -> str:
    return " ".join([str(h.get("command", "")), *[str(a) for a in (h.get("args") or [])]]).replace("\\", "/")


def is_ours(entry: dict) -> bool:
    return any(_OURS.search(_cmd_text(h)) for h in (entry.get("hooks") or []) if isinstance(h, dict))


def statusline_is_ours(sl: dict | None) -> bool:
    return bool(sl) and bool(_OURS.search(_cmd_text(sl)))


def load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        return {}
    data = json.loads(text)  # raises on invalid JSON → caller aborts without touching the file
    if not isinstance(data, dict):
        raise ValueError(L("settings.json 최상위가 객체가 아닙니다", "settings.json top level is not an object"))
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
    raise PermissionError(L(f"{path} 를 쓸 수 없습니다 (다른 프로그램이 사용 중)", f"cannot write {path} (in use)"))


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    dst = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, dst)
    return dst


def _previous_statusline_file() -> Path:
    return paths.home() / "previous_statusline.json"


def status(path: Path | None = None) -> dict:
    """What is currently registered (for doctor)."""
    path = path or settings_path()
    try:
        data = load_settings(path)
    except (ValueError, OSError) as e:
        return {"ok": False, "error": str(e), "hooks": [], "statusline": None, "statusline_ours": False}
    hooks = data.get("hooks") or {}
    ours = [ev for ev, _ in HOOK_SPECS if any(is_ours(e) for e in hooks.get(ev) or [] if isinstance(e, dict))]
    sl = data.get("statusLine")
    return {"ok": True, "hooks": ours, "statusline": sl, "statusline_ours": statusline_is_ours(sl),
            "entries": {ev: [e for e in hooks.get(ev) or [] if isinstance(e, dict) and is_ours(e)]
                        for ev in ours}}


def install(path: Path | None = None, force_statusline: bool = False) -> list[str]:
    path = path or settings_path()
    data = load_settings(path)
    msgs = []
    b = backup(path)
    if b:
        msgs.append(L(f"백업: {b}", f"backup: {b}"))
    hooks = data.setdefault("hooks", {})
    for event, matcher in HOOK_SPECS:
        arr = hooks.setdefault(event, [])
        arr[:] = [e for e in arr if not (isinstance(e, dict) and is_ours(e))]
        arr.append(hook_entry(event, matcher))
    msgs.append(L("hooks 등록: ", "hooks registered: ") + ", ".join(e for e, _ in HOOK_SPECS))
    ours = {"type": "command", "command": statusline_command(), "padding": 0}
    current = data.get("statusLine")
    if current and not statusline_is_ours(current) and not force_statusline:
        msgs.append(L(f"기존 statusLine 유지: {current.get('command')!r} — 교체하려면 --force-statusline",
                      f"kept existing statusLine {current.get('command')!r} — use --force-statusline to replace"))
    else:
        if current and not statusline_is_ours(current):
            _previous_statusline_file().write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
            msgs.append(L("기존 statusLine을 저장하고 교체 (제거 시 복원)", "saved and replaced the existing statusLine "
                                                                    "(restored on remove)"))
        data["statusLine"] = ours
        msgs.append(L("statusLine 등록", "statusLine registered"))
    write_settings(path, data)
    msgs.append(L(f"저장: {path}", f"saved: {path}"))
    return msgs


def remove(path: Path | None = None) -> list[str]:
    path = path or settings_path()
    if not path.exists():
        return [L("settings.json 없음 — 할 일 없음", "no settings.json — nothing to do")]
    data = load_settings(path)
    msgs = []
    b = backup(path)
    if b:
        msgs.append(L(f"백업: {b}", f"backup: {b}"))
    # restore / remove the status line first so a half-finished uninstall never leaves a dangling command
    sl = data.get("statusLine")
    if statusline_is_ours(sl):
        prev = _previous_statusline_file()
        if prev.exists():
            data["statusLine"] = json.loads(prev.read_text(encoding="utf-8"))
            msgs.append(L("이전 statusLine 복원", "previous statusLine restored"))
        else:
            data.pop("statusLine", None)
            msgs.append(L("statusLine 제거", "statusLine removed"))
    hooks = data.get("hooks") or {}
    for event in list(hooks.keys()):
        arr = [e for e in hooks[event] if not (isinstance(e, dict) and is_ours(e))]
        if arr:
            hooks[event] = arr
        else:
            del hooks[event]
    if not hooks:
        data.pop("hooks", None)
    write_settings(path, data)
    msgs.append(L(f"tokenalarm 항목 제거 완료: {path}", f"tokenalarm entries removed: {path}"))
    return msgs
