"""`tokenalarm doctor`: check every moving part and say how to fix what is wrong."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import __version__, paths
from .config import ConfigError, format_duration, load_config
from .i18n import L

OK, WARN, FAIL = "ok", "warn", "fail"
ICON = {OK: "✅", WARN: "⚠️ ", FAIL: "❌"}


@dataclass
class Check:
    id: str
    status: str
    title: str
    detail: str = ""
    fix: str = ""


def _run(cmd: list[str] | str, timeout: float = 20, stdin: str = "", shell: bool = False,
         env: dict | None = None) -> subprocess.CompletedProcess | None:
    from .claude_cli import popen_kwargs

    try:
        kw = {} if shell else popen_kwargs()
        return subprocess.run(cmd, input=stdin.encode("utf-8"), capture_output=True, timeout=timeout, shell=shell,
                              env=env, **kw)
    except (OSError, subprocess.TimeoutExpired):
        return None


def run_checks(config_path: Path, tasks_path: Path, check_claude: bool = True) -> list[Check]:
    from . import claude_setup, winsvc
    from .daemon import is_running
    from .store import Store
    from .tasks import TaskError, load_tasks

    checks: list[Check] = []
    add = checks.append
    v = sys.version_info
    add(Check("python", OK if v >= (3, 10) else FAIL, f"Python {v.major}.{v.minor}.{v.micro}",
              f"tokenalarm {__version__} · {sys.executable}"))
    add(Check("home", OK, L("데이터 폴더", "Data folder"), str(paths.home())))

    cfg = None
    try:
        cfg = load_config(config_path)
        if config_path.exists():
            add(Check("config", OK, L("설정 파일", "Config file"),
                      f"{config_path} (preset {cfg.preset}, language {cfg.language})"))
        else:
            add(Check("config", WARN, L("설정 파일 없음 (기본값 사용)", "No config file (defaults in use)"),
                      str(config_path), "tokenalarm setup"))
    except (ConfigError, OSError) as e:
        add(Check("config", FAIL, L("설정 파일 오류", "Config file error"), str(e), "tokenalarm open config"))

    try:
        tasks = load_tasks(tasks_path)
        enabled = [t for t in tasks if t.enabled]
        if enabled:
            add(Check("tasks", OK, L(f"작업 {len(enabled)}개 활성", f"{len(enabled)} task(s) enabled"),
                      ", ".join(t.id for t in enabled)))
        else:
            add(Check("tasks", WARN, L("활성화된 작업 없음 — 남는 사용량을 쓸 일이 없습니다",
                                       "No enabled tasks — nothing to spend leftover quota on"),
                      str(tasks_path), 'tokenalarm add research "..."'))
        if any(t.kind == "repo" for t in enabled) and not shutil.which("git"):
            add(Check("git", FAIL, L("git 이 없습니다 (repo 작업에 필요)", "git not found (needed for repo tasks)"),
                      "", "https://git-scm.com/download/win"))
    except TaskError as e:
        add(Check("tasks", FAIL, L("작업 파일 오류", "Task file error"), str(e), "tokenalarm open tasks"))

    # --- Claude Code CLI + auth ------------------------------------------------------------------
    claude_cmd = None
    try:
        from .claude_cli import resolve_claude_cmd

        claude_cmd = resolve_claude_cmd(cfg.runner.claude_cmd if cfg else None)
        res = _run([*claude_cmd, "--version"], timeout=30)
        ver = (res.stdout.decode("utf-8", "replace").strip() if res and res.returncode == 0 else "?")
        add(Check("claude", OK, "Claude Code CLI", f"{' '.join(claude_cmd)} · {ver}"))
    except Exception as e:
        add(Check("claude", FAIL, L("Claude Code CLI 없음", "Claude Code CLI not found"), str(e),
                  "https://code.claude.com/docs/en/setup"))
    if claude_cmd and check_claude:
        from .claude_cli import autorun_env, run_stream

        events = run_stream([*claude_cmd, "-p", "/usage", "--output-format", "stream-json", "--verbose",
                             "--no-session-persistence"], None, autorun_env(), 60)
        init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), None)
        allowed = cfg.runner.allowed_api_key_sources if cfg else ["none"]
        if init is None:
            add(Check("auth", FAIL, L("Claude Code가 응답하지 않음", "Claude Code did not respond"), "",
                      L("터미널에서 `claude` 를 한 번 실행해 로그인하세요", "Run `claude` once in a terminal and log in")))
        elif init.get("apiKeySource") in allowed:
            add(Check("auth", OK, L("구독 로그인", "Subscription login"), f"apiKeySource={init.get('apiKeySource')}"))
        else:
            add(Check("auth", FAIL, L("API 키 인증 — 자동 실행이 차단됩니다", "API-key auth — autorun is blocked"),
                      f"apiKeySource={init.get('apiKeySource')}",
                      L("`claude` 에서 /login 으로 구독 계정에 로그인하세요", "Use /login in `claude` with your subscription")))
    if os.environ.get("ANTHROPIC_API_KEY"):
        add(Check("apikey_env", WARN, L("ANTHROPIC_API_KEY 환경변수가 설정됨", "ANTHROPIC_API_KEY is set"),
                  L("자동 실행에서는 제거하지만, 대화형 Claude Code는 API로 과금될 수 있습니다",
                    "tokenalarm strips it for autoruns, but interactive Claude Code may bill the API")))

    # --- Claude Code settings (status line + hooks) -----------------------------------------------
    st = claude_setup.status()
    if not st["ok"]:
        add(Check("settings", FAIL, L("settings.json 을 읽을 수 없음", "Cannot read settings.json"), st.get("error", ""),
                  str(claude_setup.settings_path())))
    else:
        missing = [e for e, _ in claude_setup.HOOK_SPECS if e not in st["hooks"]]
        if missing:
            add(Check("hooks", WARN, L("활동 감지 훅 미등록", "Activity hooks not registered"), ", ".join(missing),
                      "tokenalarm setup-claude"))
        else:
            broken = []
            for ev, entries in st["entries"].items():
                for e in entries:
                    for h in e.get("hooks") or []:
                        cmd = h.get("command", "")
                        if h.get("args") is not None and not Path(cmd).exists():
                            broken.append(f"{ev}: {cmd}")
                        elif h.get("args") is not None:
                            r = _run([cmd, *[a if a not in dict(claude_setup.HOOK_SPECS) else "DoctorPing"
                                             for a in h["args"]]], timeout=15)
                            if r is None or r.returncode != 0:
                                broken.append(f"{ev}: exit {None if r is None else r.returncode}")
            if broken:
                add(Check("hooks", FAIL, L("훅 명령이 실행되지 않음", "Hook command does not run"), "; ".join(broken[:3]),
                          "tokenalarm setup --repair"))
            else:
                add(Check("hooks", OK, L("활동 감지 훅", "Activity hooks"), ", ".join(st["hooks"])))
        sl = st.get("statusline")
        if not sl:
            add(Check("statusline", WARN, L("상태줄 미등록 (사용량 수집이 느려짐)", "Status line not registered "
                                                                       "(slower usage updates)"), "",
                      "tokenalarm setup-claude"))
        elif not st["statusline_ours"]:
            add(Check("statusline", WARN, L("다른 상태줄 사용 중", "Another status line is in use"),
                      str(sl.get("command")), "tokenalarm setup-claude --force-statusline"))
        else:
            cmd = str(sl.get("command", ""))
            r = _run(cmd, timeout=15, stdin="{}", shell=True)
            if r is None or r.returncode != 0:
                add(Check("statusline", FAIL, L("상태줄 명령이 실행되지 않음", "Status line command fails"), cmd,
                          "tokenalarm setup --repair"))
            elif cmd.startswith('"'):
                add(Check("statusline", WARN, L("상태줄 경로에 공백이 있음 (PowerShell에서 실패할 수 있음)",
                                                "Status line path has spaces (may fail under PowerShell)"), cmd))
            else:
                add(Check("statusline", OK, L("상태줄", "Status line"), cmd))

    # --- usage data ----------------------------------------------------------------------------
    store = Store(paths.db_file())
    try:
        from .ingest import ingest_spool

        ingest_spool(store)
        got = store.latest_window("seven_day", time.time())
        if got is None:
            add(Check("usage", WARN, L("사용량 데이터 없음", "No usage data yet"), "",
                      L("Claude Code를 한 번 사용하거나 `tokenalarm status --refresh`",
                        "Use Claude Code once or run `tokenalarm status --refresh`")))
        else:
            w, obs, src = got
            age = time.time() - obs
            status = OK if age < 6 * 3600 else WARN
            add(Check("usage", status, L(f"주간 {w.pct:.0f}% ({format_duration(age)} 전, {src})",
                                         f"weekly {w.pct:.0f}% ({format_duration(age)} ago, {src})"),
                      L(f"리셋 {format_duration(w.seconds_left(time.time()))} 후",
                        f"resets in {format_duration(w.seconds_left(time.time()))}")))
        paused = paths.pause_file().exists() or (store.get("pause_until") or 0) > time.time()
        if paused:
            add(Check("pause", WARN, L("자동 실행 일시정지 중", "Autorun is paused"), "", "tokenalarm resume"))
    finally:
        store.close()

    # --- daemon / autostart / notifications ------------------------------------------------------
    running, pid = is_running()
    installed = winsvc.is_installed()
    if running:
        add(Check("daemon", OK, L("데몬 실행 중", "Daemon running"), f"pid {pid}"))
    else:
        add(Check("daemon", WARN, L("데몬이 실행 중이 아님", "Daemon not running"), "",
                  "tokenalarm autostart" if not installed else "schtasks /Run /TN TokenAlarm"))
    if installed is not None:
        add(Check("autostart", OK if installed else WARN,
                  L("로그온 시 자동 시작", "Start at logon") + (" ✓" if installed else L(" 미등록", " not set")),
                  "", "" if installed else "tokenalarm autostart"))
    if cfg is not None:
        n = cfg.notify
        chans = (["desktop"] if n.desktop else []) + (["ntfy"] if n.ntfy.topic else []) + \
            [f"{h.type} webhook" for h in n.webhooks]
        add(Check("notify", OK if chans else WARN, L("알림 채널", "Notification channels"),
                  ", ".join(chans) or L("없음", "none"), "" if chans else "tokenalarm setup"))
    return checks


# --- output --------------------------------------------------------------------------------------

def redact(text: str, cfg_topic: str | None = None) -> str:
    home = str(Path.home())
    out = text.replace(home, "~").replace(home.replace("\\", "/"), "~")
    user = os.environ.get("USERNAME") or os.environ.get("USER")
    if user and len(user) > 2:
        out = re.sub(re.escape(user), "<user>", out, flags=re.I)
    out = re.sub(r"(https?://[^/\s]+)/\S+", r"\1/<redacted>", out)
    if cfg_topic:
        out = out.replace(cfg_topic, "<ntfy-topic>")
    return out


def render(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        line = f"{ICON[c.status]} {c.title}"
        if c.detail:
            line += f" — {c.detail}"
        lines.append(line)
        if c.fix and c.status != OK:
            lines.append(f"     → {c.fix}")
    fails = sum(c.status == FAIL for c in checks)
    warns = sum(c.status == WARN for c in checks)
    lines.append("")
    if fails:
        lines.append(L(f"문제 {fails}개, 주의 {warns}개", f"{fails} problem(s), {warns} warning(s)"))
    elif warns:
        lines.append(L(f"동작 가능 (주의 {warns}개)", f"Working ({warns} warning(s))"))
    else:
        lines.append(L("모두 정상 ✅", "All good ✅"))
    return "\n".join(lines)


def to_json(checks: list[Check], topic: str | None = None) -> str:
    data = {"version": __version__, "platform": sys.platform, "checks": [asdict(c) for c in checks]}
    return redact(json.dumps(data, ensure_ascii=False, indent=2), topic)
