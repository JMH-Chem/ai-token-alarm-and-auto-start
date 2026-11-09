"""Start tokenalarm at logon: Windows Task Scheduler (XML) or a printed systemd user unit elsewhere."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from . import paths
from .i18n import L

TASK_NAME = "TokenAlarm"


def _flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def launcher(mode: str) -> tuple[str, str]:
    """(command, arguments) for the logon task. ``mode`` is "daemon" or "tray"."""
    from .claude_setup import script_path

    gui = script_path("tokenalarmw")  # GUI entry point: no console window
    if gui:
        return gui, mode
    exe = Path(sys.executable)
    if sys.platform == "win32" and exe.name.lower() == "python.exe" and exe.with_name("pythonw.exe").exists():
        exe = exe.with_name("pythonw.exe")
    return str(exe), f"-m tokenalarm {mode}"


def task_xml(user: str, command: str, arguments: str, workdir: str) -> str:
    # "Run only when user is logged on" (InteractiveToken) is required for keyboard/mouse idle detection
    # and the tray icon; no execution time limit, keep running on battery, restart on failure.
    desc = "TokenAlarm: Claude usage forecast + spend leftover quota before reset"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(desc)}</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user)}</UserId>
      <Delay>PT1M</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT5M</Interval>
      <Count>288</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(workdir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def systemd_unit(mode: str = "daemon") -> str:
    cmd, args = launcher(mode)
    return f"""[Unit]
Description=TokenAlarm (Claude usage forecaster + leftover-quota task runner)

[Service]
ExecStart={cmd} {args}
Restart=on-failure
RestartSec=60

[Install]
WantedBy=default.target
"""


def current_user() -> str:
    return f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")


def is_installed() -> bool | None:
    """True/False on Windows, None elsewhere (unknown)."""
    if sys.platform != "win32":
        return None
    res = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True, text=True,
                         creationflags=_flags())
    return res.returncode == 0


def install(start_now: bool = True, tray: bool = False, dry_run: bool = False) -> list[str]:
    mode = "tray" if tray else "daemon"
    if sys.platform != "win32":
        unit = Path.home() / ".config" / "systemd" / "user" / "tokenalarm.service"
        return [
            L("Windows가 아니므로 systemd 사용자 유닛 예시를 출력합니다:",
              "Not Windows — here is a systemd user unit you can install:"),
            f"# {unit}",
            systemd_unit(mode),
            "systemctl --user daemon-reload && systemctl --user enable --now tokenalarm",
        ]
    cmd, args = launcher(mode)
    xml = task_xml(current_user(), cmd, args, str(paths.home()))
    if dry_run:
        return [xml]
    xml_path = paths.home() / "tokenalarm-task.xml"
    xml_path.write_text(xml, encoding="utf-16")
    res = subprocess.run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"],
                         capture_output=True, text=True, creationflags=_flags())
    msgs = [res.stdout.strip() or res.stderr.strip()]
    if res.returncode != 0:
        msgs.append(L("작업 등록 실패 — 위 메시지를 확인하세요", "Task registration failed — see the message above"))
        return msgs
    what = L("트레이 아이콘", "tray icon") if tray else L("백그라운드 데몬", "background daemon")
    msgs.append(L(f"로그온 시 자동 시작 등록: {what} (작업 스케줄러 '{TASK_NAME}')",
                  f"Registered to start at logon: {what} (Task Scheduler '{TASK_NAME}')"))
    if start_now:
        r2 = subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], capture_output=True, text=True,
                            creationflags=_flags())
        msgs.append(r2.stdout.strip() or r2.stderr.strip())
    return msgs


def end_task() -> None:
    if sys.platform == "win32":
        subprocess.run(["schtasks", "/End", "/TN", TASK_NAME], capture_output=True, text=True,
                       creationflags=_flags())


def uninstall() -> list[str]:
    if sys.platform != "win32":
        return [L("systemctl --user disable --now tokenalarm  # 으로 해제하세요",
                  "Run: systemctl --user disable --now tokenalarm")]
    end_task()
    res = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, text=True,
                         creationflags=_flags())
    return [res.stdout.strip() or res.stderr.strip()]
