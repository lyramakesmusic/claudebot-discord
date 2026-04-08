"""Supervisor watchdog — ensures supervisor stays alive.

Each bot starts a background thread that periodically checks if the supervisor
is still running. If the supervisor dies, the watchdog relaunches it.

This is the last line of defense: even if the supervisor crashes, as long as
ONE bot is alive, the supervisor (and thus all other bots) will be recovered.
"""

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from shared.lockfile import read_lock_pid, _is_pid_alive

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CHECK_INTERVAL = 15  # seconds between checks
_GRACE_PERIOD = 30    # seconds after startup before first check (let supervisor start)
_MAINTENANCE_FLAG = _PROJECT_ROOT / "data" / "maintenance.flag"
_watchdog_thread = None


def _venv_python() -> str:
    if os.name == "nt":
        return str(_PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe")
    return str(_PROJECT_ROOT / ".venv" / "bin" / "python")


def _supervisor_is_alive() -> bool:
    """Check if supervisor is running via its lockfile."""
    pid = read_lock_pid("supervisor")
    return pid is not None and _is_pid_alive(pid)


def _relaunch_supervisor():
    """Start run.py as a detached process (it loops and manages the supervisor)."""
    python = _venv_python()
    run_py = str(_PROJECT_ROOT / "run.py")
    create_flags = 0
    if os.name == "nt":
        create_flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    try:
        # run.py is the outermost loop. If it's already running, the supervisor
        # inside it holds a mutex and the duplicate will just exit harmlessly.
        subprocess.Popen(
            [python, run_py],
            cwd=str(_PROJECT_ROOT),
            creationflags=create_flags,
            close_fds=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.warning("Watchdog: relaunched run.py")
    except Exception as e:
        log.error(f"Watchdog: failed to relaunch run.py: {e}")


def _in_maintenance() -> bool:
    """Check if maintenance mode is active (clean restart in progress)."""
    return _MAINTENANCE_FLAG.exists()


def _watchdog_loop():
    """Background loop that monitors supervisor health."""
    time.sleep(_GRACE_PERIOD)  # wait for everything to settle on startup
    while True:
        try:
            if _in_maintenance():
                pass  # don't respawn during maintenance
            elif not _supervisor_is_alive():
                log.warning("Watchdog: supervisor not running, relaunching...")
                _relaunch_supervisor()
                time.sleep(_GRACE_PERIOD)  # give it time to start
        except Exception as e:
            log.error(f"Watchdog error: {e}")
        time.sleep(_CHECK_INTERVAL)


def start_watchdog():
    """Start the supervisor watchdog in a daemon thread.
    Safe to call multiple times — only starts once."""
    global _watchdog_thread
    if _watchdog_thread is not None and _watchdog_thread.is_alive():
        return
    _watchdog_thread = threading.Thread(target=_watchdog_loop, daemon=True, name="supervisor-watchdog")
    _watchdog_thread.start()
    log.info("Supervisor watchdog started")
