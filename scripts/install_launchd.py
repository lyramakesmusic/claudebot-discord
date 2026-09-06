#!/usr/bin/env python3
"""Install claudebot as a macOS launchd user agent so it starts at login and stays up.

    .venv/bin/python scripts/install_launchd.py            # install (or reinstall) + start
    .venv/bin/python scripts/install_launchd.py --uninstall
    .venv/bin/python scripts/install_launchd.py --plist-only    # rewrite the plist without restarting

The agent runs run.py (the outermost restart loop) under the project venv with
KeepAlive, so the chain is: launchd -> run.py -> supervisor -> bots.
Logs go to logs/launchd.out.log and logs/launchd.err.log.
"""

import os
import plistlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABEL = "com.lyra.claudebot"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
DOMAIN = f"gui/{os.getuid()}"


def _bootout():
    subprocess.run(["launchctl", "bootout", f"{DOMAIN}/{LABEL}"], capture_output=True)


def install():
    python = ROOT / ".venv" / "bin" / "python"
    if not python.exists():
        sys.exit(f"venv python not found at {python} — run `uv sync` first")
    (ROOT / "logs").mkdir(exist_ok=True)
    home = str(Path.home())
    path = ":".join([
        f"{home}/.local/bin", "/opt/homebrew/bin", "/opt/homebrew/sbin",
        "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin",
    ])
    plist = {
        "Label": LABEL,
        # caffeinate -is: no idle/system sleep while the bot runs (this is a laptop)
        "ProgramArguments": ["/usr/bin/caffeinate", "-is", str(python), "-u", str(ROOT / "run.py")],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Interactive",
        "StandardOutPath": str(ROOT / "logs" / "launchd.out.log"),
        "StandardErrorPath": str(ROOT / "logs" / "launchd.err.log"),
        "EnvironmentVariables": {
            "PATH": path,
            "HOME": home,
            "SHELL": "/bin/zsh",
            "LANG": "en_US.UTF-8",
            "PYTHONUNBUFFERED": "1",
        },
    }
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    if "--plist-only" in sys.argv:
        with open(PLIST, "wb") as f:
            plistlib.dump(plist, f)
        print(f"Wrote {PLIST} (takes effect at next login/bootstrap; running agent untouched)")
        return
    _bootout()
    with open(PLIST, "wb") as f:
        plistlib.dump(plist, f)
    r = subprocess.run(["launchctl", "bootstrap", DOMAIN, str(PLIST)], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"launchctl bootstrap failed: {r.stderr.strip()}")
    subprocess.run(["launchctl", "kickstart", "-k", f"{DOMAIN}/{LABEL}"], capture_output=True)
    print(f"Installed {LABEL}\n  plist: {PLIST}\n  logs:  {ROOT / 'logs'}/launchd.*.log")
    print(f"Status: launchctl print {DOMAIN}/{LABEL} | head -20")


def uninstall():
    _bootout()
    if PLIST.exists():
        PLIST.unlink()
    print(f"Removed {LABEL}")


if __name__ == "__main__":
    uninstall() if "--uninstall" in sys.argv else install()
