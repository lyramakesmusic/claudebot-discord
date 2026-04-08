"""Restart a specific bot without needing it to be online.

Usage:
    python scripts/restart_bot.py claude     # restart claude bot
    python scripts/restart_bot.py codex      # restart codex bot
    python scripts/restart_bot.py kimi       # restart kimi bot
    python scripts/restart_bot.py selfbot    # restart selfbot
    python scripts/restart_bot.py all        # restart all bots (not supervisor)
    python scripts/restart_bot.py supervisor # full restart (supervisor + all bots)

This kills the target bot process(es) and lets the supervisor restart them.
For 'supervisor', it runs a full nuke_and_restart cycle.
"""

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

import psutil

from shared.lockfile import read_lock_pid

MY_PID = os.getpid()

# Map friendly names to lockfile names and script filenames
BOT_MAP = {
    "claude": {"lock": "claudebot", "script": "bot.py"},
    "codex": {"lock": "codexbot", "script": "codex_bot.py"},
    "kimi": {"lock": "kimibot", "script": "kimi_bot.py"},
    "selfbot": {"lock": "selfbot", "script": "self.py"},
}

ALL_BOTS = list(BOT_MAP.keys())


def _kill_bot(name: str) -> bool:
    """Kill a specific bot by name. Returns True if it was running and killed."""
    info = BOT_MAP[name]
    pid = read_lock_pid(info["lock"])

    if pid is None:
        print(f"  {name}: not running (no lockfile PID)")
        return False

    # Kill the real process (UV trampoline parent exits on its own)
    try:
        proc = psutil.Process(pid)
        proc.kill()
        print(f"  {name}: killed PID {pid}")
    except psutil.NoSuchProcess:
        print(f"  {name}: PID {pid} already dead")
    except psutil.AccessDenied:
        print(f"  {name}: ACCESS DENIED for PID {pid}")
        return False

    # Clean lockfile so the new instance can start
    lock_path = os.path.join(_ROOT, "data", f"{info['lock']}.lock")
    try:
        os.remove(lock_path)
    except OSError:
        pass

    # Clean heartbeat so the new instance gets a grace period
    hb_key = {"claudebot": "claude", "codexbot": "codex", "kimibot": "kimi"}.get(
        info["lock"], info["lock"]
    )
    hb_path = os.path.join(_ROOT, "data", f"heartbeat_{hb_key}.json")
    try:
        os.remove(hb_path)
    except OSError:
        pass

    return True


def _wait_for_bot(name: str, timeout: int = 15) -> bool:
    """Wait for the supervisor to restart the bot."""
    info = BOT_MAP[name]
    start = time.time()
    while time.time() - start < timeout:
        pid = read_lock_pid(info["lock"])
        if pid is not None:
            print(f"  {name}: back online (PID {pid})")
            return True
        time.sleep(1)
    print(f"  {name}: did NOT come back within {timeout}s!")
    return False


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("Usage: python scripts/restart_bot.py <bot|all|supervisor>")
        print(f"  Bots: {', '.join(ALL_BOTS)}")
        print("  all: restart all bots (supervisor stays up)")
        print("  supervisor: full nuke & restart")
        return

    target = sys.argv[1].lower()

    if target == "supervisor":
        print("Full supervisor restart requested — delegating to nuke_and_restart.py")
        os.execv(
            sys.executable,
            [sys.executable, os.path.join(_ROOT, "scripts", "nuke_and_restart.py")],
        )
        return

    if target == "all":
        targets = ALL_BOTS
    elif target in BOT_MAP:
        targets = [target]
    else:
        print(f"Unknown bot: {target}")
        print(f"Valid: {', '.join(ALL_BOTS)}, all, supervisor")
        sys.exit(1)

    # Check supervisor is running (needed to restart bots)
    sup_pid = read_lock_pid("supervisor")
    if sup_pid is None:
        print("WARNING: Supervisor is NOT running!")
        print("Bots won't auto-restart. Use 'supervisor' target for full restart.")
        print()

    print(f"=== Restarting: {', '.join(targets)} ===\n")

    # Kill
    print("Killing:")
    killed = []
    for name in targets:
        if _kill_bot(name):
            killed.append(name)

    if not killed:
        print("\nNothing was running. Nothing to restart.")
        if sup_pid is None:
            print("Supervisor is also down. Run: python scripts/nuke_and_restart.py")
        return

    # Wait for supervisor to restart them
    if sup_pid is not None:
        print(f"\nWaiting for supervisor to restart {len(killed)} bot(s)...")
        time.sleep(3)  # give supervisor time to detect the exit
        all_ok = True
        for name in killed:
            if not _wait_for_bot(name):
                all_ok = False

        print(f"\n{'SUCCESS' if all_ok else 'SOME BOTS DID NOT RESTART'}")
    else:
        print("\nSupervisor is down — cannot auto-restart. Run nuke_and_restart.py")


if __name__ == "__main__":
    main()
