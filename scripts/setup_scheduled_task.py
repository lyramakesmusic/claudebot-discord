"""Create a Windows Scheduled Task to auto-start claudebot on logon.

Usage:
    python scripts/setup_scheduled_task.py          # create/update the task
    python scripts/setup_scheduled_task.py --remove  # remove the task

The task runs run.py under pythonw.exe (no console window) at user logon.
If the task already exists, it is replaced.
"""

import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASK_NAME = "ClaudebotSupervisor"


def _pythonw():
    """Path to pythonw.exe in the venv (windowless Python)."""
    return os.path.join(_ROOT, ".venv", "Scripts", "pythonw.exe")


def create_task():
    pythonw = _pythonw()
    run_py = os.path.join(_ROOT, "run.py")

    if not os.path.exists(pythonw):
        print(f"ERROR: {pythonw} not found")
        sys.exit(1)

    # Delete existing task if any (ignore errors)
    subprocess.run(
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True,
    )

    # Create the task:
    #   - Triggers at logon of current user
    #   - Runs pythonw.exe run.py (no console window)
    #   - Working directory is the project root
    #   - No time limit (run indefinitely)
    # Try ONLOGON first (needs admin), fall back to startup folder shortcut
    result = subprocess.run(
        [
            "schtasks", "/Create",
            "/TN", TASK_NAME,
            "/TR", f'"{pythonw}" "{run_py}"',
            "/SC", "ONLOGON",
            "/F",
        ],
        capture_output=True, text=True,
    )

    if result.returncode != 0:
        # Fallback: create a shortcut in the Startup folder
        startup_dir = os.path.join(
            os.environ.get("APPDATA", ""),
            "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
        )
        shortcut_vbs = os.path.join(_ROOT, "data", "_create_shortcut.vbs")
        shortcut_path = os.path.join(startup_dir, f"{TASK_NAME}.lnk")

        # Use VBScript to create a proper .lnk shortcut
        vbs = f'''Set ws = CreateObject("WScript.Shell")
Set link = ws.CreateShortcut("{shortcut_path}")
link.TargetPath = "{pythonw}"
link.Arguments = """{run_py}"""
link.WorkingDirectory = "{_ROOT}"
link.Description = "Claudebot supervisor auto-start"
link.Save
'''
        with open(shortcut_vbs, "w") as f:
            f.write(vbs)
        result = subprocess.run(
            ["cscript", "//nologo", shortcut_vbs],
            capture_output=True, text=True,
        )
        try:
            os.remove(shortcut_vbs)
        except OSError:
            pass

        if result.returncode != 0:
            print(f"ERROR creating startup shortcut:")
            print(result.stderr or result.stdout)
            sys.exit(1)

        print(f"Startup shortcut created at:")
        print(f"  {shortcut_path}")
        print(f"  Target: {pythonw} {run_py}")
        print()
        print("The bots will now auto-start whenever you log in to Windows.")
        return

    if result.returncode == 0:
        print(f"Scheduled task '{TASK_NAME}' created successfully.")
        print(f"  Trigger: at logon")
        print(f"  Action:  {pythonw} {run_py}")
        print(f"  CWD:     {_ROOT}")
        print()
        print("The bots will now auto-start whenever you log in to Windows.")
    else:
        print(f"ERROR creating task (code {result.returncode}):")
        print(result.stderr or result.stdout)
        sys.exit(1)


def remove_task():
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"Scheduled task '{TASK_NAME}' removed.")
    else:
        print(f"Task not found or already removed.")


def main():
    if "--remove" in sys.argv:
        remove_task()
    else:
        create_task()


if __name__ == "__main__":
    main()
