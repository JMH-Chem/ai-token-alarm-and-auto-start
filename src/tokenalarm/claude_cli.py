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
    raise ClaudeNotFound("`claude` CLI를 찾을 수 없습니다. Claude Code를 설치하거나 config의 runner.claude_cmd를 지정하세요.")


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
