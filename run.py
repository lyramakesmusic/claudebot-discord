#!/usr/bin/env python3
"""
claudebot supervisor — runs bot.py, codex_bot.py, and selfbot with auto-restart.

Watches .env for config changes. Restarts all processes when:
  - any process crashes (any exit code)
  - .env is modified
  - a bot exits cleanly (hot reload after self-edit)

Usage: python run.py
"""

import os
import sys
import subprocess
import time
from pathlib import Path

WATCH_DIR = Path(__file__).parent
WATCH_FILES = [".env"]
RESTART_DELAY = 2       # seconds between restart attempts
POLL_INTERVAL = 1       # seconds between file change checks
MAX_RAPID_RESTARTS = 5  # if it crashes this many times in RAPID_WINDOW, back off
RAPID_WINDOW = 30       # seconds
BACKOFF_DELAY = 15      # seconds to wait after rapid crash loop

CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def get_mtimes() -> dict[str, float]:
    mtimes = {}
    for name in WATCH_FILES:
        p = WATCH_DIR / name
        if p.exists():
            mtimes[name] = p.stat().st_mtime
    return mtimes


SELFBOT_SCRIPT = WATCH_DIR / "selfbot" / "self.py"
CODEX_BOT_SCRIPT = WATCH_DIR / "codex_bot.py"


VENV_PYTHON = str(WATCH_DIR / ".venv" / "Scripts" / "python.exe") if os.name == "nt" else str(WATCH_DIR / ".venv" / "bin" / "python")


def run_bot() -> subprocess.Popen:
    # Use venv python directly — uv run's trampoline on Windows spawns
    # a parent+child python pair that both run the script, causing double logins.
    return subprocess.Popen(
        [VENV_PYTHON, str(WATCH_DIR / "bot.py")],
        cwd=str(WATCH_DIR),
        creationflags=CREATE_FLAGS,
    )


def run_selfbot() -> subprocess.Popen | None:
    if not SELFBOT_SCRIPT.exists():
        return None
    return subprocess.Popen(
        [sys.executable, str(SELFBOT_SCRIPT)],
        cwd=str(WATCH_DIR),
        creationflags=CREATE_FLAGS,
    )


def run_codex_bot() -> subprocess.Popen | None:
    if not CODEX_BOT_SCRIPT.exists():
        return None
    return subprocess.Popen(
        [VENV_PYTHON, str(CODEX_BOT_SCRIPT)],
        cwd=str(WATCH_DIR),
        creationflags=CREATE_FLAGS,
    )


def _terminate(proc):
    if proc is None:
        return
    # On Windows, proc.terminate() only kills the wrapper (uv), not child python.
    # Use psutil to kill the entire process tree.
    try:
        import psutil
        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
        for child in children:
            child.kill()
        parent.kill()
        parent.wait(timeout=10)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    except ImportError:
        # fallback if psutil not available
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def main():
    print(f"[supervisor] watching: {', '.join(WATCH_FILES)}")
    print(f"[supervisor] starting bots...")

    recent_crashes: list[float] = []
    mtimes = get_mtimes()
    proc = run_bot()
    selfbot = run_selfbot()
    codex = run_codex_bot()
    if selfbot:
        print("[supervisor] selfbot started")
    if codex:
        print("[supervisor] codex_bot started")

    try:
        while True:
            time.sleep(POLL_INTERVAL)

            # check for file changes
            new_mtimes = get_mtimes()
            changed = [f for f in WATCH_FILES if new_mtimes.get(f) != mtimes.get(f)]
            if changed:
                print(f"[supervisor] file changed: {', '.join(changed)} — restarting all")
                mtimes = new_mtimes
                _terminate(proc)
                _terminate(selfbot)
                _terminate(codex)
                time.sleep(RESTART_DELAY)
                proc = run_bot()
                selfbot = run_selfbot()
                codex = run_codex_bot()
                continue

            # check if bot died
            ret = proc.poll()
            if ret is not None:
                print(f"[supervisor] bot exited with code {ret}")

                now = time.time()
                recent_crashes = [t for t in recent_crashes if now - t < RAPID_WINDOW]
                recent_crashes.append(now)

                if len(recent_crashes) >= MAX_RAPID_RESTARTS:
                    print(f"[supervisor] {MAX_RAPID_RESTARTS} crashes in {RAPID_WINDOW}s — backing off {BACKOFF_DELAY}s")
                    time.sleep(BACKOFF_DELAY)
                    recent_crashes.clear()
                else:
                    time.sleep(RESTART_DELAY)

                mtimes = get_mtimes()
                print("[supervisor] restarting bot + selfbot...")
                _terminate(selfbot)
                _terminate(codex)
                proc = run_bot()
                selfbot = run_selfbot()
                codex = run_codex_bot()

            # check if selfbot died
            if selfbot and selfbot.poll() is not None:
                print(f"[supervisor] selfbot exited with code {selfbot.returncode}")
                time.sleep(RESTART_DELAY)
                selfbot = run_selfbot()

            # check if codex_bot died
            if codex and codex.poll() is not None:
                print(f"[supervisor] codex_bot exited with code {codex.returncode}")
                time.sleep(RESTART_DELAY)
                codex = run_codex_bot()

    except KeyboardInterrupt:
        print("\n[supervisor] shutting down...")
        _terminate(proc)
        _terminate(selfbot)
        _terminate(codex)
        print("[supervisor] done")


if __name__ == "__main__":
    main()
