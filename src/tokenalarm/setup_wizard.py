"""`tokenalarm setup`: one guided pass from install to a working background service."""
from __future__ import annotations

import argparse
import secrets
import shutil
import sys
from pathlib import Path

from . import i18n, paths
from .config import PRESETS, load_config, minimal_config_text
from .i18n import L

PRESET_TEXT = {
    "standard": ("기본 — 리셋 3일 전 <20%→30%, 2일 전 <40%→50%, 1일 전 <60%→70%, 12시간 전 <80%→90%",
                 "standard — 3d before reset <20%→30%, 2d <40%→50%, 1d <60%→70%, 12h <80%→90%"),
    "conservative": ("보수적 — 2일 전 <30%→40%, 1일 전 <50%→60%, 12시간 전 <70%→80%",
                     "conservative — 2d <30%→40%, 1d <50%→60%, 12h <70%→80%"),
    "aggressive": ("적극적 — 3일 전 <30%→45%, 2일 전 <50%→65%, 1일 전 <70%→85%, 12시간 전 <85%→93%",
                   "aggressive — 3d <30%→45%, 2d <50%→65%, 1d <70%→85%, 12h <85%→93%"),
}


class Prompter:
    def __init__(self, assume_yes: bool):
        self.yes = assume_yes or not sys.stdin or not sys.stdin.isatty()

    def _input(self, text: str) -> str | None:
        if self.yes:
            return None
        try:
            return input(text).strip()
        except EOFError:
            self.yes = True
            return None

    def ask(self, text: str, default: str = "") -> str:
        shown = f" [{default}]" if default else ""
        ans = self._input(f"{text}{shown}: ")
        return default if not ans else ans

    def yes_no(self, text: str, default: bool = True) -> bool:
        hint = "Y/n" if default else "y/N"
        ans = self._input(f"{text} [{hint}]: ")
        if not ans:
            return default
        return ans.lower() in ("y", "yes", "예", "네", "ㅇ", "ㅇㅇ")

    def choose(self, text: str, options: list[str], default: int = 0) -> int:
        print(text)
        for i, o in enumerate(options, 1):
            print(f"  {i}) {o}")
        ans = self._input(L(f"번호 선택 [{default + 1}]: ", f"Choose [{default + 1}]: "))
        try:
            idx = int(ans) - 1 if ans else default
            return idx if 0 <= idx < len(options) else default
        except ValueError:
            return default


def _step(n: int, total: int, title: str) -> None:
    print(f"\n[{n}/{total}] {title}")


def _detect_webhook(url: str) -> str:
    u = url.lower()
    if "discord.com/api/webhooks" in u or "discordapp.com/api/webhooks" in u:
        return "discord"
    if "hooks.slack.com" in u:
        return "slack"
    return "generic"


def _test_desktop(p: Prompter, app_id: str) -> str:
    """Send a test toast; on Windows ask whether it appeared and fall back to PowerShell's sender id."""
    from .config import NtfyConfig, NotifyConfig
    from .notify import Notifier

    cfg = NotifyConfig(desktop=True, ntfy=NtfyConfig(), desktop_app_id=app_id)
    Notifier(cfg).send("TokenAlarm", L("데스크톱 알림 테스트입니다 ✅", "Desktop notification test ✅"))
    if p.yes or sys.platform != "win32":
        return app_id
    if p.yes_no(L("알림이 화면 오른쪽 아래에 보였나요?", "Did a notification appear (bottom-right)?"), True):
        return app_id
    cfg.desktop_app_id = "powershell"
    Notifier(cfg).send("TokenAlarm", L("두 번째 테스트 (PowerShell 이름으로)", "Second test (as PowerShell)"))
    if p.yes_no(L("이번에는 보였나요?", "Did it appear this time?"), True):
        return "powershell"
    print(L("  → Windows 설정 > 시스템 > 알림 에서 알림/방해 금지 모드를 확인하세요.",
            "  → Check Windows Settings > System > Notifications / Do not disturb."))
    return app_id


def run(args: argparse.Namespace) -> int:
    from . import claude_setup, maintenance, winsvc
    from .notify import channels

    p = Prompter(getattr(args, "yes", False))
    if getattr(args, "repair", False):
        return repair()

    total = 7
    print("TokenAlarm setup" + f" (v{__import__('tokenalarm').__version__})")
    print(L(f"데이터 폴더: {paths.home()}", f"Data folder: {paths.home()}"))

    # 1. language ---------------------------------------------------------------------------------
    lang = args.lang or (i18n.detect() if p.yes else None)
    if not lang:
        lang = "ko" if p.choose("Language / 언어", ["한국어", "English"], 0 if i18n.detect() == "ko" else 1) == 0 \
            else "en"
    i18n.set_lang(lang)

    # 2. Claude Code ------------------------------------------------------------------------------
    _step(1, total, L("Claude Code 확인", "Checking Claude Code"))
    claude_ok = _check_claude(args)

    # 3. preset -----------------------------------------------------------------------------------
    _step(2, total, L("리셋 전 자동 채우기 규칙", "Fill-before-reset rules"))
    names = list(PRESETS)
    preset = args.preset or names[p.choose(L("어떤 규칙을 쓸까요?", "Which rules?"),
                                           [L(*PRESET_TEXT[n]) for n in names], 0)]

    # 4. notifications ----------------------------------------------------------------------------
    _step(3, total, L("알림", "Notifications"))
    desktop = True
    app_id = "auto"
    if sys.platform == "win32":
        channels.register_aumid()
    if not args.no_test_notify:
        app_id = _test_desktop(p, app_id)
    topic = None
    ntfy_arg = args.ntfy
    if ntfy_arg is None and not p.yes:
        if p.yes_no(L("휴대폰 푸시 알림(ntfy, 무료)을 쓸까요?", "Use phone push notifications (ntfy, free)?"), False):
            ntfy_arg = "auto"
    if ntfy_arg and ntfy_arg != "none":
        topic = f"tokenalarm-{secrets.token_urlsafe(9).replace('_', 'x').replace('-', 'y').lower()}" \
            if ntfy_arg == "auto" else ntfy_arg
        print(L(f"  ntfy 앱에서 이 토픽을 구독하세요: {topic}  (https://ntfy.sh/{topic})",
                f"  Subscribe to this topic in the ntfy app: {topic}  (https://ntfy.sh/{topic})"))
        print(L("  ※ ntfy.sh 는 공개 서버라 토픽 이름을 아는 사람은 알림을 볼 수 있습니다. 이름을 공유하지 마세요.",
                "  ※ ntfy.sh is public: anyone who knows the topic can read it. Keep the name private."))
    webhooks = []
    url = args.webhook
    if url is None and not p.yes:
        url = p.ask(L("Slack/Discord 웹훅 URL (없으면 Enter)", "Slack/Discord webhook URL (Enter to skip)"), "")
    if url:
        webhooks.append({"type": _detect_webhook(url), "url": url})
    if not args.no_test_notify and (topic or webhooks):
        from .config import NotifyConfig, NtfyConfig, WebhookConfig
        from .notify import Notifier

        ncfg = NotifyConfig(desktop=False, ntfy=NtfyConfig(topic=topic),
                            webhooks=[WebhookConfig(**h) for h in webhooks])
        ok = Notifier(ncfg).send("TokenAlarm", L("휴대폰/웹훅 알림 테스트 ✅", "Phone/webhook notification test ✅"))
        print(L("  테스트 알림 전송 " + ("성공" if ok else "실패 — URL/토픽을 확인하세요"),
                "  Test notification " + ("sent" if ok else "failed — check the URL/topic")))
    quiet = args.quiet_hours
    if quiet is None and not p.yes:
        quiet = p.ask(L("자동 실행을 막을 시간대 (예: 09:00-18:00, 없으면 Enter)",
                        "Hours when autorun must not start (e.g. 09:00-18:00, Enter for none)"), "") or None

    # 5. write config + tasks ---------------------------------------------------------------------
    _step(4, total, L("설정 저장", "Saving settings"))
    cfg_path = paths.config_file()
    write = True
    if cfg_path.exists() and not args.force:
        write = p.yes_no(L(f"기존 설정({cfg_path})을 새로 만들까요? (백업됨)",
                           f"Replace existing config ({cfg_path})? (backed up)"), False) if not p.yes else False
    if write:
        if cfg_path.exists():
            shutil.copy2(cfg_path, cfg_path.with_name("config.yaml.bak"))
        text = minimal_config_text(lang, preset, desktop, topic, webhooks, quiet)
        if app_id != "auto":
            text = text.replace("notify:\n  desktop: true", f"notify:\n  desktop: true\n  desktop_app_id: {app_id}")
        cfg_path.write_text(text, encoding="utf-8")
        load_config(cfg_path)  # validate what we wrote
        print(L(f"  저장: {cfg_path}", f"  saved: {cfg_path}"))
    else:
        print(L("  기존 설정 유지", "  keeping the existing config"))
    _copy_examples()
    _first_task(p, args)

    # 6. Claude Code integration ------------------------------------------------------------------
    _step(5, total, L("Claude Code 연동 (상태줄 + 활동 감지)", "Claude Code integration (status line + activity)"))
    if args.no_claude_setup:
        print(L("  건너뜀", "  skipped"))
    elif p.yes or p.yes_no(L("Claude Code 설정에 상태줄과 훅을 추가할까요? (기존 설정은 백업·유지)",
                             "Add the status line and hooks to Claude Code settings? (backed up, existing kept)"),
                           True):
        try:
            for m in claude_setup.install(force_statusline=args.force_statusline):
                print("  " + m)
        except ValueError as e:
            print(L(f"  settings.json 을 읽을 수 없어 건너뜀: {e}", f"  skipped — cannot read settings.json: {e}"))

    # 7. autostart --------------------------------------------------------------------------------
    _step(6, total, L("로그온 시 자동 시작", "Start at logon"))
    mode = args.autostart
    tray_ok = _tray_available()
    if mode is None:
        opts = [L("백그라운드 (권장)", "background (recommended)"),
                L("트레이 아이콘", "tray icon") + ("" if tray_ok else L(" — 설치 필요: tokenalarm[tray]",
                                                                  " — needs tokenalarm[tray]")),
                L("사용 안 함", "don't start automatically")]
        mode = ["daemon", "tray", "none"][p.choose(L("어떻게 실행할까요?", "How should it run?"), opts, 0)]
    if mode == "tray" and not tray_ok:
        print(L("  트레이 구성요소가 없어 백그라운드로 등록합니다.", "  Tray extra missing — using background mode."))
        mode = "daemon"
    if mode != "none":
        for m in winsvc.install(start_now=claude_ok, tray=(mode == "tray")):
            if m:
                print("  " + m.replace("\n", "\n  "))
    maintenance.save_install_record(autostart=mode, lang=lang)

    # 8. summary ----------------------------------------------------------------------------------
    _step(7, total, L("점검", "Check"))
    from . import doctor

    checks = doctor.run_checks(paths.config_file(), paths.tasks_file(), check_claude=False)
    print(doctor.render(checks))
    print(L("\n끝! 자주 쓰는 명령: tokenalarm status · tokenalarm add · tokenalarm pause · tokenalarm doctor",
            "\nDone! Everyday commands: tokenalarm status · tokenalarm add · tokenalarm pause · tokenalarm doctor"))
    return 0


def _check_claude(args: argparse.Namespace) -> bool:
    from .claude_cli import ClaudeNotFound, autorun_env, resolve_claude_cmd, run_stream

    try:
        cmd = resolve_claude_cmd(None)
    except ClaudeNotFound:
        print(L("  ❌ Claude Code CLI를 찾지 못했습니다. 설치 후 `claude` 를 한 번 실행해 로그인하세요:",
                "  ❌ Claude Code CLI not found. Install it, then run `claude` once and log in:"))
        print("     https://code.claude.com/docs/en/setup")
        return False
    if getattr(args, "skip_claude_check", False):
        print(f"  ✅ {cmd[0]}")
        return True
    events = run_stream([*cmd, "-p", "/usage", "--output-format", "stream-json", "--verbose",
                         "--no-session-persistence"], None, autorun_env(), 60)
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), None)
    if init is None:
        print(L("  ⚠️  Claude Code가 응답하지 않습니다. 터미널에서 `claude` 를 실행해 로그인했는지 확인하세요.",
                "  ⚠️  Claude Code did not respond. Run `claude` in a terminal and make sure you are logged in."))
        return False
    src = init.get("apiKeySource")
    if src == "none":
        print(L(f"  ✅ 구독 계정으로 로그인됨 ({cmd[0]})", f"  ✅ Logged in with a subscription ({cmd[0]})"))
        return True
    print(L(f"  ⚠️  API 키 인증({src})입니다. 자동 실행은 차단됩니다 — `claude` 에서 /login 으로 구독 계정에 로그인하세요.",
            f"  ⚠️  API-key auth ({src}). Autorun will be blocked — use /login in `claude` with your subscription."))
    return False


def _copy_examples() -> None:
    ex = paths.examples_dir()
    pdir = paths.home() / "prompts"
    pdir.mkdir(exist_ok=True)
    for f in (ex / "prompts").glob("*.md"):
        if not (pdir / f.name).exists():
            shutil.copy2(f, pdir / f.name)
    ref = paths.home() / "config.example.yaml"
    shutil.copy2(ex / "config.example.yaml", ref)  # always refresh the reference copy


def _first_task(p: Prompter, args: argparse.Namespace) -> None:
    from .tasks import add_task, read_raw, write_raw

    tf = paths.tasks_file()
    if not tf.exists():
        write_raw(tf, {"tasks": []})
    raw = read_raw(tf)
    if raw["tasks"] or p.yes:
        return
    choice = p.choose(L("남는 사용량으로 실행할 첫 작업을 추가할까요?", "Add a first task for leftover quota?"),
                      [L("예시: 주간 AI 소식 리서치 보고서 (매주)", "Example: weekly AI news research report"),
                       L("직접 입력", "Type my own"), L("나중에 (tokenalarm add)", "Later (tokenalarm add)")], 0)
    if choice == 0:
        tid = add_task(tf, "research", "@" + str(paths.home() / "prompts" / "research_weekly.md"),
                       task_id="weekly-ai-news", repeat="weekly", priority=5)
        print(L(f"  추가: {tid}", f"  added: {tid}"))
    elif choice == 1:
        kind = ["research", "study", "repo"][p.choose(L("종류", "Kind"), [
            L("리서치·보고서", "research / report"), L("학습·정리", "study / notes"),
            L("코드 저장소 작업 (git worktree)", "code repository (git worktree)")], 0)]
        repo = p.ask(L("저장소 경로", "Repository path")) if kind == "repo" else None
        prompt = p.ask(L("무엇을 할까요? (프롬프트)", "What should it do? (prompt)"))
        if prompt:
            try:
                tid = add_task(tf, kind, prompt, repo=repo, repeat="weekly")
                print(L(f"  추가: {tid}", f"  added: {tid}"))
            except Exception as e:
                print(L(f"  추가 실패: {e}", f"  could not add: {e}"))


def _tray_available() -> bool:
    try:
        import PIL  # noqa: F401
        import pystray  # noqa: F401

        return True
    except Exception:
        return False


def repair() -> int:
    """Re-register hooks/status line/AUMID/autostart with the current install paths (after an upgrade)."""
    from . import claude_setup, maintenance, winsvc
    from .notify import channels

    print("TokenAlarm setup --repair")
    try:
        st = claude_setup.status()
        if st["ok"] and (st["hooks"] or st["statusline_ours"]):
            for m in claude_setup.install():
                print("  " + m)
    except ValueError as e:
        print(f"  settings.json: {e}")
    if sys.platform == "win32":
        channels.register_aumid()
    rec = maintenance.install_record()
    mode = rec.get("autostart")
    if mode in ("daemon", "tray") and winsvc.is_installed():
        for m in winsvc.install(start_now=True, tray=(mode == "tray")):
            if m:
                print("  " + m)
    print(L("복구 완료", "Repair done"))
    return 0


def add_parser_args(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--yes", "-y", action="store_true", help="non-interactive, accept defaults")
    sp.add_argument("--repair", action="store_true", help="re-register hooks/status line/autostart (after upgrade)")
    sp.add_argument("--lang", choices=["ko", "en"])
    sp.add_argument("--preset", choices=list(PRESETS))
    sp.add_argument("--ntfy", help="auto | none | <topic>")
    sp.add_argument("--webhook", help="Slack/Discord webhook URL")
    sp.add_argument("--quiet-hours", help='e.g. "09:00-18:00"')
    sp.add_argument("--autostart", choices=["daemon", "tray", "none"])
    sp.add_argument("--no-claude-setup", action="store_true")
    sp.add_argument("--force-statusline", action="store_true")
    sp.add_argument("--no-test-notify", action="store_true")
    sp.add_argument("--skip-claude-check", action="store_true", help=argparse.SUPPRESS)
    sp.add_argument("--force", action="store_true", help="overwrite an existing config.yaml (backed up)")


__all__ = ["run", "repair", "add_parser_args", "Path"]
