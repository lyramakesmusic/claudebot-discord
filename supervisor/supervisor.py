"""Main supervisor loop and process orchestration."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from supervisor.forensics import capture_crash_info
from supervisor.health import check_heartbeat, write_supervisor_heartbeat
from supervisor.process import BotProcess

WATCH_FILES = [".env"]
RESTART_DELAY = 2
POLL_INTERVAL = 1
MAX_RAPID_RESTARTS = 5
RAPID_WINDOW = 30
BACKOFF_DELAY = 15
CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


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


def _get_mtimes(root: Path) -> dict[str, float]:
    out = {}
    for name in WATCH_FILES:
        p = root / name
        if p.exists():
            out[name] = p.stat().st_mtime
    return out


def _venv_python(root: Path) -> str:
    if os.name == "nt":
        return str(root / ".venv" / "Scripts" / "python.exe")
    return str(root / ".venv" / "bin" / "python")


def _start_claude(root: Path) -> subprocess.Popen:
    return subprocess.Popen([
        _venv_python(root), str(root / "bot.py")
    ], cwd=str(root), creationflags=CREATE_FLAGS)


def _start_codex(root: Path) -> subprocess.Popen:
    return subprocess.Popen([
        _venv_python(root), str(root / "codex_bot.py")
    ], cwd=str(root), creationflags=CREATE_FLAGS)


def _start_selfbot(root: Path) -> subprocess.Popen | None:
    script = root / "selfbot" / "self.py"
    if not script.exists():
        return None
    return subprocess.Popen([
        sys.executable, str(script)
    ], cwd=str(root), creationflags=CREATE_FLAGS)


def main():
    root = Path(__file__).resolve().parent.parent
    log = _setup_logging(root)
    mtimes = _get_mtimes(root)

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

            new_mtimes = _get_mtimes(root)
            changed = [f for f in WATCH_FILES if new_mtimes.get(f) != mtimes.get(f)]
            if changed:
                mtimes = new_mtimes
                log.info("file changed: %s; restarting all", ", ".join(changed))
                for bp in bots.values():
                    bp.terminate()
                time.sleep(RESTART_DELAY)
                for name, bp in bots.items():
                    bp.start()
                    log.info("%s restarted (pid=%s)", name, bp.pid)
                continue

            for name, bp in bots.items():
                ret = bp.poll()
                if ret is not None:
                    log.warning("%s exited with code %s", name, ret)
                    capture_crash_info(name, bp.pid, ret, root)
                    bp.register_crash_backoff()
                    bp.start()
                    log.info("%s restarted (pid=%s)", name, bp.pid)
                    continue

                if not check_heartbeat(root, "claude" if name == "claudebot" else "codex" if name == "codexbot" else "selfbot"):
                    log.warning("%s heartbeat stale, restarting", name)
                    capture_crash_info(name, bp.pid, -100, root)
                    bp.terminate()
                    bp.register_crash_backoff()
                    bp.start()
                    log.info("%s restarted after stale heartbeat (pid=%s)", name, bp.pid)

            write_supervisor_heartbeat(root, bots)

    except KeyboardInterrupt:
        log.info("shutting down")
        for bp in bots.values():
            bp.terminate()


if __name__ == "__main__":
    main()
