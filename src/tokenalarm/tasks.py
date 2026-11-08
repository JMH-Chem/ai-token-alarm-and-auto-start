"""Task queue (tasks.yaml): what to run with leftover quota."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config import Config
from .store import Store

KINDS = ("repo", "research", "study")
MAX_FAILURES = 5
REPEATS = ("once", "daily", "weekly", "always")

DEFAULT_TOOLS = {
    "repo": ["Read", "Edit", "Write", "Glob", "Grep", "Bash(git status:*)", "Bash(git diff:*)",
             "Bash(git add:*)", "Bash(git commit:*)", "Bash(git log:*)"],
    "research": ["Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch"],
    "study": ["Read", "Write", "Edit", "Glob", "Grep"],
}
ALWAYS_DISALLOWED = ["Bash(git push:*)", "Bash(git remote:*)", "Bash(rm -rf:*)", "Bash(git reset --hard:*)"]


@dataclass
class Task:
    id: str
    kind: str
    prompt: str
    priority: int = 0
    repeat: str = "once"
    enabled: bool = True
    repo: str | None = None
    base_ref: str | None = None
    output_dir: str | None = None
    model: str | None = None
    permission_mode: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    max_chunks: int | None = None
    chunk_turns: int | None = None
    append_system_prompt: str | None = None
    env: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.allowed_tools:
            self.allowed_tools = list(DEFAULT_TOOLS.get(self.kind, []))
        self.disallowed_tools = list(dict.fromkeys([*self.disallowed_tools, *ALWAYS_DISALLOWED]))


class TaskError(ValueError):
    pass


def load_tasks(path: Path, cfg: Config | None = None) -> list[Task]:
    path = Path(path)
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig") as f:
        raw = yaml.safe_load(f) or {}
    defaults: dict[str, Any] = raw.get("defaults") or {}
    tasks: list[Task] = []
    seen: set[str] = set()
    for i, item in enumerate(raw.get("tasks") or []):
        merged = {**defaults, **(item or {})}
        tid = str(merged.get("id") or "").strip()
        if not tid:
            raise TaskError(f"tasks[{i}]: id가 필요합니다")
        if not all(c.isalnum() or c in "-_." for c in tid):
            raise TaskError(f"{tid}: id에는 영문/숫자/-_. 만 사용하세요")
        if tid in seen:
            raise TaskError(f"{tid}: 중복된 id")
        seen.add(tid)
        kind = merged.get("kind", "research")
        if kind not in KINDS:
            raise TaskError(f"{tid}: kind는 {KINDS} 중 하나")
        repeat = merged.get("repeat", "once")
        if repeat not in REPEATS:
            raise TaskError(f"{tid}: repeat는 {REPEATS} 중 하나")
        prompt = merged.get("prompt")
        if merged.get("prompt_file"):
            pf = Path(str(merged["prompt_file"])).expanduser()
            if not pf.is_absolute():
                pf = path.parent / pf
            if not pf.exists():
                raise TaskError(f"{tid}: prompt_file 없음: {pf}")
            prompt = pf.read_text(encoding="utf-8-sig")
        if not prompt or not str(prompt).strip():
            raise TaskError(f"{tid}: prompt 또는 prompt_file이 필요합니다")
        repo = merged.get("repo")
        if kind == "repo":
            if not repo:
                raise TaskError(f"{tid}: kind=repo에는 repo 경로가 필요합니다")
            repo = str(Path(str(repo)).expanduser())
        output_dir = merged.get("output_dir")
        if output_dir:
            output_dir = str(Path(str(output_dir)).expanduser())
        allowed = merged.get("allowed_tools") or DEFAULT_TOOLS[kind]
        disallowed = list(dict.fromkeys([*(merged.get("disallowed_tools") or []), *ALWAYS_DISALLOWED]))
        tasks.append(Task(
            id=tid, kind=kind, prompt=str(prompt).strip(), priority=int(merged.get("priority", 0)),
            repeat=repeat, enabled=bool(merged.get("enabled", True)), repo=repo,
            base_ref=merged.get("base_ref"), output_dir=output_dir, model=merged.get("model"),
            permission_mode=merged.get("permission_mode"), allowed_tools=list(allowed),
            disallowed_tools=disallowed, max_chunks=merged.get("max_chunks"),
            chunk_turns=merged.get("chunk_turns") or merged.get("max_turns"),
            append_system_prompt=merged.get("append_system_prompt"),
            env={str(k): str(v) for k, v in (merged.get("env") or {}).items()},
        ))
    return tasks


def is_due(task: Task, state: dict[str, Any], now: float, week_start: float | None = None) -> tuple[bool, str]:
    if not task.enabled:
        return False, "비활성화됨"
    if int(state.get("fail_count") or 0) >= MAX_FAILURES:
        return False, f"{MAX_FAILURES}회 연속 실패로 보류 (tasks.yaml/로그 확인)"
    cooldown = state.get("cooldown_until")
    if cooldown and now < cooldown:
        return False, "실패 후 쿨다운 중"
    status = state.get("last_status")
    if status == "partial":
        return True, "이전 실행 이어하기"
    last_ok = state.get("last_success_at")
    if task.repeat == "once":
        return (last_ok is None), ("이미 완료" if last_ok else "대기 중")
    if task.repeat == "always" or last_ok is None:
        return True, "대기 중"
    if task.repeat == "daily":
        ok = now - last_ok >= 20 * 3600
        return ok, "대기 중" if ok else "오늘 이미 실행"
    if task.repeat == "weekly":
        if week_start is not None:
            ok = last_ok < week_start
        else:
            ok = now - last_ok >= 6 * 86400
        return ok, "대기 중" if ok else "이번 주 이미 실행"
    return False, "?"


def select_task(tasks: list[Task], store: Store, now: float | None = None, week_start: float | None = None,
                exclude: set[str] | None = None, only: str | None = None) -> Task | None:
    now = time.time() if now is None else now
    candidates = []
    for t in tasks:
        if only and t.id != only:
            continue
        if exclude and t.id in exclude:
            continue
        due, _ = is_due(t, store.task_state(t.id), now, week_start)
        if due or (only and t.enabled):
            state = store.task_state(t.id)
            resume_first = 1 if state.get("last_status") == "partial" else 0
            candidates.append((resume_first, t.priority, t))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    return candidates[0][2]
