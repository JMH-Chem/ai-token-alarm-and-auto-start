"""Locating and launching the official ``claude`` CLI safely."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

AUTORUN_ENV = "TOKENALARM_AUTORUN"
# Variables that would silently switch billing from the subscription to the API.
BILLING_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


class ClaudeNotFound(RuntimeError):
    pass


def resolve_claude_cmd(configured: list[str] | None = None) -> list[str]:
    if configured:
        return list(configured)
    candidates: list[str | None] = []
    if sys.platform == "win32":
        candidates.append(shutil.which("claude.exe"))
        candidates.append(str(Path.home() / ".local" / "bin" / "claude.exe"))
    candidates.append(shutil.which("claude"))
    candidates.append(str(Path.home() / ".local" / "bin" / "claude"))
    for c in candidates:
        if c and Path(c).exists():
            return [c]
    from .i18n import L

    raise ClaudeNotFound(L("`claude` CLI를 찾을 수 없습니다. Claude Code를 설치하거나 config의 runner.claude_cmd를 지정하세요.",
                           "`claude` CLI not found. Install Claude Code or set runner.claude_cmd in config."))


def autorun_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    for k in BILLING_ENV_VARS:
        env.pop(k, None)
    env[AUTORUN_ENV] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if extra:
        env.update(extra)
    return env


def popen_kwargs() -> dict[str, Any]:
    kw: dict[str, Any] = {}
    if sys.platform == "win32":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    else:
        kw["start_new_session"] = True
    return kw


_job_handle: int | None = None  # 0 = unavailable


def _windows_job() -> int | None:
    """A Job Object with KILL_ON_JOB_CLOSE: if tokenalarm dies, its claude children die with it."""
    global _job_handle
    if _job_handle is not None:
        return _job_handle or None
    _job_handle = 0
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        handle = k32.CreateJobObjectW(None, None)
        if not handle:
            return None
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return None
        _job_handle = int(handle)
        return _job_handle
    except Exception:
        return None


def attach_to_job(proc: subprocess.Popen) -> bool:
    if sys.platform != "win32":
        return False  # POSIX: start_new_session + killpg already handle the tree
    job = _windows_job()
    handle = getattr(proc, "_handle", None)
    if not job or handle is None:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        return bool(k32.AssignProcessToJobObject(job, int(handle)))
    except Exception:
        return False


def user_message(text: str) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}}, ensure_ascii=False) + "\n"


def interrupt_message(request_id: str) -> str:
    return json.dumps({"type": "control_request", "request_id": request_id,
                       "request": {"subtype": "interrupt"}}) + "\n"


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=30, **popen_kwargs())
        else:
            import signal

            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
    except Exception:  # pragma: no cover - last resort
        proc.kill()


def run_stream(cmd: list[str], stdin_text: str | None, env: dict[str, str], timeout: float,
               cwd: str | None = None) -> list[dict[str, Any]]:
    """Run a short headless command and return its parsed stream-json events."""
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=cwd, env=env, **popen_kwargs())
    attach_to_job(proc)
    events: list[dict[str, Any]] = []

    def reader() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            try:
                events.append(json.loads(raw.decode("utf-8", errors="replace")))
            except ValueError:
                continue

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    threading.Thread(target=lambda: proc.stderr.read() if proc.stderr else None, daemon=True).start()
    try:
        if proc.stdin is not None:
            if stdin_text:
                proc.stdin.write(stdin_text.encode("utf-8"))
            proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if any(e.get("type") == "result" for e in events):
                break
            if proc.poll() is not None:
                break
            time.sleep(0.1)
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
        t.join(2)
    return events
