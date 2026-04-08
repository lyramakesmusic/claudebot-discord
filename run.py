#!/usr/bin/env python3
"""Supervisor entry point — resilient loop.

This is the outermost layer of the persistence chain:

    Scheduled Task (Windows runs this at logon)
      → run.py (this file — restarts supervisor if it exits)
        → supervisor (restarts bots if they exit)
          → bot processes

run.py's only job: run the supervisor, and if it ever exits, run it again.
The Scheduled Task's only job: run run.py, and if it ever exits, run it again.
"""

import logging
import os
import subprocess
import sys
import time

_root = os.path.dirname(os.path.abspath(__file__))
if _root not in sys.path:
    sys.path.insert(0, _root)
os.chdir(_root)

_MAINTENANCE_FLAG = os.path.join(_root, "data", "maintenance.flag")
_RESTART_DELAY = 3
_CRASH_BACKOFF = 30  # if supervisor crashes fast, wait longer


def _setup_logging():
    logs_dir = os.path.join(_root, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    logger = logging.getLogger("run")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        fh = logging.FileHandler(os.path.join(logs_dir, "run.log"), encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(sh)
    return logger


def main():
    from shared.lockfile import LOCKFILE_EXIT_CODE, acquire_lock
    if not acquire_lock("run"):
        print("[run.py] Another instance already running. Exiting.")
        sys.exit(0)

    log = _setup_logging()
    python = sys.executable
    supervisor_py = os.path.join(_root, "supervisor", "supervisor.py")
    log.info("run.py started (pid=%d)", os.getpid())

    while True:
        # Maintenance mode: nuke_and_restart is doing a clean cycle.
        # Wait until it's done before starting the supervisor.
        if os.path.exists(_MAINTENANCE_FLAG):
            time.sleep(2)
            continue

        log.info("Starting supervisor...")
        start = time.time()
        try:
            result = subprocess.run(
                [python, "-u", supervisor_py],
                cwd=_root,
            )
            code = result.returncode
        except KeyboardInterrupt:
            log.info("KeyboardInterrupt — exiting")
            break
        except Exception as e:
            log.error("Failed to start supervisor: %s", e)
            code = 1

        uptime = time.time() - start

        # Clean exit (0) from KeyboardInterrupt in supervisor — user wants to stop
        if code == 0:
            log.info("Supervisor exited cleanly — stopping")
            break

        # Supervisor lock conflict (another supervisor currently owns mutex):
        # retry quickly; this is not a real crash.
        if code == LOCKFILE_EXIT_CODE:
            delay = _RESTART_DELAY
            log.info("Supervisor lock is held by another instance (code=%s), retrying in %ds", code, delay)
        # Supervisor crashed
        elif uptime < 10:
            delay = _CRASH_BACKOFF
            log.warning("Supervisor crashed after %.1fs (code=%s), backing off %ds",
                        uptime, code, delay)
        else:
            delay = _RESTART_DELAY
            log.warning("Supervisor exited (code=%s, uptime=%.0fs), restarting in %ds",
                        code, uptime, delay)

        time.sleep(delay)


if __name__ == "__main__":
    main()
