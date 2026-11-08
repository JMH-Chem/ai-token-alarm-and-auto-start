"""Run the daemon at logon: Windows Task Scheduler (XML) or a printed systemd user unit elsewhere."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from . import paths

TASK_NAME = "TokenAlarm"


def daemon_python() -> str:
    exe = Path(sys.executable)
    if sys.platform == "win32" and exe.name.lower() == "python.exe":
        alt = exe.with_name("pythonw.exe")  # no console window
        if alt.exists():
            return str(alt)
    return str(exe)


def daemon_args() -> str:
    args = "-m tokenalarm"
    home = os.environ.get("TOKENALARM_HOME")
    if home:
        args += f' --home "{home}"'
    return args + " daemon"


def task_xml(user: str, command: str, arguments: str, workdir: str) -> str:
    # "Run only when user is logged on" (InteractiveToken) is required for keyboard/mouse idle detection;
    # no execution time limit, keep running on battery, restart on failure.
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>TokenAlarm: Claude 사용량 예측 알림 + 리셋 전 자동 작업 실행</Description>
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


def systemd_unit() -> str:
    return f"""[Unit]
Description=TokenAlarm (Claude usage forecaster + leftover-quota task runner)

[Service]
ExecStart={sys.executable} {daemon_args()}
Restart=on-failure
RestartSec=60

[Install]
WantedBy=default.target
"""


def install(start_now: bool = True) -> list[str]:
    if sys.platform != "win32":
        unit = Path.home() / ".config" / "systemd" / "user" / "tokenalarm.service"
        return [
            "Windows가 아니므로 작업 스케줄러 대신 systemd 사용자 유닛 예시를 출력합니다:",
            f"# {unit}",
            systemd_unit(),
            "systemctl --user daemon-reload && systemctl --user enable --now tokenalarm",
        ]
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
    xml = task_xml(user, daemon_python(), daemon_args(), str(paths.home()))
    xml_path = paths.home() / "tokenalarm-task.xml"
    xml_path.write_text(xml, encoding="utf-16")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    res = subprocess.run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"],
                         capture_output=True, text=True, creationflags=flags)
    msgs = [res.stdout.strip() or res.stderr.strip()]
    if res.returncode != 0:
        msgs.append("작업 등록 실패 — 위 메시지를 확인하세요")
        return msgs
    msgs.append(f"작업 스케줄러 등록: '{TASK_NAME}' (로그온 시 자동 시작, 사용자 로그온 중에만 실행)")
    if start_now:
        r2 = subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], capture_output=True, text=True,
                            creationflags=flags)
        msgs.append(r2.stdout.strip() or r2.stderr.strip())
    return msgs


def uninstall() -> list[str]:
    if sys.platform != "win32":
        return ["systemctl --user disable --now tokenalarm  # 으로 해제하세요"]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(["schtasks", "/End", "/TN", TASK_NAME], capture_output=True, text=True, creationflags=flags)
    res = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, text=True,
                         creationflags=flags)
    return [res.stdout.strip() or res.stderr.strip()]
