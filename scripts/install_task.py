"""Register claudebot supervisor as a Windows scheduled task.
Runs at logon, restarts on failure, stays running forever.

Usage: python install_task.py          (install)
       python install_task.py remove   (uninstall)
"""
import os
import sys
import subprocess
import tempfile
from pathlib import Path

TASK_NAME = "claudebot"
# pythonw.exe = windowless python — no console window
PYTHON = str(Path(sys.executable).parent / "pythonw.exe")
RUN_PY = str(Path(__file__).resolve().parent.parent / "run.py")
WORK_DIR = str(Path(__file__).resolve().parent.parent)
USERNAME = os.environ.get("USERNAME", "Lyra")
DOMAIN = os.environ.get("USERDOMAIN", "DESKTOP")

# XML task definition — user-level logon trigger, restart on failure
TASK_XML = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{DOMAIN}\\{USERNAME}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <AllowHardTerminate>true</AllowHardTerminate>
  </Settings>
  <Actions>
    <Exec>
      <Command>{PYTHON}</Command>
      <Arguments>"{RUN_PY}"</Arguments>
      <WorkingDirectory>{WORK_DIR}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def install():
    print(f"Creating scheduled task '{TASK_NAME}'...")

    # write XML to temp file (schtasks needs UTF-16)
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False, encoding="utf-16")
    tmp.write(TASK_XML)
    tmp.close()

    try:
        r = subprocess.run(
            ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", tmp.name, "/F"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(f"Failed: {r.stderr.strip()}")
            return False
        print(r.stdout.strip())
    finally:
        os.unlink(tmp.name)

    # start it now
    print("Starting task...")
    r2 = subprocess.run(
        ["schtasks", "/Run", "/TN", TASK_NAME],
        capture_output=True, text=True,
    )
    if r2.returncode != 0:
        print(f"Start failed: {r2.stderr.strip()}")
        print("Task is registered and will start at next logon.")
    else:
        print(r2.stdout.strip())

    print(f"\nDone. claudebot will auto-start at every logon.")
    print(f"Restarts on failure every 30s, up to 999 times.")
    print(f"  Stop:    schtasks /End /TN {TASK_NAME}")
    print(f"  Remove:  python install_task.py remove")
    return True


def remove():
    print(f"Removing scheduled task '{TASK_NAME}'...")
    subprocess.run(["schtasks", "/End", "/TN", TASK_NAME],
                   capture_output=True, text=True)
    r = subprocess.run(
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"Failed: {r.stderr.strip()}")
        return False
    print(r.stdout.strip())
    return True


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "remove":
        remove()
    else:
        install()
