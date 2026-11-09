"""Task queue (tasks.yaml): what to run with leftover quota."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config import Config
from .i18n import L
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
            raise TaskError(L(f"tasks[{i}]: id가 필요합니다", f"tasks[{i}]: id is required"))
        if not all(c.isalnum() or c in "-_." for c in tid):
            raise TaskError(L(f"{tid}: id에는 영문/숫자/-_. 만 사용하세요", f"{tid}: id may only contain letters, digits, -_."))
        if tid in seen:
            raise TaskError(L(f"{tid}: 중복된 id", f"{tid}: duplicate id"))
        seen.add(tid)
        kind = merged.get("kind", "research")
        if kind not in KINDS:
            raise TaskError(L(f"{tid}: kind는 {KINDS} 중 하나", f"{tid}: kind must be one of {KINDS}"))
        repeat = merged.get("repeat", "once")
        if repeat not in REPEATS:
            raise TaskError(L(f"{tid}: repeat는 {REPEATS} 중 하나", f"{tid}: repeat must be one of {REPEATS}"))
        prompt = merged.get("prompt")
        if merged.get("prompt_file"):
            pf = Path(str(merged["prompt_file"])).expanduser()
            if not pf.is_absolute():
                pf = path.parent / pf
            if not pf.exists():
                raise TaskError(L(f"{tid}: prompt_file 없음: {pf}", f"{tid}: prompt_file not found: {pf}"))
            prompt = pf.read_text(encoding="utf-8-sig")
        if not prompt or not str(prompt).strip():
            raise TaskError(L(f"{tid}: prompt 또는 prompt_file이 필요합니다", f"{tid}: prompt or prompt_file is required"))
        repo = merged.get("repo")
        if kind == "repo":
            if not repo:
                raise TaskError(L(f"{tid}: kind=repo에는 repo 경로가 필요합니다", f"{tid}: kind=repo needs a repo path"))
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
        return False, L("비활성화됨", "disabled")
    if int(state.get("fail_count") or 0) >= MAX_FAILURES:
        return False, L(f"{MAX_FAILURES}회 연속 실패로 보류 (tasks.yaml/로그 확인)", f"on hold after {MAX_FAILURES} failures (check tasks.yaml/logs)")
    cooldown = state.get("cooldown_until")
    if cooldown and now < cooldown:
        return False, L("실패 후 쿨다운 중", "cooling down after a failure")
    status = state.get("last_status")
    if status == "partial":
        return True, L("이전 실행 이어하기", "resume previous run")
    last_ok = state.get("last_success_at")
    if task.repeat == "once":
        return (last_ok is None), (L("이미 완료", "already done") if last_ok else L("대기 중", "waiting"))
    if task.repeat == "always" or last_ok is None:
        return True, L("대기 중", "waiting")
    if task.repeat == "daily":
        ok = now - last_ok >= 20 * 3600
        return ok, L("대기 중", "waiting") if ok else L("오늘 이미 실행", "already ran today")
    if task.repeat == "weekly":
        if week_start is not None:
            ok = last_ok < week_start
        else:
            ok = now - last_ok >= 6 * 86400
        return ok, L("대기 중", "waiting") if ok else L("이번 주 이미 실행", "already ran this week")
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


# --- editing tasks.yaml from the CLI (`tokenalarm add/remove/enable/disable`) ---------------------

def _header() -> str:
    return L(
        "# TokenAlarm 작업 큐 — `tokenalarm add/remove/enable/disable` 로 관리하거나 직접 편집하세요.\n"
        "# (CLI로 수정하면 이 파일의 주석은 다시 만들어집니다. 원본은 tasks.yaml.bak 에 보관)\n"
        "# kind: repo(git worktree에서 코드 작업) | research(조사·보고서) | study(학습 노트)\n"
        "# repeat: once | daily | weekly | always   priority: 높을수록 먼저\n",
        "# TokenAlarm task queue — manage with `tokenalarm add/remove/enable/disable` or edit by hand.\n"
        "# (CLI edits regenerate these comments; the previous file is kept as tasks.yaml.bak)\n"
        "# kind: repo (code work in a git worktree) | research (reports) | study (notes)\n"
        "# repeat: once | daily | weekly | always   priority: higher runs first\n",
    )


def read_raw(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {"tasks": []}
    with open(path, encoding="utf-8-sig") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise TaskError(L(f"{path}: 최상위는 매핑이어야 합니다", f"{path}: top level must be a mapping"))
    raw.setdefault("tasks", [])
    if raw["tasks"] is None:
        raw["tasks"] = []
    return raw


def write_raw(path: Path, raw: dict[str, Any]) -> None:
    """Validate, back up, and atomically replace tasks.yaml."""
    import os
    import shutil
    import tempfile

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False, default_flow_style=False, width=120)
    fd, tmp = tempfile.mkstemp(prefix=".tasks.", suffix=".yaml", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(_header() + "\n" + body)
    try:
        load_tasks(Path(tmp))  # never write an invalid queue (prompt_file paths resolve from the same folder)
        if path.exists():
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                time.sleep(0.2 * (attempt + 1))
        else:
            raise PermissionError(str(path))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def make_id(kind: str, text: str, existing: set[str]) -> str:
    import re

    words = re.findall(r"[a-zA-Z0-9]+", text.lower())[:3]
    base = "-".join([kind, *words])[:40].strip("-") or kind
    tid, n = base, 2
    while tid in existing:
        tid, n = f"{base}-{n}", n + 1
    return tid


def add_task(path: Path, kind: str, prompt: str, *, task_id: str | None = None, repo: str | None = None,
             repeat: str = "weekly", priority: int = 0, model: str | None = None, output_dir: str | None = None,
             allow: list[str] | None = None, enabled: bool = True) -> str:
    raw = read_raw(path)
    existing = {str(t.get("id")) for t in raw["tasks"] if isinstance(t, dict)}
    tid = task_id or make_id(kind, prompt if not prompt.startswith("@") else Path(prompt[1:]).stem, existing)
    if tid in existing:
        raise TaskError(L(f"{tid}: 이미 있는 id", f"{tid}: id already exists"))
    item: dict[str, Any] = {"id": tid, "kind": kind}
    if kind == "repo":
        if not repo:
            raise TaskError(L("repo 작업에는 저장소 경로가 필요합니다", "a repo task needs a repository path"))
        rp = Path(repo).expanduser().resolve()
        if not (rp / ".git").exists():
            raise TaskError(L(f"git 저장소가 아닙니다: {rp}", f"not a git repository: {rp}"))
        item["repo"] = str(rp).replace("\\", "/")
    if prompt.startswith("@"):
        pf = Path(prompt[1:]).expanduser().resolve()
        if not pf.exists():
            raise TaskError(L(f"프롬프트 파일 없음: {pf}", f"prompt file not found: {pf}"))
        item["prompt_file"] = str(pf).replace("\\", "/")
    else:
        item["prompt"] = prompt
    item["repeat"] = repeat
    item["priority"] = int(priority)
    if model:
        item["model"] = model
    if output_dir:
        item["output_dir"] = output_dir
    if allow:
        item["allowed_tools"] = list(DEFAULT_TOOLS[kind]) + [a for a in allow if a not in DEFAULT_TOOLS[kind]]
    if not enabled:
        item["enabled"] = False
    raw["tasks"].append(item)
    write_raw(path, raw)
    return tid


def _find(raw: dict[str, Any], task_id: str) -> int:
    for i, t in enumerate(raw["tasks"]):
        if isinstance(t, dict) and str(t.get("id")) == task_id:
            return i
    raise TaskError(L(f"작업을 찾을 수 없음: {task_id}", f"no such task: {task_id}"))


def remove_task(path: Path, task_id: str) -> None:
    raw = read_raw(path)
    del raw["tasks"][_find(raw, task_id)]
    write_raw(path, raw)


def set_enabled(path: Path, task_id: str, enabled: bool) -> None:
    raw = read_raw(path)
    item = raw["tasks"][_find(raw, task_id)]
    if enabled:
        item.pop("enabled", None)
    else:
        item["enabled"] = False
    write_raw(path, raw)
