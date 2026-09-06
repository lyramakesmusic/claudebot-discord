"""Full-restart launcher: kills the whole claudebot process tree and relaunches it.

Windows runs scripts/restart.ps1 under PowerShell; macOS/Linux run scripts/restart.sh.
The child is fully detached so it survives the caller exiting.
"""

import os
import subprocess
from pathlib import Path


def restart_script_path(project_root: Path) -> Path:
    name = "restart.ps1" if os.name == "nt" else "restart.sh"
    return Path(project_root) / "scripts" / name


def launch_full_restart(project_root: Path) -> subprocess.Popen:
    script = restart_script_path(project_root)
    if os.name == "nt":
        return subprocess.Popen(
            ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
            close_fds=True,
        )
    return subprocess.Popen(
        ["/bin/zsh", str(script)],
        cwd=str(project_root),
        start_new_session=True,
        close_fds=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
