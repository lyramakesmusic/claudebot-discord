"""Resume codex bot after stop_codex.py.

Removes the pause sentinel and lets the supervisor restart codex normally.

Usage:
    python scripts/start_codex.py
"""

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

from shared.lockfile import read_lock_pid

SENTINEL = os.path.join(_ROOT, "data", "codex_paused.sentinel")


def main():
    # Remove sentinel
    try:
        os.remove(SENTINEL)
        print("Removed pause sentinel — supervisor will respawn codex")
    except FileNotFoundError:
        print("No sentinel found (codex wasn't paused?)")

    # Clean stale heartbeat so supervisor gives grace period
    hb_path = os.path.join(_ROOT, "data", "heartbeat_codex.json")
    try:
        os.remove(hb_path)
    except OSError:
        pass

    # Wait for supervisor to restart it
    sup_pid = read_lock_pid("supervisor")
    if sup_pid is None:
        print("WARNING: Supervisor not running! Start it manually.")
        return

    print("Waiting for codex to come back...")
    start = time.time()
    while time.time() - start < 20:
        pid = read_lock_pid("codexbot")
        if pid is not None:
            print(f"Codex is back online (PID {pid})")
            return
        time.sleep(1)

    print("Codex didn't come back within 20s — check supervisor logs")


if __name__ == "__main__":
    main()
