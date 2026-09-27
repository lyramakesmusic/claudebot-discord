"""Stop codex bot without auto-restart.

Kills codex and temporarily pauses the supervisor's codex respawn
by creating a sentinel file. Run start_codex.py when ready to bring it back.

Usage:
    python scripts/stop_codex.py
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

import psutil
from shared.lockfile import read_lock_pid

SENTINEL = os.path.join(_ROOT, "data", "codex_paused.sentinel")


def main():
    # Create sentinel BEFORE killing so supervisor doesn't respawn
    with open(SENTINEL, "w") as f:
        f.write("paused for update\n")
    print(f"Created pause sentinel: {SENTINEL}")

    # Kill codex
    pid = read_lock_pid("codexbot")
    if pid is None:
        print("Codex not running (no lockfile PID)")
    else:
        try:
            proc = psutil.Process(pid)
            proc.kill()
            print(f"Killed codex PID {pid}")
        except psutil.NoSuchProcess:
            print(f"Codex PID {pid} already dead")
        except psutil.AccessDenied:
            print(f"ACCESS DENIED for PID {pid}")

    # Clean lockfile
    lock_path = os.path.join(_ROOT, "data", "codexbot.lock")
    try:
        os.remove(lock_path)
    except OSError:
        pass

    print()
    print("Codex is stopped and supervisor won't respawn it.")
    print("Do your update now (e.g. npm update -g @openai/codex)")
    print("Then run: python scripts/start_codex.py")


if __name__ == "__main__":
    main()
