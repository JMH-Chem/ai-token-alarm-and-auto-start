"""Individual notification channels. Each returns True on success and never raises."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import urllib.request

from ..config import NtfyConfig, WebhookConfig

log = logging.getLogger(__name__)
APP_NAME = "TokenAlarm"


def _post_json(url: str, payload: dict, headers: dict | None = None, timeout: float = 10) -> bool:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json; charset=utf-8", "User-Agent": "tokenalarm", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        log.warning("POST %s failed: %s", url.split("?")[0], e)
        return False


def ntfy(cfg: NtfyConfig, title: str, body: str, priority: str = "default") -> bool:
    # JSON publishing keeps Korean titles intact (HTTP headers would need encoding).
    prio = {"low": 2, "default": 3, "high": 4, "urgent": 5}.get(priority, 3)
    headers = {"Authorization": f"Bearer {cfg.token}"} if cfg.token else {}
    return _post_json(cfg.url.rstrip("/"), {"topic": cfg.topic, "title": title, "message": body,
                                            "priority": prio, "tags": ["robot"]}, headers)


def webhook(cfg: WebhookConfig, title: str, body: str) -> bool:
    kind = (cfg.type or "slack").lower()
    if kind == "discord":
        payload = {"content": f"**{title}**\n{body}"[:1900], "username": APP_NAME}
    elif kind == "slack":
        payload = {"text": f"*{title}*\n{body}"}
    else:
        payload = {"title": title, "body": body, "app": APP_NAME}
    return _post_json(cfg.url, payload)


_PS_TOAST = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$t = [Console]::In.ReadLine(); $b = [Console]::In.ReadToEnd()
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc = '<toast><visual><binding template="ToastGeneric"><text></text><text></text></binding></visual></toast>'
$xml.LoadXml($doc)
$nodes = $xml.GetElementsByTagName('text')
$nodes.Item(0).AppendChild($xml.CreateTextNode($t)) | Out-Null
$nodes.Item(1).AppendChild($xml.CreateTextNode($b)) | Out-Null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show(
  [Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def desktop(title: str, body: str) -> bool:
    try:
        if sys.platform == "win32":
            try:
                from win11toast import notify as win_notify  # optional extra [windows]

                win_notify(title, body, app_id=APP_NAME)
                return True
            except Exception:
                pass
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            res = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _PS_TOAST],
                input=(title + "\n" + body).encode("utf-8"), capture_output=True, timeout=30, creationflags=flags)
            return res.returncode == 0
        if sys.platform == "darwin":
            script = f"display notification {json.dumps(body)} with title {json.dumps(title)}"
            return subprocess.run(["osascript", "-e", script], capture_output=True, timeout=15).returncode == 0
        if shutil.which("notify-send"):
            return subprocess.run(["notify-send", "-a", APP_NAME, title, body], capture_output=True,
                                  timeout=15).returncode == 0
    except Exception as e:
        log.warning("desktop notification failed: %s", e)
    return False
