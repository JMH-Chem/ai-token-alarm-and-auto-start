"""Command line interface: ``tokenalarm <command>`` (also ``python -m tokenalarm``)."""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path


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


def _setup_logging(verbose: bool, console: bool = True) -> None:
    from . import paths

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.handlers.RotatingFileHandler(paths.logs_dir() / "tokenalarm.log", maxBytes=2_000_000,
                                              backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(fh)
    if console and sys.stderr is not None:  # pythonw has no stderr
        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG if verbose else logging.WARNING)
        ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
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
        from . import paths
        from .config import load_config
        from .store import Store

        self.args = args
        cfg_path = Path(args.config) if args.config else paths.config_file()
        self.cfg = load_config(cfg_path)
        self.cfg_path = cfg_path
        self.store = Store(paths.db_file())

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


# --- commands ------------------------------------------------------------------------------------

def cmd_init(ctx_args: argparse.Namespace) -> int:
    from . import paths

    ex = paths.examples_dir()
    home = paths.home()
    copied = []
    for name, dst in (("config.example.yaml", paths.config_file()), ("tasks.example.yaml", paths.tasks_file())):
        if not dst.exists():
            shutil.copy2(ex / name, dst)
            copied.append(str(dst))
    pdir = home / "prompts"
    pdir.mkdir(exist_ok=True)
    for p in (ex / "prompts").glob("*.md"):
        if not (pdir / p.name).exists():
            shutil.copy2(p, pdir / p.name)
            copied.append(str(pdir / p.name))
    print(f"tokenalarm 폴더: {home}")
    for c in copied:
        print(f"  생성: {c}")
    if not copied:
        print("  (이미 초기화되어 있음 — 기존 파일은 덮어쓰지 않았습니다)")
    print("다음 단계: config.yaml/tasks.yaml 편집 → tokenalarm setup-claude → tokenalarm status")
    return 0


def _fmt_ts(ts: float | None) -> str:
    return "-" if not ts else datetime.fromtimestamp(ts).strftime("%m/%d(%a) %H:%M")


def cmd_status(ctx: Ctx) -> int:
    from . import paths
    from .config import format_duration
    from .daemon import summary_text
    from .ingest import ingest_spool
    from .forecast import forecast_window
    from .policy import decide
    from .usage.base import FIVE_HOUR, SEVEN_DAY

    now = time.time()
    ingest_spool(ctx.store)
    usage = ctx.usage()
    if ctx.args.refresh or usage.age(now) is None:
        usage.refresh(now, allow_spend=bool(ctx.args.refresh))
    snap = usage.current(now)
    d = decide(snap, ctx.cfg.weekly_fill_rules, ctx.cfg.guards, now, ctx.store.get("sticky_fill"))
    week = snap.get(SEVEN_DAY) if snap else None
    five = snap.get(FIVE_HOUR) if snap else None
    wf = forecast_window(ctx.store, week, now) if week else None
    ff = forecast_window(ctx.store, five, now, recent_hours=1) if five else None
    act = ctx.activity().state(now)
    dstate = ctx.store.get("daemon_state") or {"state": "MONITOR"}
    if ctx.args.json:
        from dataclasses import asdict

        print(json.dumps({"week": asdict(wf) if wf else None, "five": asdict(ff) if ff else None,
                          "decision": d.reason, "target": d.target, "wants_fill": d.wants_fill,
                          "daemon": dstate, "claude_busy": act.claude_busy,
                          "observed_at": snap.observed_at if snap else None,
                          "source": snap.source if snap else None}, ensure_ascii=False, indent=2))
        return 0
    print("━━ Claude 사용량 ━━")
    if snap is None:
        print("사용량 데이터가 아직 없습니다.")
        print("  → `tokenalarm setup-claude` 후 Claude Code를 한 번 사용하거나 `tokenalarm status --refresh`")
    else:
        print(f"관측: {_fmt_ts(snap.observed_at)} ({format_duration(now - snap.observed_at)} 전, {snap.source})")
        print(summary_text(wf, ff, None))
        for k, w in sorted(snap.windows.items()):
            if ":" in k:
                print(f"{k}: {w.pct:.0f}% (리셋 {format_duration(w.seconds_left(now))} 후)")
    print("━━ 자동 실행 ━━")
    print(f"판단: {d.reason}")
    print(f"데몬 상태: {dstate.get('state')}" + (f" — {dstate.get('why')}" if dstate.get("why") else ""))
    print(f"Claude Code 활동: {act.reason}" + (f", 입력 유휴 {format_duration(act.input_idle)}"
                                              if act.input_idle is not None else ""))
    if paths.pause_file().exists() or (ctx.store.get("pause_until") or 0) > now:
        print("⏸ 일시정지 중 (tokenalarm resume 으로 해제)")
    ppu = ctx.store.get("pct_per_usd")
    if ppu:
        print(f"보정값: 비용(추정) $1 ≈ 주간 {float(ppu):.2f}%")
    return 0


def cmd_forecast(ctx: Ctx) -> int:
    from .forecast import daily_usage
    from .ingest import ingest_spool

    cmd_status(ctx)
    now = time.time()
    ingest_spool(ctx.store)
    print("━━ 최근 일별 주간 사용량 (Δ%p) ━━")
    rows = daily_usage(ctx.store, "seven_day", now, days=14)
    if not rows:
        print("(데이터 부족)")
    for day, delta in rows:
        print(f"{day}  {delta:5.1f}  {'█' * int(round(delta))}")
    return cmd_history(ctx, limit=5)


def cmd_history(ctx: Ctx, limit: int | None = None) -> int:
    limit = limit or getattr(ctx.args, "n", 20) or 20
    runs = ctx.store.runs(limit=limit)
    print("━━ 자동 실행 기록 ━━")
    if not runs:
        print("(아직 없음)")
    for r in runs:
        delta = (f"{r.pct_start:.0f}→{r.pct_end:.0f}%" if r.pct_start is not None and r.pct_end is not None else "-")
        print(f"{_fmt_ts(r.started_at)}  {r.task_id:<20} {r.status or '-':<8} {delta:<10} ${r.cost_usd or 0:.2f} "
              f"{r.turns or 0}턴  {r.reason or ''}")
        if r.branch or r.workdir:
            print(f"    {r.branch or ''} {r.workdir or ''}")
    return 0


def cmd_probe(ctx: Ctx) -> int:
    from .usage import oauth, probe

    cmd = ctx.claude_cmd()
    print(f"claude: {' '.join(cmd)}")
    s = probe.probe_usage_command(cmd, ctx.cfg.usage.rate_limit_scale)
    print(f"/usage 프로브 (무료): {_describe_snap(s)}")
    if ctx.args.ping:
        s2 = probe.probe_ping(cmd, ctx.cfg.usage.ping_model, ctx.cfg.usage.rate_limit_scale,
                              allowed_key_sources=ctx.cfg.runner.allowed_api_key_sources)
        print(f"ping 프로브 (Haiku 1회): {_describe_snap(s2)}")
        if s2:
            ctx.store.add_snapshot(s2)
    if ctx.args.oauth:
        try:
            s3 = oauth.fetch_oauth_usage()
        except oauth.OAuthUnavailable as e:
            s3 = None
            print(f"oauth: 사용 불가 — {e}")
        print(f"oauth (비공식): {_describe_snap(s3)}")
    if s:
        ctx.store.add_snapshot(s)
    return 0


def _describe_snap(s) -> str:
    from .config import format_duration

    if not s:
        return "데이터 없음"
    now = time.time()
    return ", ".join(f"{k} {w.pct:.1f}% (리셋 {format_duration(w.seconds_left(now))})" for k, w in s.windows.items())


def cmd_run(ctx: Ctx) -> int:
    from .fill import describe_report

    target = float(ctx.args.until)
    if not 0 < target <= 100:
        print("--until 은 0~100 사이 값이어야 합니다")
        return 2
    filler = ctx.filler(use_activity=ctx.args.respect_activity, notify=not ctx.args.dry_run)
    if not filler.tasks:
        print(f"작업이 없습니다: {ctx.tasks_path()} (tokenalarm init 으로 예시 생성)")
        return 1
    max_seconds = ctx.args.max_minutes * 60 if ctx.args.max_minutes else None
    report = filler.fill(target, only_task=ctx.args.task, dry_run=ctx.args.dry_run, max_seconds=max_seconds)
    print(describe_report(report))
    for o in report.outcomes:
        print(f"  - {o.task_id}: {o.status} ({o.reason}) {o.branch or ''} {o.workdir or ''}")
    return 0


def cmd_daemon(ctx: Ctx) -> int:
    from . import paths
    from .daemon import Daemon, SingleInstance

    daemon = Daemon(ctx.cfg, ctx.store, ctx.usage(), lambda: ctx.filler(use_activity=True, notify=True),
                    ctx.activity(), ctx.notifier(not ctx.args.dry_run), dry_run=ctx.args.dry_run)
    if ctx.args.once or ctx.args.dry_run:
        r = daemon.tick()
        print(f"[{r.state}] {r.decision}")
        print(f"→ {r.action}")
        if r.blocked_by and r.blocked_by not in r.action:
            print(f"  보류 사유: {r.blocked_by}")
        return 0
    lock = SingleInstance(paths.lock_file())
    if not lock.acquire():
        print("이미 실행 중인 tokenalarm 데몬이 있습니다.")
        return 1
    logging.getLogger(__name__).info("daemon started (pid %s)", os.getpid())
    try:
        daemon.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        lock.release()
    return 0


def cmd_tasks(ctx: Ctx) -> int:
    from .tasks import TaskError, is_due

    try:
        tasks = ctx.tasks()
    except TaskError as e:
        print(f"오류: {e}")
        return 1
    print(f"작업 파일: {ctx.tasks_path()}")
    if not tasks:
        print("(작업 없음)")
    now = time.time()
    for t in tasks:
        st = ctx.store.task_state(t.id)
        due, why = is_due(t, st, now)
        where = t.repo if t.kind == "repo" else (t.output_dir or "(기본 outputs 폴더)")
        print(f"{'●' if due else '○'} {t.id:<20} [{t.kind}/{t.repeat}] 우선순위 {t.priority}  {why}  → {where}")
    if ctx.args.action == "validate":
        print("검증 완료")
    return 0


def cmd_setup_claude(ctx_args: argparse.Namespace) -> int:
    from . import claude_setup

    path = Path(ctx_args.settings) if ctx_args.settings else None
    try:
        msgs = claude_setup.remove(path) if ctx_args.remove else claude_setup.install(
            path, force_statusline=ctx_args.force_statusline)
    except ValueError as e:
        print(f"settings.json 을 읽을 수 없어 변경하지 않았습니다: {e}")
        return 1
    for m in msgs:
        print(m)
    if not ctx_args.remove:
        print("Claude Code를 재시작하면 상태줄과 활동 감지가 적용됩니다.")
    return 0


def cmd_install(ctx_args: argparse.Namespace) -> int:
    from . import winsvc

    for m in (winsvc.uninstall() if ctx_args.command == "uninstall" else winsvc.install()):
        print(m)
    return 0


def cmd_pause(ctx: Ctx) -> int:
    from . import paths
    from .config import parse_duration

    if ctx.args.command == "resume":
        ctx.store.set("pause_until", None)
        if paths.pause_file().exists():
            paths.pause_file().unlink()
        print("자동 실행 재개")
        return 0
    if ctx.args.duration:
        until = time.time() + parse_duration(ctx.args.duration)
        ctx.store.set("pause_until", until)
        print(f"{_fmt_ts(until)} 까지 자동 실행 일시정지")
    else:
        paths.pause_file().write_text("paused\n", encoding="utf-8")
        print(f"자동 실행 일시정지 (해제: tokenalarm resume) — {paths.pause_file()}")
    return 0


def cmd_notify_test(ctx: Ctx) -> int:
    n = ctx.notifier(True)
    ok = n.send("TokenAlarm 테스트 알림", "알림 채널이 정상적으로 연결되었습니다. ✅")
    print("전송 성공" if ok else "전송 실패 — config.yaml 의 notify 설정과 logs/tokenalarm.log 를 확인하세요")
    return 0 if ok else 1


# --- parser --------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tokenalarm", description="Claude 사용량 예측 알람 + 리셋 전 자동 작업 실행")
    p.add_argument("--home", help="tokenalarm 데이터 폴더 (기본: %%LOCALAPPDATA%%\\tokenalarm)")
    p.add_argument("--config", help="config.yaml 경로")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="예시 config/tasks/prompts 생성")
    s = sub.add_parser("status", help="현재 사용량, 예측, 자동 실행 판단")
    s.add_argument("--json", action="store_true")
    s.add_argument("--refresh", action="store_true", help="지금 사용량을 다시 읽기 (필요 시 Haiku ping 사용)")
    f = sub.add_parser("forecast", help="상세 예측 + 일별 사용량 + 최근 실행")
    f.add_argument("--json", action="store_true")
    f.add_argument("--refresh", action="store_true")
    h = sub.add_parser("history", help="자동 실행 기록")
    h.add_argument("-n", type=int, default=20)
    pr = sub.add_parser("probe", help="사용량 소스 진단")
    pr.add_argument("--ping", action="store_true", help="Haiku 1회 요청으로 rate_limit_event 확인")
    pr.add_argument("--oauth", action="store_true", help="비공식 oauth/usage 엔드포인트 시험")
    r = sub.add_parser("run", help="지금 바로 목표 %%까지 작업 큐 실행")
    r.add_argument("--until", required=True, help="목표 주간 사용률 %%")
    r.add_argument("--task", help="특정 작업만 실행")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--max-minutes", type=float)
    r.add_argument("--respect-activity", action="store_true", help="대화형 Claude Code 사용이 감지되면 중단")
    d = sub.add_parser("daemon", help="백그라운드 감시 + 자동 실행")
    d.add_argument("--once", action="store_true", help="한 번만 판단/실행")
    d.add_argument("--dry-run", action="store_true", help="판단만 출력 (실행·알림 없음)")
    t = sub.add_parser("tasks", help="작업 큐 보기/검증")
    t.add_argument("action", nargs="?", choices=["list", "validate"], default="list")
    sc = sub.add_parser("setup-claude", help="Claude Code settings.json 에 상태줄/훅 등록")
    sc.add_argument("--remove", action="store_true")
    sc.add_argument("--force-statusline", action="store_true")
    sc.add_argument("--settings", help="settings.json 경로 (기본: ~/.claude/settings.json)")
    sub.add_parser("install", help="로그온 시 데몬 자동 시작 등록 (Windows 작업 스케줄러)")
    sub.add_parser("uninstall", help="자동 시작 해제")
    pa = sub.add_parser("pause", help="자동 실행 일시정지")
    pa.add_argument("--for", dest="duration", help="예: 6h, 2d")
    sub.add_parser("resume", help="일시정지 해제")
    sub.add_parser("notify-test", help="알림 채널 테스트")
    sub.add_parser("statusline", help="(Claude Code가 호출) 상태줄 출력")
    hk = sub.add_parser("hook", help="(Claude Code가 호출) 활동 기록")
    hk.add_argument("event")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    fast = _fast_path(argv)
    if fast is not None:
        return fast
    _utf8_stdout()
    args = build_parser().parse_args(argv)
    if args.home:
        os.environ["TOKENALARM_HOME"] = args.home
    _setup_logging(args.verbose, console=args.command != "daemon" or sys.stderr is not None)
    if args.command == "init":
        return cmd_init(args)
    if args.command == "setup-claude":
        return cmd_setup_claude(args)
    if args.command in ("install", "uninstall"):
        return cmd_install(args)
    try:
        ctx = Ctx(args)
    except (ValueError, OSError) as e:
        print(f"설정 오류: {e}")
        return 2
    handlers = {
        "status": cmd_status, "forecast": cmd_forecast, "history": cmd_history, "probe": cmd_probe,
        "run": cmd_run, "daemon": cmd_daemon, "tasks": cmd_tasks, "pause": cmd_pause, "resume": cmd_pause,
        "notify-test": cmd_notify_test,
    }
    try:
        return handlers[args.command](ctx)
    except Exception as e:
        from .claude_cli import ClaudeNotFound
        from .tasks import TaskError

        if isinstance(e, (ClaudeNotFound, TaskError)):
            print(f"오류: {e}")
            return 1
        raise
    finally:
        ctx.store.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
