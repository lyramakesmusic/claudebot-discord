"""Main supervisor loop and process orchestration."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

# Ensure project root is on sys.path (needed when run as subprocess)
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from shared.lockfile import LOCKFILE_EXIT_CODE, LOCKFILE_STDERR_MARKER
from supervisor.forensics import capture_crash_info
from supervisor.health import write_supervisor_heartbeat
from supervisor.process import BotProcess

RESTART_DELAY = 2
POLL_INTERVAL = 1
MAX_RAPID_RESTARTS = 5
RAPID_WINDOW = 30
BACKOFF_DELAY = 15
LOCK_CONFLICT_RETRY_DELAY = 5
# CREATE_NEW_PROCESS_GROUP isolates each bot so a signal to one
# can't cascade and kill the supervisor, siblings, or unrelated processes.
CREATE_FLAGS = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0


def _setup_logging(root: Path) -> logging.Logger:
    logs_dir = root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("supervisor")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        fh = logging.FileHandler(logs_dir / "supervisor.log", encoding="utf-8")
        fh.setFormatter(formatter)
        sh = logging.StreamHandler()
        sh.setFormatter(formatter)
        logger.addHandler(fh)
        logger.addHandler(sh)
    return logger



def _venv_python(root: Path) -> str:
    if os.name == "nt":
        return str(root / ".venv" / "Scripts" / "python.exe")
    return str(root / ".venv" / "bin" / "python")


def _start_bot(cmd: list[str], cwd: str) -> subprocess.Popen:
    """Start a bot process. stderr goes to devnull to prevent pipe buffer deadlocks.

    Previously stderr was captured via PIPE, but nobody read the pipe while
    the process was alive. After hours of discord.py logging to stderr, the
    64KB pipe buffer filled up and the write() call blocked while holding
    Python's logging lock — deadlocking the event loop and the Discord
    heartbeat thread simultaneously.
    """
    return subprocess.Popen(
        cmd, cwd=cwd, creationflags=CREATE_FLAGS,
        stderr=subprocess.DEVNULL,
    )


def _start_claude(root: Path) -> subprocess.Popen:
    return _start_bot(
        [_venv_python(root), str(root / "bot.py")], str(root),
    )


def _start_codex(root: Path) -> subprocess.Popen:
    return _start_bot(
        [_venv_python(root), str(root / "codex_bot.py")], str(root),
    )



def _start_selfbot(root: Path) -> subprocess.Popen | None:
    script = root / "selfbot" / "self.py"
    if not script.exists():
        return None
    # selfbot uses its own venv (discord.py-self conflicts with discord.py)
    selfbot_dir = root / "selfbot"
    python = _venv_python(selfbot_dir) if (selfbot_dir / ".venv").exists() else _venv_python(root)
    return _start_bot(
        [python, str(script)], str(root),
    )




def main():
    root = Path(__file__).resolve().parent.parent

    # Acquire supervisor lock — prevents duplicate supervisors.
    from shared.lockfile import acquire_or_exit
    acquire_or_exit("supervisor")

    log = _setup_logging(root)

    bots: dict[str, BotProcess] = {
        "claudebot": BotProcess(
            name="claudebot",
            start_fn=lambda: _start_claude(root),
            rapid_window=RAPID_WINDOW,
            max_rapid_restarts=MAX_RAPID_RESTARTS,
            restart_delay=RESTART_DELAY,
            backoff_delay=BACKOFF_DELAY,
        ),
        "codexbot": BotProcess(
            name="codexbot",
            start_fn=lambda: _start_codex(root),
            rapid_window=RAPID_WINDOW,
            max_rapid_restarts=MAX_RAPID_RESTARTS,
            restart_delay=RESTART_DELAY,
            backoff_delay=BACKOFF_DELAY,
        ),
    }

    sb_proc = _start_selfbot(root)
    if sb_proc is not None:
        bots["selfbot"] = BotProcess(
            name="selfbot",
            start_fn=lambda: _start_selfbot(root),
            rapid_window=RAPID_WINDOW,
            max_rapid_restarts=MAX_RAPID_RESTARTS,
            restart_delay=RESTART_DELAY,
            backoff_delay=BACKOFF_DELAY,
            proc=sb_proc,
            start_time=time.time(),
        )

    for name, bp in bots.items():
        if not bp.proc:
            bp.start()
        log.info("%s started (pid=%s)", name, bp.pid)

    try:
        while True:
            time.sleep(POLL_INTERVAL)

            for name, bp in bots.items():
                ret = bp.poll()
                if ret is None:
                    continue

                # Lockfile duplicate: another instance is already running.
                if ret == LOCKFILE_EXIT_CODE:
                    log.info(
                        "%s exited: another instance already running (code=%s); retrying in %ss",
                        name, ret, LOCK_CONFLICT_RETRY_DELAY,
                    )
                    time.sleep(LOCK_CONFLICT_RETRY_DELAY)
                    bp.start()
                    log.info("%s restart retry (pid=%s)", name, bp.pid)
                    continue

                # Bot died on its own. Log, back off, restart.
                uptime = time.time() - bp.start_time if bp.start_time else 0
                log.warning(
                    "%s exited with code %s (pid=%s uptime=%.0fs)",
                    name, ret, bp.pid, uptime,
                )
                capture_crash_info(name, bp.pid, ret, root, uptime=uptime)
                bp.register_crash_backoff()
                bp.start()
                log.info("%s restarted (pid=%s)", name, bp.pid)

            write_supervisor_heartbeat(root, bots)

    except KeyboardInterrupt:
        log.info("shutting down")
        for bp in bots.values():
            bp.terminate()


if __name__ == "__main__":
    main()
