"""stop / open / clean / uninstall."""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import paths
from .i18n import L

log = logging.getLogger(__name__)


def _flags() -> dict:
    from .claude_cli import popen_kwargs

    return popen_kwargs()


# --- stop ------------------------------------------------------------------------------------------

def stop_daemon(timeout: float = 30) -> tuple[bool, bool]:
    """Ask the daemon/tray to exit (STOP file), then force it. Returns (was_running, stopped)."""
    from .daemon import is_running

    running, pid = is_running()
    if not running:
        return False, True
    paths.stop_file().write_text("stop\n", encoding="utf-8")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        if not is_running()[0]:
            break
    if is_running()[0]:
        from . import winsvc

        winsvc.end_task()
        if pid:
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, **_flags())
                else:
                    import signal

                    os.kill(pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
        for _ in range(20):
            if not is_running()[0]:
                break
            time.sleep(0.5)
    try:
        paths.stop_file().unlink()
    except OSError:
        pass
    return True, not is_running()[0]


# --- open ------------------------------------------------------------------------------------------

def open_target(target: str) -> Path:
    p = {
        "data": paths.home(),
        "config": paths.config_file(),
        "tasks": paths.tasks_file(),
        "logs": paths.logs_dir(),
        "runs": paths.runs_dir(),
    }[target]
    if sys.platform == "win32":
        if p.is_dir():
            os.startfile(str(p))  # type: ignore[attr-defined]  # Explorer
        else:
            if not p.exists():
                p.write_text("", encoding="utf-8")
            subprocess.Popen(["notepad.exe", str(p)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    elif shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return p


# --- clean -----------------------------------------------------------------------------------------

def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=120, **_flags())


def _main_repo_of(worktree: Path) -> Path | None:
    r = _git("-C", str(worktree), "rev-parse", "--git-common-dir")
    if r.returncode != 0:
        return None
    common = Path(r.stdout.strip())
    if not common.is_absolute():
        common = (worktree / common).resolve()
    return common.parent


def remove_worktree(worktree: Path, force: bool = False) -> tuple[bool, str]:
    """``git worktree remove`` — the branch and its commits stay in the main repository."""
    repo = _main_repo_of(worktree)
    if repo is None:
        shutil.rmtree(worktree, ignore_errors=True)
        return True, L("git 정보 없음 — 폴더만 삭제", "no git metadata — folder deleted")
    args = ["-C", str(repo), "worktree", "remove", str(worktree)] + (["--force"] if force else [])
    r = _git(*args)
    if r.returncode != 0:
        return False, r.stderr.strip()
    _git("-C", str(repo), "worktree", "prune")
    return True, str(repo)


def clean(store, days: float = 14, dry_run: bool = False) -> list[str]:
    """Remove finished worktrees and old run logs (outputs/deliverables are never touched)."""
    msgs: list[str] = []
    cutoff = time.time() - days * 86400
    active = set()
    for row in store.conn.execute("SELECT workdir FROM task_state WHERE last_status='partial'"):
        if row[0]:
            active.add(str(Path(row[0])))
    for wt in sorted(paths.worktrees_dir().iterdir()) if paths.worktrees_dir().exists() else []:
        if not wt.is_dir() or str(wt) in active or wt.stat().st_mtime > cutoff:
            continue
        if dry_run:
            msgs.append(L(f"[dry-run] worktree 삭제 예정: {wt}", f"[dry-run] would remove worktree: {wt}"))
            continue
        ok, info = remove_worktree(wt)
        msgs.append((L("worktree 삭제 (브랜치는 보존): ", "removed worktree (branch kept): ") + str(wt)) if ok else
                    L(f"건너뜀 (커밋 안 된 변경 있음?): {wt} — {info}", f"skipped (uncommitted changes?): {wt} — {info}"))
    for run in sorted(paths.runs_dir().iterdir()) if paths.runs_dir().exists() else []:
        if run.is_dir() and run.stat().st_mtime < cutoff:
            if dry_run:
                msgs.append(L(f"[dry-run] 실행 로그 삭제 예정: {run.name}", f"[dry-run] would delete run log: {run.name}"))
            else:
                shutil.rmtree(run, ignore_errors=True)
                msgs.append(L(f"실행 로그 삭제: {run.name}", f"deleted run log: {run.name}"))
    return msgs or [L("정리할 것이 없습니다", "nothing to clean")]


# --- uninstall -------------------------------------------------------------------------------------

def uninstall(remove_data: bool = False) -> list[str]:
    """Order matters: stop → autostart → settings.json → AUMID → worktrees → data."""
    from . import claude_setup, winsvc
    from .notify import channels

    msgs: list[str] = []
    was, stopped = stop_daemon()
    if was:
        msgs.append(L("데몬 중지", "daemon stopped") if stopped else L("데몬 중지 실패", "could not stop daemon"))
    msgs += [m for m in winsvc.uninstall() if m]
    try:
        msgs += claude_setup.remove()
    except ValueError as e:
        msgs.append(L(f"settings.json 은 건드리지 않음: {e}", f"settings.json left untouched: {e}"))
    channels.unregister_aumid()
    if remove_data:
        wdir = paths.worktrees_dir()
        for wt in sorted(wdir.iterdir()) if wdir.exists() else []:
            if wt.is_dir():
                ok, info = remove_worktree(wt, force=True)
                msgs.append(L(f"worktree 정리: {wt.name}", f"worktree removed: {wt.name}") if ok else info)
        for h in list(logging.getLogger().handlers):  # Windows cannot delete an open log file
            try:
                h.close()
            finally:
                logging.getLogger().removeHandler(h)
        home = paths.home()
        for _ in range(5):
            shutil.rmtree(home, ignore_errors=True)
            if not home.exists():
                break
            time.sleep(1)
        msgs.append(L(f"데이터 폴더 삭제: {home}", f"data folder deleted: {home}") if not home.exists() else
                    L(f"데이터 폴더 일부를 지우지 못함: {home}", f"could not fully delete: {home}"))
    else:
        msgs.append(L(f"데이터는 남겨 둠: {paths.home()} (모두 지우려면 --all)",
                      f"data kept: {paths.home()} (use --all to delete)"))
    msgs.append(L("프로그램 제거: uv tool uninstall tokenalarm", "Remove the program: uv tool uninstall tokenalarm"))
    return msgs


# --- install record ---------------------------------------------------------------------------------

def install_record() -> dict:
    p = paths.home() / "install.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_install_record(**kw) -> None:
    rec = install_record()
    rec.update(kw)
    (paths.home() / "install.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
