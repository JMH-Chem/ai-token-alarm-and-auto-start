"""Run one queued task with the official ``claude`` CLI, in small budgeted chunks.

* The prompt goes over stdin (``--input-format stream-json``): no command-line quoting / length issues.
* Each chunk is one process with ``--max-turns`` and ``--max-budget-usd``; the next chunk resumes the same
  session (``--resume``). Between chunks the caller re-checks usage, so the target is approached in steps.
* Mid-chunk stops use the interrupt control request (same as the SDKs), then close stdin; the process tree
  is killed only if it does not exit within the grace period.
* Every ``rate_limit_event`` in the stream is real plan utilization → reported to ``on_snapshot``.
"""
from __future__ import annotations

import json
import logging
import queue
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import paths
from .claude_cli import autorun_env, interrupt_message, kill_tree, popen_kwargs, user_message
from .config import Config
from .store import Store
from .tasks import Task
from .usage.base import Snapshot
from .usage.parse import parse_rate_limit_event

log = logging.getLogger(__name__)

StopFn = Callable[[], "str | None"]
BudgetFn = Callable[[], float]
SnapshotFn = Callable[[Snapshot], None]
CostFn = Callable[[float], None]


@dataclass
class ChunkResult:
    subtype: str | None = None
    is_error: bool = False
    cost: float = 0.0
    turns: int = 0
    session_id: str | None = None
    text: str | None = None
    interrupted: bool = False
    stop_reason: str | None = None
    api_key_source: str | None = None
    error: str | None = None
    exit_code: int | None = None


@dataclass
class RunOutcome:
    task_id: str
    status: str  # done | partial | failed | blocked
    reason: str | None
    cost: float = 0.0
    turns: int = 0
    chunks: int = 0
    session_id: str | None = None
    workdir: str | None = None
    branch: str | None = None
    log_dir: str | None = None
    summary: str | None = None
    run_id: int | None = None
    pct_start: float | None = None
    pct_end: float | None = None
    chunk_results: list[ChunkResult] = field(default_factory=list)


class WorkdirError(RuntimeError):
    pass


def _stamp(now: float) -> str:
    return datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S")


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120, **popen_kwargs())


class Runner:
    def __init__(self, cfg: Config, store: Store, claude_cmd: list[str], now: Callable[[], float] = time.time):
        self.cfg = cfg
        self.store = store
        self.claude_cmd = claude_cmd
        self.now = now

    # --- working directory ---------------------------------------------------------------------
    def prepare_workdir(self, task: Task, resume_state: dict[str, Any] | None) -> tuple[str, str | None]:
        if resume_state and resume_state.get("workdir") and Path(resume_state["workdir"]).exists():
            return resume_state["workdir"], resume_state.get("branch")
        stamp = _stamp(self.now())
        if task.kind == "repo":
            repo = task.repo or ""
            top = _git(repo, "rev-parse", "--show-toplevel")
            if top.returncode != 0:
                raise WorkdirError(f"git 저장소가 아닙니다: {repo} ({top.stderr.strip()})")
            branch = f"tokenalarm/{task.id}/{stamp}"
            wt = paths.worktrees_dir() / f"{task.id}-{stamp}"
            base = task.base_ref or "HEAD"
            res = _git(repo, "worktree", "add", "-b", branch, str(wt), base)
            if res.returncode != 0:
                raise WorkdirError(f"worktree 생성 실패: {res.stderr.strip()}")
            return str(wt), branch
        base_dir = Path(task.output_dir) if task.output_dir else paths.home() / "outputs"
        wd = base_dir / task.id / stamp
        wd.mkdir(parents=True, exist_ok=True)
        return str(wd), None

    # --- command line --------------------------------------------------------------------------
    def system_prompt(self, task: Task, workdir: str, branch: str | None) -> str:
        parts = [self.cfg.runner.append_system_prompt]
        if task.kind == "repo":
            parts.append(f"You are in a dedicated git worktree on branch `{branch}`. Commit after each meaningful "
                         "step with clear messages. Do not push and do not switch branches.")
        else:
            parts.append(f"Save every deliverable as Markdown (or other) files inside the current working "
                         f"directory ({workdir}). Finish with a short summary of what you produced.")
        if task.append_system_prompt:
            parts.append(task.append_system_prompt)
        return "\n\n".join(p for p in parts if p)

    def build_command(self, task: Task, budget_usd: float, resume_session: str | None,
                      system_prompt_file: Path) -> list[str]:
        r = self.cfg.runner
        cmd = [*self.claude_cmd, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
               "--verbose",
               "--model", task.model or r.default_model,
               "--permission-mode", task.permission_mode or r.default_permission_mode,
               "--max-turns", str(int(task.chunk_turns or r.chunk_turns)),
               "--max-budget-usd", f"{max(0.01, budget_usd):.2f}",
               "--append-system-prompt-file", str(system_prompt_file)]
        if resume_session:
            cmd += ["--resume", resume_session]
        if task.allowed_tools:
            cmd += ["--allowedTools", *task.allowed_tools]
        if task.disallowed_tools:
            cmd += ["--disallowedTools", *task.disallowed_tools]
        return cmd

    # --- one chunk -----------------------------------------------------------------------------
    def run_chunk(self, cmd: list[str], prompt: str, cwd: str, env: dict[str, str], log_path: Path,
                  stop_fn: StopFn, on_snapshot: SnapshotFn | None = None, timeout: float | None = None,
                  ) -> ChunkResult:
        r = self.cfg.runner
        res = ChunkResult()
        timeout = timeout or r.chunk_timeout_minutes * 60
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    cwd=cwd, env=env, **popen_kwargs())
        except OSError as e:
            res.error = f"실행 실패: {e}"
            return res
        events: "queue.Queue[bytes | None]" = queue.Queue()

        def pump_stdout() -> None:
            assert proc.stdout is not None
            for raw in proc.stdout:
                events.put(raw)
            events.put(None)

        stderr_path = log_path.with_suffix(".stderr.txt")

        def pump_stderr() -> None:
            assert proc.stderr is not None
            with open(stderr_path, "wb") as f:
                for raw in proc.stderr:
                    f.write(raw)

        threading.Thread(target=pump_stdout, daemon=True).start()
        threading.Thread(target=pump_stderr, daemon=True).start()

        def send(text: str) -> bool:
            try:
                assert proc.stdin is not None
                proc.stdin.write(text.encode("utf-8"))
                proc.stdin.flush()
                return True
            except (OSError, ValueError):
                return False

        def close_stdin() -> None:
            try:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()
            except OSError:
                pass

        send(user_message(prompt))
        started = time.monotonic()
        interrupt_at: float | None = None
        allowed_sources = r.allowed_api_key_sources
        got_result = False
        result_at: float | None = None
        with open(log_path, "wb") as logf:
            while True:
                try:
                    raw = events.get(timeout=0.5)
                except queue.Empty:
                    raw = b""
                if raw is None:
                    break  # stdout closed: process ended
                if raw:
                    logf.write(raw)
                    logf.flush()
                    try:
                        ev = json.loads(raw.decode("utf-8", errors="replace"))
                    except ValueError:
                        ev = None
                    if isinstance(ev, dict):
                        t = ev.get("type")
                        if t == "system" and ev.get("subtype") == "init":
                            res.session_id = ev.get("session_id") or res.session_id
                            res.api_key_source = ev.get("apiKeySource")
                            if res.api_key_source not in allowed_sources:
                                res.error = "api_key_source"
                                res.stop_reason = (f"구독이 아닌 인증({res.api_key_source}) 감지 — API 과금 방지를 "
                                                   "위해 중단")
                                send(interrupt_message(str(uuid.uuid4())))
                                close_stdin()
                                kill_tree(proc)
                                break
                        elif t == "rate_limit_event" and on_snapshot is not None:
                            snap = parse_rate_limit_event(ev, time.time(), self.cfg.usage.rate_limit_scale)
                            if snap:
                                on_snapshot(snap)
                        elif t == "result":
                            got_result = True
                            res.subtype = ev.get("subtype")
                            res.is_error = bool(ev.get("is_error"))
                            res.cost = float(ev.get("total_cost_usd") or 0.0)
                            res.turns = int(ev.get("num_turns") or 0)
                            res.session_id = ev.get("session_id") or res.session_id
                            if isinstance(ev.get("result"), str):
                                res.text = ev["result"]
                            close_stdin()  # done: let the process exit
                if got_result:
                    if result_at is None:
                        result_at = time.monotonic()
                    elif time.monotonic() - result_at > r.kill_grace_seconds:
                        kill_tree(proc)  # finished but did not exit after stdin closed
                        break
                    continue  # drain until EOF
                if interrupt_at is None:
                    reason = stop_fn()
                    if reason is None and time.monotonic() - started > timeout:
                        reason = f"청크 시간 초과 ({int(timeout // 60)}분)"
                    if reason:
                        res.interrupted = True
                        res.stop_reason = reason
                        interrupt_at = time.monotonic()
                        if not send(interrupt_message(str(uuid.uuid4()))):
                            kill_tree(proc)
                elif time.monotonic() - interrupt_at > r.kill_grace_seconds:
                    log.warning("process did not stop after interrupt; killing tree")
                    close_stdin()
                    kill_tree(proc)
                    break
        close_stdin()
        try:
            res.exit_code = proc.wait(r.kill_grace_seconds)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            res.exit_code = proc.poll()
        if not got_result and not res.error and not res.interrupted:
            res.error = f"result 없이 종료 (exit {res.exit_code}) — {stderr_path}"
        return res

    # --- a whole task --------------------------------------------------------------------------
    def run_task(self, task: Task, stop_fn: StopFn, budget_fn: BudgetFn, on_snapshot: SnapshotFn | None = None,
                 on_chunk_cost: CostFn | None = None, pct_fn: Callable[[], float | None] | None = None,
                 target: float | None = None) -> RunOutcome:
        now = self.now()
        state = self.store.task_state(task.id)
        resuming = state.get("last_status") == "partial" and bool(state.get("session_id"))
        outcome = RunOutcome(task.id, "failed", None)
        try:
            workdir, branch = self.prepare_workdir(task, state if resuming else None)
        except WorkdirError as e:
            outcome.reason = str(e)
            self._save_failure(task, outcome, now)
            return outcome
        if resuming and workdir != state.get("workdir"):
            resuming = False  # old workdir is gone; start over
        outcome.workdir, outcome.branch = workdir, branch
        log_dir = paths.runs_dir() / f"{_stamp(now)}-{task.id}"
        log_dir.mkdir(parents=True, exist_ok=True)
        outcome.log_dir = str(log_dir)
        sp_file = log_dir / "system_prompt.md"
        sp_file.write_text(self.system_prompt(task, workdir, branch), encoding="utf-8")
        outcome.pct_start = pct_fn() if pct_fn else None
        run_id = self.store.start_run(task.id, now, target, outcome.pct_start, workdir, branch, str(log_dir))
        outcome.run_id = run_id
        self.store.save_task_state(task.id, last_started_at=now, workdir=workdir, branch=branch)

        session = state.get("session_id") if resuming else None
        env = autorun_env(task.env)
        max_chunks = int(task.max_chunks or self.cfg.runner.max_chunks_per_task)
        status, reason = "partial", "최대 청크 수 도달"
        for i in range(1, max_chunks + 1):
            pre = stop_fn()
            if pre:
                status, reason = "partial", pre
                break
            budget = budget_fn()
            cmd = self.build_command(task, budget, session, sp_file)
            prompt = task.prompt if session is None else self.cfg.runner.continue_prompt
            log.info("task %s chunk %d (budget $%.2f, resume=%s)", task.id, i, budget, bool(session))
            res = self.run_chunk(cmd, prompt, workdir, env, log_dir / f"chunk-{i:02d}.jsonl", stop_fn, on_snapshot)
            outcome.chunk_results.append(res)
            outcome.chunks = i
            outcome.cost += res.cost
            outcome.turns += res.turns
            if on_chunk_cost:
                on_chunk_cost(res.cost)
            if res.session_id:
                session = res.session_id
            if res.text:
                outcome.summary = res.text
            if res.error == "api_key_source":
                status, reason = "blocked", res.stop_reason
                break
            if res.interrupted:
                status, reason = "partial", res.stop_reason
                break
            if res.subtype == "success" and not res.is_error:
                status, reason = "done", "작업 완료"
                break
            if res.subtype in ("error_max_turns", "error_max_budget_usd"):
                continue  # chunk boundary — re-check usage and continue
            status, reason = "failed", res.error or res.subtype or "알 수 없는 오류"
            break
        outcome.status, outcome.reason, outcome.session_id = status, reason, session
        outcome.pct_end = pct_fn() if pct_fn else None
        end = self.now()
        self.store.update_run(run_id, ended_at=end, status=status, reason=reason, cost_usd=outcome.cost,
                              turns=outcome.turns, chunks=outcome.chunks, session_id=session,
                              pct_end=outcome.pct_end, summary=(outcome.summary or "")[:4000])
        if status == "done":
            self.store.save_task_state(task.id, last_finished_at=end, last_status="done", last_success_at=end,
                                       fail_count=0, cooldown_until=None, session_id=None)
        elif status == "partial":
            self.store.save_task_state(task.id, last_finished_at=end, last_status="partial", session_id=session)
        else:
            self._save_failure(task, outcome, end)
        (log_dir / "outcome.json").write_text(json.dumps({
            k: v for k, v in outcome.__dict__.items() if k != "chunk_results"}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        return outcome

    def _save_failure(self, task: Task, outcome: RunOutcome, now: float) -> None:
        st = self.store.task_state(task.id)
        fails = int(st.get("fail_count") or 0) + 1
        cooldown = now + min(24 * 3600, 3 * 3600 * fails)
        self.store.save_task_state(task.id, last_finished_at=now, last_status=outcome.status, fail_count=fails,
                                   cooldown_until=cooldown, session_id=None)
