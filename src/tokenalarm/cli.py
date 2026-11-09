"""Command line interface: ``tokenalarm <command>`` (also ``python -m tokenalarm``)."""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from .i18n import L


def _fast_path(argv: list[str]) -> int | None:
    """``statusline`` and ``hook`` run on every Claude Code refresh/prompt: skip argparse/yaml/sqlite."""
    args = list(argv)
    if len(args) >= 2 and args[0] == "--home":
        os.environ["TOKENALARM_HOME"] = args[1]
        args = args[2:]
    if args and args[0] == "statusline":
        from .integrations import statusline_main

        return statusline_main()
    if len(args) >= 2 and args[0] == "hook":
        from .integrations import hook_main

        return hook_main(args[1])
    return None


def _setup_logging(verbose: bool) -> None:
    from . import paths

    root = logging.getLogger()
    if any(getattr(h, "_tokenalarm", False) for h in root.handlers):
        return
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.handlers.RotatingFileHandler(paths.logs_dir() / "tokenalarm.log", maxBytes=2_000_000,
                                              backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    fh._tokenalarm = True  # type: ignore[attr-defined]
    root.addHandler(fh)
    if sys.stderr is not None:  # pythonw / tokenalarmw have no stderr
        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG if verbose else logging.WARNING)
        ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        ch._tokenalarm = True  # type: ignore[attr-defined]
        root.addHandler(ch)


def _utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


class Ctx:
    def __init__(self, args: argparse.Namespace):
        from . import i18n, paths
        from .config import load_config
        from .store import Store

        self.args = args
        cfg_path = Path(args.config) if getattr(args, "config", None) else paths.config_file()
        self.cfg = load_config(cfg_path)
        i18n.set_lang(self.cfg.language)
        self.cfg_path = cfg_path
        self.store = Store(paths.db_file())
        self._cfg_sig = self._sig()

    def _sig(self) -> tuple:
        try:
            st = self.cfg_path.stat()
            return st.st_mtime_ns, st.st_size
        except OSError:
            return (0, 0)

    def reload_if_changed(self):
        """For the daemon: return a new Config when config.yaml changed and is valid (else None)."""
        from .config import ConfigError, load_config

        sig = self._sig()
        if sig == self._cfg_sig:
            return None
        self._cfg_sig = sig
        try:
            self.cfg = load_config(self.cfg_path)
            return self.cfg
        except (ConfigError, OSError) as e:
            logging.getLogger(__name__).warning("config reload failed: %s", e)
            self.notifier().send(L("설정 파일 오류 — 이전 설정으로 계속 실행", "Config error — keeping previous settings"),
                                 str(e), key=f"cfgerr:{sig}", dedupe_seconds=86400)
            return None

    def claude_cmd(self) -> list[str]:
        from .claude_cli import resolve_claude_cmd

        return resolve_claude_cmd(self.cfg.runner.claude_cmd)

    def usage(self):
        from .usage.chain import UsageService

        return UsageService.default(self.cfg, self.store, self.claude_cmd)

    def tasks_path(self) -> Path:
        from . import paths

        if self.cfg.tasks_file:
            p = Path(self.cfg.tasks_file).expanduser()
            return p if p.is_absolute() else self.cfg_path.parent / p
        return paths.tasks_file()

    def tasks(self):
        from .tasks import load_tasks

        return load_tasks(self.tasks_path(), self.cfg)

    def notifier(self, enabled: bool = True):
        from .notify import Notifier

        return Notifier(self.cfg.notify, self.store, enabled=enabled)

    def activity(self):
        from .activity import Activity

        return Activity(self.store, self.cfg.guards)

    def filler(self, use_activity: bool, notify: bool = True):
        from .fill import FillController
        from .runner import Runner

        return FillController(self.cfg, self.store, self.usage(), Runner(self.cfg, self.store, self.claude_cmd()),
                              self.tasks(), self.notifier(notify), self.activity() if use_activity else None)

    def daemon(self, dry_run: bool = False):
        from .daemon import Daemon

        return Daemon(self.cfg, self.store, self.usage(), lambda: self.filler(use_activity=True, notify=True),
                      self.activity(), self.notifier(not dry_run), dry_run=dry_run, reloader=self.reload_if_changed)


def _fmt_ts(ts: float | None) -> str:
    return "-" if not ts else datetime.fromtimestamp(ts).strftime("%m/%d %H:%M")


# --- everyday commands ---------------------------------------------------------------------------

def cmd_status(ctx: Ctx) -> int:
    from . import paths
    from .config import format_duration
    from .daemon import is_running, summary_text
    from .forecast import forecast_window
    from .ingest import ingest_spool
    from .policy import decide
    from .usage.base import FIVE_HOUR, SEVEN_DAY

    now = time.time()
    ingest_spool(ctx.store)
    usage = ctx.usage()
    if getattr(ctx.args, "refresh", False) or usage.age(now) is None:
        usage.refresh(now, allow_spend=bool(getattr(ctx.args, "refresh", False)))
    snap = usage.current(now)
    d = decide(snap, ctx.cfg.weekly_fill_rules, ctx.cfg.guards, now, ctx.store.get("sticky_fill"))
    week = snap.get(SEVEN_DAY) if snap else None
    five = snap.get(FIVE_HOUR) if snap else None
    wf = forecast_window(ctx.store, week, now) if week else None
    ff = forecast_window(ctx.store, five, now, recent_hours=1) if five else None
    act = ctx.activity().state(now)
    dstate = ctx.store.get("daemon_state") or {"state": "MONITOR"}
    running, _ = is_running()
    if getattr(ctx.args, "json", False):
        from dataclasses import asdict

        print(json.dumps({"week": asdict(wf) if wf else None, "five": asdict(ff) if ff else None,
                          "decision": d.reason, "target": d.target, "wants_fill": d.wants_fill,
                          "daemon": dstate, "daemon_running": running, "claude_busy": act.claude_busy,
                          "observed_at": snap.observed_at if snap else None,
                          "source": snap.source if snap else None}, ensure_ascii=False, indent=2))
        return 0
    print(L("━━ Claude 사용량 ━━", "━━ Claude usage ━━"))
    if snap is None:
        print(L("사용량 데이터가 아직 없습니다.", "No usage data yet."))
        print(L("  → Claude Code를 한 번 사용하거나 `tokenalarm status --refresh`",
                "  → use Claude Code once, or run `tokenalarm status --refresh`"))
    else:
        age = format_duration(now - snap.observed_at)
        print(L(f"관측: {_fmt_ts(snap.observed_at)} ({age} 전, {snap.source})",
                f"observed: {_fmt_ts(snap.observed_at)} ({age} ago, {snap.source})"))
        print(summary_text(wf, ff, None))
        for k, w in sorted(snap.windows.items()):
            if ":" in k:
                left = format_duration(w.seconds_left(now))
                print(L(f"{k}: {w.pct:.0f}% (리셋 {left} 후)", f"{k}: {w.pct:.0f}% (resets in {left})"))
    print(L("━━ 자동 실행 ━━", "━━ Autorun ━━"))
    print(L(f"판단: {d.reason}", f"decision: {d.reason}"))
    why = f" — {dstate.get('why')}" if dstate.get("why") else ""
    run_txt = L("실행 중", "running") if running else L("꺼져 있음", "not running")
    print(L(f"데몬: {run_txt} · 상태 {dstate.get('state')}{why}", f"daemon: {run_txt} · state {dstate.get('state')}{why}"))
    idle = (L(f", 입력 유휴 {format_duration(act.input_idle)}", f", input idle {format_duration(act.input_idle)}")
            if act.input_idle is not None else "")
    print(L(f"Claude Code 활동: {act.reason}{idle}", f"Claude Code activity: {act.reason}{idle}"))
    if paths.pause_file().exists() or (ctx.store.get("pause_until") or 0) > now:
        print(L("⏸ 일시정지 중 (tokenalarm resume 으로 해제)", "⏸ paused (tokenalarm resume to continue)"))
    ppu = ctx.store.get("pct_per_usd")
    if ppu:
        print(L(f"보정값: 비용(추정) $1 ≈ 주간 {float(ppu):.2f}%", f"calibration: est. $1 ≈ {float(ppu):.2f}% weekly"))
    return 0


def cmd_forecast(ctx: Ctx) -> int:
    from .forecast import daily_usage

    cmd_status(ctx)
    print(L("━━ 최근 일별 주간 사용량 (Δ%p) ━━", "━━ Daily weekly-usage change (Δ%pt) ━━"))
    rows = daily_usage(ctx.store, "seven_day", time.time(), days=14)
    if not rows:
        print(L("(데이터 부족)", "(not enough data)"))
    for day, delta in rows:
        print(f"{day}  {delta:5.1f}  {'█' * int(round(delta))}")
    return cmd_history(ctx, limit=5)


def cmd_history(ctx: Ctx, limit: int | None = None) -> int:
    limit = limit or getattr(ctx.args, "n", 20) or 20
    runs = ctx.store.runs(limit=limit)
    print(L("━━ 자동 실행 기록 ━━", "━━ Autorun history ━━"))
    if not runs:
        print(L("(아직 없음)", "(none yet)"))
    for r in runs:
        delta = (f"{r.pct_start:.0f}→{r.pct_end:.0f}%" if r.pct_start is not None and r.pct_end is not None else "-")
        print(f"{_fmt_ts(r.started_at)}  {r.task_id:<22} {r.status or '-':<8} {delta:<10} ${r.cost_usd or 0:.2f} "
              f"{r.turns or 0}{L('턴', ' turns')}  {r.reason or ''}")
        if r.branch or r.workdir:
            print(f"    {r.branch or ''} {r.workdir or ''}")
    return 0


def cmd_run(ctx: Ctx) -> int:
    from .fill import describe_report

    target = float(ctx.args.until)
    if not 0 < target <= 100:
        print(L("--until 은 0~100 사이 값이어야 합니다", "--until must be between 0 and 100"))
        return 2
    filler = ctx.filler(use_activity=ctx.args.respect_activity, notify=not ctx.args.dry_run)
    if not filler.tasks:
        print(L(f"작업이 없습니다: {ctx.tasks_path()} — tokenalarm add 로 추가하세요",
                f"No tasks in {ctx.tasks_path()} — add one with tokenalarm add"))
        return 1
    max_seconds = ctx.args.max_minutes * 60 if ctx.args.max_minutes else None
    report = filler.fill(target, only_task=ctx.args.task, dry_run=ctx.args.dry_run, max_seconds=max_seconds)
    print(describe_report(report))
    for o in report.outcomes:
        print(f"  - {o.task_id}: {o.status} ({o.reason}) {o.branch or ''} {o.workdir or ''}")
    return 0


def cmd_tasks(ctx: Ctx) -> int:
    from .tasks import is_due

    tasks = ctx.tasks()
    print(L(f"작업 파일: {ctx.tasks_path()}", f"task file: {ctx.tasks_path()}"))
    if not tasks:
        print(L('(작업 없음) — 예: tokenalarm add research "이번 주 AI 소식 정리"',
                '(no tasks) — e.g. tokenalarm add research "Summarize this week\'s AI news"'))
    now = time.time()
    for t in tasks:
        due, why = is_due(t, ctx.store.task_state(t.id), now)
        where = t.repo if t.kind == "repo" else (t.output_dir or L("(기본 outputs 폴더)", "(default outputs folder)"))
        prio = L(f"우선순위 {t.priority}", f"priority {t.priority}")
        print(f"{'●' if due else '○'} {t.id:<22} [{t.kind}/{t.repeat}] {prio}  {why}  → {where}")
    if getattr(ctx.args, "action", None) == "validate":
        print(L("검증 완료", "valid"))
    return 0


def cmd_add(ctx: Ctx) -> int:
    from .tasks import add_task

    a = ctx.args
    if a.kind == "repo":
        if not a.text or len(a.text) < 2:
            print(L('사용법: tokenalarm add repo <저장소 경로> "<프롬프트>"', 'usage: tokenalarm add repo <repo path> "<prompt>"'))
            return 2
        repo, prompt = a.text[0], " ".join(a.text[1:])
    else:
        repo, prompt = None, " ".join(a.text or [])
    if not prompt.strip():
        print(L("프롬프트가 필요합니다 (또는 @파일경로)", "a prompt is required (or @path/to/file.md)"))
        return 2
    tid = add_task(ctx.tasks_path(), a.kind, prompt, task_id=a.id, repo=repo, repeat=a.repeat, priority=a.priority,
                   model=a.model, output_dir=a.out, allow=a.allow, enabled=not a.disabled)
    print(L(f"추가됨: {tid}  (확인: tokenalarm tasks)", f"added: {tid}  (see: tokenalarm tasks)"))
    return 0


def cmd_remove(ctx: Ctx) -> int:
    from .tasks import remove_task, set_enabled

    if ctx.args.command == "remove":
        remove_task(ctx.tasks_path(), ctx.args.id)
        print(L(f"삭제됨: {ctx.args.id}", f"removed: {ctx.args.id}"))
    else:
        on = ctx.args.command == "enable"
        set_enabled(ctx.tasks_path(), ctx.args.id, on)
        print(L(f"{'활성화' if on else '비활성화'}: {ctx.args.id}", f"{'enabled' if on else 'disabled'}: {ctx.args.id}"))
    return 0


def cmd_pause(ctx: Ctx) -> int:
    from . import paths
    from .config import parse_duration

    if ctx.args.command == "resume":
        ctx.store.set("pause_until", None)
        if paths.pause_file().exists():
            paths.pause_file().unlink()
        print(L("자동 실행 재개", "autorun resumed"))
        return 0
    if ctx.args.duration:
        until = time.time() + parse_duration(ctx.args.duration)
        ctx.store.set("pause_until", until)
        print(L(f"{_fmt_ts(until)} 까지 자동 실행 일시정지", f"autorun paused until {_fmt_ts(until)}"))
    else:
        paths.pause_file().write_text("paused\n", encoding="utf-8")
        print(L("자동 실행 일시정지 (해제: tokenalarm resume)", "autorun paused (resume: tokenalarm resume)"))
    return 0


def cmd_notify_test(ctx: Ctx) -> int:
    ok = ctx.notifier(True).send(L("TokenAlarm 테스트 알림", "TokenAlarm test"),
                                 L("알림 채널이 정상적으로 연결되었습니다. ✅", "Notifications are working. ✅"))
    print(L("전송 성공", "sent") if ok else
          L("전송 실패 — config.yaml 의 notify 설정과 로그를 확인하세요", "failed — check notify settings and the log"))
    return 0 if ok else 1


def cmd_probe(ctx: Ctx) -> int:
    from .usage import oauth, probe

    cmd = ctx.claude_cmd()
    print(f"claude: {' '.join(cmd)}")
    s = probe.probe_usage_command(cmd, ctx.cfg.usage.rate_limit_scale)
    print(L("/usage 프로브 (무료): ", "/usage probe (free): ") + _describe_snap(s))
    if ctx.args.ping:
        s2 = probe.probe_ping(cmd, ctx.cfg.usage.ping_model, ctx.cfg.usage.rate_limit_scale,
                              allowed_key_sources=ctx.cfg.runner.allowed_api_key_sources)
        print(L("ping 프로브 (Haiku 1회): ", "ping probe (one Haiku call): ") + _describe_snap(s2))
        if s2:
            ctx.store.add_snapshot(s2)
    if ctx.args.oauth:
        try:
            s3 = oauth.fetch_oauth_usage()
        except oauth.OAuthUnavailable as e:
            s3 = None
            print(f"oauth: {e}")
        print(L("oauth (비공식): ", "oauth (unofficial): ") + _describe_snap(s3))
    if s:
        ctx.store.add_snapshot(s)
    return 0


def _describe_snap(s) -> str:
    from .config import format_duration

    if not s:
        return L("데이터 없음", "no data")
    now = time.time()
    return ", ".join(f"{k} {w.pct:.1f}% ({L('리셋', 'reset')} {format_duration(w.seconds_left(now))})"
                     for k, w in s.windows.items())


# --- background --------------------------------------------------------------------------------

def cmd_daemon(ctx: Ctx) -> int:
    from . import paths
    from .daemon import SingleInstance

    daemon = ctx.daemon(dry_run=ctx.args.dry_run)
    if ctx.args.once or ctx.args.dry_run:
        r = daemon.tick()
        print(f"[{r.state}] {r.decision}")
        print(f"→ {r.action}")
        if r.blocked_by and r.blocked_by not in r.action:
            print(L(f"  보류 사유: {r.blocked_by}", f"  on hold: {r.blocked_by}"))
        return 0
    lock = SingleInstance(paths.lock_file())
    if not lock.acquire():
        print(L("이미 실행 중인 tokenalarm 데몬이 있습니다.", "A tokenalarm daemon is already running."))
        return 1
    logging.getLogger(__name__).info("daemon started (pid %s)", os.getpid())
    try:
        daemon.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        lock.release()
    return 0


def cmd_tray(ctx: Ctx) -> int:
    try:
        from .tray import run_tray
    except ImportError:
        print(L('트레이 구성요소가 없습니다: uv tool install --reinstall "tokenalarm[tray]"',
                'Tray extra missing: uv tool install --reinstall "tokenalarm[tray]"'))
        return 1
    return run_tray(ctx)


def cmd_stop(ctx_args: argparse.Namespace) -> int:
    from .maintenance import stop_daemon

    was, stopped = stop_daemon()
    if not was:
        print(L("실행 중인 데몬이 없습니다", "no daemon running"))
    else:
        print(L("중지했습니다", "stopped") if stopped else L("중지하지 못했습니다", "could not stop it"))
    return 0 if stopped else 1


def cmd_autostart(ctx_args: argparse.Namespace) -> int:
    from . import winsvc
    from .maintenance import save_install_record

    a = ctx_args
    if getattr(a, "off", False) or a.command == "uninstall-autostart":
        msgs = winsvc.uninstall()
        save_install_record(autostart="none")
    else:
        msgs = winsvc.install(start_now=not a.dry_run, tray=a.tray, dry_run=a.dry_run)
        if not a.dry_run:
            save_install_record(autostart="tray" if a.tray else "daemon")
    for m in msgs:
        if m:
            print(m)
    return 0


# --- setup / maintenance -----------------------------------------------------------------------

def cmd_setup(ctx_args: argparse.Namespace) -> int:
    from . import setup_wizard

    return setup_wizard.run(ctx_args)


def cmd_doctor(ctx_args: argparse.Namespace) -> int:
    from . import doctor, i18n, paths
    from .config import load_config

    topic = None
    try:
        cfg = load_config(paths.config_file())
        i18n.set_lang(cfg.language)
        topic = cfg.notify.ntfy.topic
    except Exception:
        pass
    checks = doctor.run_checks(paths.config_file(), paths.tasks_file(), check_claude=not ctx_args.no_claude)
    if ctx_args.json:
        print(doctor.to_json(checks, topic))
    else:
        print(doctor.render(checks))
    return 1 if any(c.status == doctor.FAIL for c in checks) else 0


def cmd_open(ctx_args: argparse.Namespace) -> int:
    from .maintenance import open_target

    p = open_target(ctx_args.target)
    print(p)
    return 0


def cmd_clean(ctx: Ctx) -> int:
    from .maintenance import clean

    for m in clean(ctx.store, days=ctx.args.days, dry_run=ctx.args.dry_run):
        print(m)
    return 0


def cmd_uninstall(ctx_args: argparse.Namespace) -> int:
    from . import paths
    from .maintenance import uninstall

    if not ctx_args.yes:
        print(L("다음을 제거합니다: 로그온 자동 시작, Claude Code 상태줄/훅, Windows 알림 등록",
                "This removes: start-at-logon, Claude Code status line/hooks, Windows notification registration"))
        if ctx_args.all:
            print(L(f"그리고 데이터 폴더 전체({paths.home()}): 설정, 작업, 기록, 실행 로그, worktree(커밋 안 된 변경 포함). "
                    "브랜치와 커밋은 저장소에 남습니다.",
                    f"and the whole data folder ({paths.home()}): settings, tasks, history, run logs, worktrees "
                    "(including uncommitted changes). Branches and commits stay in your repositories."))
        try:
            ans = input(L("계속할까요? [y/N]: ", "Continue? [y/N]: ")).strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes", "예", "ㅇ"):
            print(L("취소했습니다", "cancelled"))
            return 1
    for m in uninstall(remove_data=ctx_args.all):
        print(m)
    return 0


def cmd_setup_claude(ctx_args: argparse.Namespace) -> int:
    from . import claude_setup

    path = Path(ctx_args.settings) if ctx_args.settings else None
    try:
        msgs = claude_setup.remove(path) if ctx_args.remove else claude_setup.install(
            path, force_statusline=ctx_args.force_statusline)
    except ValueError as e:
        print(L(f"settings.json 을 읽을 수 없어 변경하지 않았습니다: {e}", f"settings.json unreadable — not changed: {e}"))
        return 1
    for m in msgs:
        print(m)
    if not ctx_args.remove:
        print(L("Claude Code를 재시작하면 적용됩니다.", "Restart Claude Code to apply."))
    return 0


def cmd_init(ctx_args: argparse.Namespace) -> int:
    """Legacy (v0.1): copy the full example files. `tokenalarm setup` is the recommended path."""
    import shutil

    from . import paths

    ex = paths.examples_dir()
    for name, dst in (("config.example.yaml", paths.config_file()), ("tasks.example.yaml", paths.tasks_file())):
        if not dst.exists():
            shutil.copy2(ex / name, dst)
            print(dst)
    pdir = paths.home() / "prompts"
    pdir.mkdir(exist_ok=True)
    for p in (ex / "prompts").glob("*.md"):
        if not (pdir / p.name).exists():
            shutil.copy2(p, pdir / p.name)
    print(L(f"tokenalarm 폴더: {paths.home()}", f"tokenalarm folder: {paths.home()}"))
    return 0


# --- parser --------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    from . import __version__
    from .setup_wizard import add_parser_args

    hidden = argparse.SUPPRESS
    p = argparse.ArgumentParser(
        prog="tokenalarm",
        description=L("Claude 사용량 예측 알림 + 리셋 전 남는 사용량으로 지정 작업 자동 실행",
                      "Claude usage forecast + spend leftover quota on your tasks before it resets"),
        epilog=L("처음이라면: tokenalarm setup", "First time? Run: tokenalarm setup"))
    p.add_argument("--version", action="version", version=f"tokenalarm {__version__}")
    p.add_argument("--home", help=hidden)
    p.add_argument("--config", help=hidden)
    p.add_argument("-v", "--verbose", action="store_true", help=hidden)
    sub = p.add_subparsers(dest="command", metavar="<command>")

    add_parser_args(sub.add_parser("setup", help=L("처음 설정 (마법사)", "guided first-time setup")))
    s = sub.add_parser("status", help=L("현재 사용량·예측·자동 실행 판단", "usage, forecast and autorun decision"))
    s.add_argument("--json", action="store_true")
    s.add_argument("--refresh", action="store_true", help=L("지금 다시 읽기 (필요 시 Haiku 1회)", "re-read now"))
    ad = sub.add_parser("add", help=L("작업 추가", "add a task"))
    ad.add_argument("kind", choices=["research", "study", "repo"])
    ad.add_argument("text", nargs="+", help=L('"프롬프트" 또는 @파일 (repo: <경로> "프롬프트")',
                                              '"prompt" or @file (repo: <path> "prompt")'))
    ad.add_argument("--id")
    ad.add_argument("--repeat", choices=["once", "daily", "weekly", "always"], default="weekly")
    ad.add_argument("--priority", type=int, default=0)
    ad.add_argument("--model")
    ad.add_argument("--out", help=L("결과 폴더 (research/study)", "output folder (research/study)"))
    ad.add_argument("--allow", action="append", help=L('추가로 허용할 도구, 예: "Bash(npm test:*)"',
                                                       'extra allowed tool, e.g. "Bash(npm test:*)"'))
    ad.add_argument("--disabled", action="store_true")
    t = sub.add_parser("tasks", help=L("작업 목록", "list tasks"))
    t.add_argument("action", nargs="?", choices=["list", "validate"], default="list")
    for name, h in (("remove", L("작업 삭제", "remove a task")), ("enable", L("작업 켜기", "enable a task")),
                    ("disable", L("작업 끄기", "disable a task"))):
        sub.add_parser(name, help=h).add_argument("id")
    r = sub.add_parser("run", help=L("지금 바로 목표 %%까지 실행", "run tasks now until a target %%"))
    r.add_argument("--until", required=True, help=L("목표 주간 사용률 %%", "target weekly %%"))
    r.add_argument("--task")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--max-minutes", type=float)
    r.add_argument("--respect-activity", action="store_true")
    pa = sub.add_parser("pause", help=L("자동 실행 일시정지", "pause autorun"))
    pa.add_argument("--for", dest="duration", help=L("예: 6h, 2d", "e.g. 6h, 2d"))
    sub.add_parser("resume", help=L("일시정지 해제", "resume autorun"))
    dr = sub.add_parser("doctor", help=L("문제 진단", "diagnose problems"))
    dr.add_argument("--json", action="store_true", help=L("버그 리포트용 (민감 정보 가림)", "for bug reports (redacted)"))
    dr.add_argument("--no-claude", action="store_true", help=hidden)
    op = sub.add_parser("open", help=L("폴더/설정 열기", "open folder/config"))
    op.add_argument("target", nargs="?", default="data", choices=["data", "config", "tasks", "logs", "runs"])
    f = sub.add_parser("forecast", help=L("상세 예측 + 일별 사용량", "detailed forecast + daily usage"))
    f.add_argument("--json", action="store_true")
    f.add_argument("--refresh", action="store_true")
    h = sub.add_parser("history", help=L("자동 실행 기록", "autorun history"))
    h.add_argument("-n", type=int, default=20)
    au = sub.add_parser("autostart", help=L("로그온 시 자동 시작 켜기/끄기", "start at logon on/off"))
    au.add_argument("--tray", action="store_true")
    au.add_argument("--off", action="store_true")
    au.add_argument("--dry-run", action="store_true")
    sub.add_parser("stop", help=L("실행 중인 데몬/트레이 종료", "stop the running daemon/tray"))
    cl = sub.add_parser("clean", help=L("끝난 worktree·오래된 로그 정리", "remove finished worktrees/old logs"))
    cl.add_argument("--days", type=float, default=14)
    cl.add_argument("--dry-run", action="store_true")
    un = sub.add_parser("uninstall", help=L("자동 시작·Claude 연동 제거 (--all: 데이터까지)",
                                            "remove autostart + Claude integration (--all: data too)"))
    un.add_argument("--all", action="store_true")
    un.add_argument("--yes", "-y", action="store_true")
    sub.add_parser("notify-test", help=L("알림 테스트", "send a test notification"))
    sub.add_parser("tray", help=L("트레이 아이콘 실행", "run the tray icon"))
    # internal / legacy (no help= → not listed in --help)
    d = sub.add_parser("daemon")
    d.add_argument("--once", action="store_true")
    d.add_argument("--dry-run", action="store_true")
    pr = sub.add_parser("probe")
    pr.add_argument("--ping", action="store_true")
    pr.add_argument("--oauth", action="store_true")
    sc = sub.add_parser("setup-claude")
    sc.add_argument("--remove", action="store_true")
    sc.add_argument("--force-statusline", action="store_true")
    sc.add_argument("--settings")
    ins = sub.add_parser("install")  # v0.1 name of `autostart`
    ins.add_argument("--tray", action="store_true")
    ins.add_argument("--dry-run", action="store_true")
    sub.add_parser("init")
    sub.add_parser("statusline")
    sub.add_parser("hook").add_argument("event")
    return p


NO_CTX = {"setup", "doctor", "open", "stop", "autostart", "install", "uninstall", "setup-claude", "init"}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    fast = _fast_path(argv)
    if fast is not None:
        return fast
    _utf8_stdout()
    if "--home" in argv:
        i = argv.index("--home")
        if i + 1 < len(argv):
            os.environ["TOKENALARM_HOME"] = argv[i + 1]
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    _setup_logging(args.verbose)
    handlers_no_ctx = {
        "setup": cmd_setup, "doctor": cmd_doctor, "open": cmd_open, "stop": cmd_stop, "autostart": cmd_autostart,
        "install": lambda a: cmd_autostart(argparse.Namespace(**{**vars(a), "off": False})),
        "uninstall": cmd_uninstall, "setup-claude": cmd_setup_claude, "init": cmd_init,
    }
    from .tasks import TaskError

    if args.command in NO_CTX:
        try:
            return handlers_no_ctx[args.command](args)
        except TaskError as e:
            print(L(f"오류: {e}", f"error: {e}"))
            return 1
    try:
        ctx = Ctx(args)
    except (ValueError, OSError) as e:
        print(L(f"설정 오류: {e}\n  → tokenalarm open config  또는  tokenalarm doctor",
                f"config error: {e}\n  → tokenalarm open config  or  tokenalarm doctor"))
        return 2
    handlers = {
        "status": cmd_status, "forecast": cmd_forecast, "history": cmd_history, "probe": cmd_probe,
        "run": cmd_run, "daemon": cmd_daemon, "tasks": cmd_tasks, "pause": cmd_pause, "resume": cmd_pause,
        "notify-test": cmd_notify_test, "add": cmd_add, "remove": cmd_remove, "enable": cmd_remove,
        "disable": cmd_remove, "clean": cmd_clean, "tray": cmd_tray,
    }
    try:
        return handlers[args.command](ctx)
    except Exception as e:
        from .claude_cli import ClaudeNotFound

        if isinstance(e, (ClaudeNotFound, TaskError)):
            print(L(f"오류: {e}", f"error: {e}"))
            return 1
        raise
    finally:
        ctx.store.close()


def main_gui() -> int:
    """``tokenalarmw`` (no console window): used by the logon task. Defaults to the daemon."""
    argv = sys.argv[1:] or ["daemon"]
    try:
        return main(argv)
    except SystemExit as e:
        return int(e.code or 0)
    except Exception:
        logging.getLogger(__name__).exception("tokenalarmw crashed")
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
