"""Notification fan-out: desktop toast, ntfy push, Slack/Discord webhooks."""
from __future__ import annotations

import logging
import time

from ..config import NotifyConfig
from ..store import Store
from . import channels

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, cfg: NotifyConfig, store: Store | None = None, enabled: bool = True):
        self.cfg = cfg
        self.store = store
        self.enabled = enabled
        self.sent: list[tuple[str, str]] = []  # for tests / dry runs

    def send(self, title: str, body: str, key: str | None = None, dedupe_seconds: float | None = None,
             priority: str = "default", now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if key and self.store and dedupe_seconds and self.store.notified_recently(key, dedupe_seconds, now):
            return False
        self.sent.append((title, body))
        if key and self.store:
            self.store.mark_notified(key, now)
        log.info("notify: %s — %s", title, body.replace("\n", " | "))
        if not self.enabled:
            return True
        ok = False
        if self.cfg.desktop:
            ok |= channels.desktop(title, body)
        if self.cfg.ntfy.topic:
            ok |= channels.ntfy(self.cfg.ntfy, title, body, priority)
        for hook in self.cfg.webhooks:
            ok |= channels.webhook(hook, title, body)
        return ok
