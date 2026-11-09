"""Optional system-tray icon (``pip install tokenalarm[tray]`` → ``tokenalarm tray``).

* main thread: pystray icon (required by Windows/macOS message loops)
* background thread: the normal daemon loop with its own SQLite connection
* if another daemon already holds the lock, the tray only *shows* state.json (view-only)
Menu callbacks never block: they touch files/kv or set events and return.
"""
from __future__ import annotations

import json
import logging
import threading
import time

from . import paths
from .config import format_duration
from .i18n import L

log = logging.getLogger(__name__)
TOOLTIP_MAX = 127

GREEN, YELLOW, RED, GRAY = (46, 160, 67, 255), (219, 160, 0, 255), (207, 34, 46, 255), (120, 120, 120, 255)


def color_for(pct: float | None) -> tuple[int, int, int, int]:
    if pct is None:
        return GRAY
    if pct >= 85:
        return RED
    if pct >= 60:
        return YELLOW
    return GREEN


def read_state() -> dict | None:
    try:
        return json.loads(paths.state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def week_pct(state: dict | None) -> float | None:
    w = (state or {}).get("week") or {}
    return w.get("pct")


def tooltip(state: dict | None, paused: bool = False) -> str:
    if not state:
        return "TokenAlarm · " + L("데이터 대기 중", "waiting for data")
    parts = ["TokenAlarm"]
    w = state.get("week") or {}
    if w.get("pct") is not None:
        s = L(f"주 {w['pct']:.0f}%", f"wk {w['pct']:.0f}%")
        if w.get("projected") is not None:
            s += L(f" → 예상 {w['projected']:.0f}%", f" → ~{w['projected']:.0f}%")
        parts.append(s)
        if w.get("seconds_left") is not None:
            parts.append(L(f"리셋 {format_duration(w['seconds_left'])}", f"reset {format_duration(w['seconds_left'])}"))
    f = state.get("five") or {}
    if f.get("pct") is not None:
        parts.append(f"5h {f['pct']:.0f}%")
    if paused:
        parts.append(L("일시정지", "paused"))
    elif state.get("state") == "FILLING":
        parts.append(L("자동 실행 중", "autorun"))
    elif state.get("wants_fill") and state.get("target") is not None:
        parts.append(L(f"대기 {state['target']:g}%", f"pending {state['target']:g}%"))
    text = " · ".join(parts)
    return text if len(text) <= TOOLTIP_MAX else text[: TOOLTIP_MAX - 1] + "…"


def render_icon(pct: float | None, filling: bool = False, size: int = 64):
    """Coloured disc with the weekly % (no emoji: default fonts lack them). Requires Pillow."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = 2
    d.ellipse((pad, pad, size - pad, size - pad), fill=color_for(pct))
    if filling:  # autorun in progress: dark + white double ring, visible on light and dark taskbars
        w = max(2, size // 20)
        d.ellipse((pad, pad, size - pad, size - pad), outline=(20, 20, 20, 255), width=w)
        d.ellipse((pad + w, pad + w, size - pad - w, size - pad - w), outline=(255, 255, 255, 255), width=w)
    text = "?" if pct is None else f"{min(99, int(round(pct)))}"
    try:
        font = ImageFont.load_default(size=int(size * 0.5))
    except TypeError:  # Pillow < 10.1
        font = ImageFont.load_default()
    box = d.textbbox((0, 0), text, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    d.text(((size - tw) / 2 - box[0], (size - th) / 2 - box[1]), text, fill=(255, 255, 255, 255), font=font)
    return img


def is_paused(store) -> bool:
    return paths.pause_file().exists() or (store.get("pause_until") or 0) > time.time()


def run_tray(ctx) -> int:  # pragma: no cover - needs a desktop session; pure parts are tested
    import pystray

    from .cli import Ctx
    from .daemon import SingleInstance
    from .maintenance import open_target
    from .notify import channels

    lock = SingleInstance(paths.lock_file())
    owner = lock.acquire()
    stop = threading.Event()
    wake = threading.Event()
    daemon_thread = None

    icon = pystray.Icon("TokenAlarm", render_icon(None), "TokenAlarm")

    def tray_notify(title: str, body: str) -> bool:
        try:
            icon.notify(body[:250], title[:60])
            return True
        except Exception:
            return False

    channels.desktop_override = tray_notify

    def daemon_main() -> None:
        dctx = Ctx(ctx.args)  # own SQLite connection in this thread
        try:
            dctx.daemon().run_forever(stop_event=stop, wake_event=wake)
        except Exception:
            log.exception("daemon thread crashed")
        finally:
            dctx.store.close()

    if owner:
        daemon_thread = threading.Thread(target=daemon_main, name="tokenalarm-daemon", daemon=True)
        daemon_thread.start()

    def refresh_icon() -> None:
        while not stop.is_set():
            st = read_state()
            try:
                icon.icon = render_icon(week_pct(st), bool(st and st.get("state") == "FILLING"))
                icon.title = tooltip(st, is_paused(ctx.store)) + ("" if owner else L(" (보기 전용)", " (view only)"))
            except Exception:
                pass
            stop.wait(10)

    def show_status(_icon=None, _item=None) -> None:
        st = read_state() or {}
        body = "\n".join(x for x in [tooltip(st), st.get("decision"), st.get("action")] if x)
        tray_notify("TokenAlarm", body)

    def do_refresh(_icon=None, _item=None) -> None:
        wake.set()

    def pause6(_icon=None, _item=None) -> None:
        ctx.store.set("pause_until", time.time() + 6 * 3600)

    def resume(_icon=None, _item=None) -> None:
        ctx.store.set("pause_until", None)
        try:
            paths.pause_file().unlink()
        except OSError:
            pass

    def opener(target: str):
        return lambda _icon=None, _item=None: open_target(target)

    def quit_(_icon=None, _item=None) -> None:
        stop.set()
        if owner:
            paths.stop_file().write_text("stop\n", encoding="utf-8")  # interrupts a running autorun quickly
        icon.stop()

    icon.menu = pystray.Menu(
        pystray.MenuItem(L("상태 보기", "Show status"), show_status, default=True),
        pystray.MenuItem(L("지금 새로고침", "Refresh now"), do_refresh, enabled=owner),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(L("6시간 일시정지", "Pause for 6 hours"), pause6, enabled=lambda _: not is_paused(ctx.store)),
        pystray.MenuItem(L("재개", "Resume"), resume, enabled=lambda _: is_paused(ctx.store)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(L("데이터 폴더 열기", "Open data folder"), opener("data")),
        pystray.MenuItem(L("설정 열기", "Open settings"), opener("config")),
        pystray.MenuItem(L("로그 열기", "Open logs"), opener("logs")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(L("종료", "Quit"), quit_),
    )

    def setup(ic) -> None:
        ic.visible = True
        threading.Thread(target=refresh_icon, name="tokenalarm-tray-refresh", daemon=True).start()

    try:
        icon.run(setup=setup)
    finally:
        stop.set()
        if daemon_thread is not None:
            daemon_thread.join(60)
        try:
            paths.stop_file().unlink()
        except OSError:
            pass
        channels.desktop_override = None
        if owner:
            lock.release()
    return 0
