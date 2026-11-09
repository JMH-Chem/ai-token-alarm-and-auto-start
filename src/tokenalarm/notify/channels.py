"""Individual notification channels. Each returns True on success and never raises."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import urllib.request
from typing import Callable

from ..config import NtfyConfig, WebhookConfig

log = logging.getLogger(__name__)
APP_NAME = "TokenAlarm"
AUMID = "TokenAlarm"  # registered under HKCU by `tokenalarm setup` so Windows does not drop our toasts
POWERSHELL_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
_AUMID_KEY = r"Software\Classes\AppUserModelId\TokenAlarm"

# The tray app sets this so toasts come from its own icon (pystray Icon.notify).
desktop_override: Callable[[str, str], bool] | None = None


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


# --- Windows toast ---------------------------------------------------------------------------------

def register_aumid() -> bool:
    """Register the "TokenAlarm" sender (HKCU, no admin) so toasts show our name and are not dropped."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _AUMID_KEY) as k:
            winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(k, "ShowInSettings", 0, winreg.REG_DWORD, 1)
        return True
    except OSError as e:
        log.warning("AUMID registration failed: %s", e)
        return False


def unregister_aumid() -> None:
    if sys.platform != "win32":
        return
    try:
        import winreg

        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, _AUMID_KEY)
    except OSError:
        pass


def aumid_registered() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUMID_KEY):
            return True
    except OSError:
        return False


_PS_TOAST = r"""
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding $false  # Korean text arrives as UTF-8, not CP949
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$app = [Console]::In.ReadLine(); $t = [Console]::In.ReadLine(); $b = [Console]::In.ReadToEnd()
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text></text><text></text></binding></visual></toast>')
$nodes = $xml.GetElementsByTagName('text')
$nodes.Item(0).AppendChild($xml.CreateTextNode($t)) | Out-Null
$nodes.Item(1).AppendChild($xml.CreateTextNode($b)) | Out-Null
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show(
  [Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def _windows_app_id(mode: str) -> str:
    if mode == "powershell":
        return POWERSHELL_AUMID
    if mode == "tokenalarm":
        return AUMID
    return AUMID if aumid_registered() else POWERSHELL_AUMID


def desktop(title: str, body: str, app_id_mode: str = "auto") -> bool:
    if desktop_override is not None:
        try:
            if desktop_override(title, body):
                return True
        except Exception as e:  # fall back to the normal path
            log.warning("tray notification failed: %s", e)
    try:
        if sys.platform == "win32":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            payload = "\n".join([_windows_app_id(app_id_mode), title.replace("\n", " "), body])
            res = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _PS_TOAST],
                input=payload.encode("utf-8"), capture_output=True, timeout=30, creationflags=flags)
            if res.returncode != 0:
                log.warning("toast failed: %s", res.stderr.decode("utf-8", "replace")[-300:])
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
