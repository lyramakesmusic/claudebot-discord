"""Nuclear restart: kill ALL claudebot processes and start one fresh supervisor.

Usage:
    python scripts/nuke_and_restart.py           # kill all + restart
    python scripts/nuke_and_restart.py --kill     # kill all, don't restart

Targets only claudebot bot/supervisor processes. Does NOT kill unrelated Python
processes. UV trampolines (launcher+child pairs) are expected and handled.
"""

import glob
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

import subprocess

import psutil

MY_PID = os.getpid()
MAINTENANCE_FLAG = os.path.join(_ROOT, "data", "maintenance.flag")

# These are the EXACT script filenames that claudebot uses as entry points
BOT_SCRIPTS = {"run.py", "bot.py", "codex_bot.py", "self.py", "supervisor.py"}


def _is_claudebot_process(proc: psutil.Process) -> bool:
    """Check if a process is a claudebot bot/supervisor.

    Uses absolute paths in cmdline to avoid false positives from other projects
    (e.g., trinity-sae also has a bot.py). Also catches UV trampoline parents
    by checking if their child is a claudebot process.
    """
    try:
        parts = proc.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False

    if not parts:
        return False

    for part in parts:
        basename = os.path.basename(part)
        if basename in BOT_SCRIPTS and os.path.isabs(part) and "claudebot" in part.lower():
            return True
    return False


def find_claudebot_pids() -> list[int]:
    """Find all claudebot process PIDs (including UV trampoline wrappers)."""
    direct_pids = set()
    for p in psutil.process_iter(["pid"]):
        try:
            if p.info["pid"] == MY_PID:
                continue
            if _is_claudebot_process(p):
                direct_pids.add(p.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # Also find UV trampoline parents: if a process's only child is a
    # claudebot process and it's also python, it's a trampoline wrapper.
    all_pids = set(direct_pids)
    for pid in list(direct_pids):
        try:
            p = psutil.Process(pid)
            ppid = p.ppid()
            if ppid and ppid != MY_PID and ppid not in all_pids:
                parent = psutil.Process(ppid)
                if "python" in parent.name().lower():
                    all_pids.add(ppid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    return sorted(all_pids)


def kill_pids(pids: list[int]):
    """Force-kill a list of PIDs."""
    for pid in pids:
        try:
            psutil.Process(pid).kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


def _count_unique_bots() -> dict[str, int]:
    """Count unique claudebot processes (excluding trampoline duplicates).

    Only counts the 'real' process (the child in a trampoline pair).
    Uses lockfiles as the source of truth for running processes.
    """
    from shared.lockfile import read_lock_pid

    counts = {}
    for name in ["supervisor", "claudebot", "codexbot", "kimibot", "selfbot"]:
        pid = read_lock_pid(name)
        if pid is not None:
            counts[name] = pid
    return counts


def main():
    kill_only = "--kill" in sys.argv

    print(f"=== Claudebot {'Kill' if kill_only else 'Nuke & Restart'} (self PID {MY_PID}) ===\n")

    # Step 1: Maintenance mode
    os.makedirs(os.path.dirname(MAINTENANCE_FLAG), exist_ok=True)
    with open(MAINTENANCE_FLAG, "w") as f:
        f.write(str(MY_PID))
    print("Maintenance mode: ON")

    # Step 2: Kill loop
    attempt = 0
    while True:
        attempt += 1
        pids = find_claudebot_pids()
        if not pids:
            if attempt == 1:
                time.sleep(2)
                pids = find_claudebot_pids()
                if not pids:
                    break
            else:
                break

        print(f"  Kill round {attempt}: {len(pids)} PIDs")
        kill_pids(pids)
        time.sleep(2)

        if attempt > 15:
            print("ERROR: Cannot kill all processes after 15 attempts!")
            try:
                os.remove(MAINTENANCE_FLAG)
            except OSError:
                pass
            sys.exit(1)

    print(f"  All dead after {attempt} round(s).\n")

    # Step 3: Clean stale state
    for pattern in ["data/*.lock", "data/heartbeat_*.json", "data/eventloop_*.json"]:
        for f in glob.glob(os.path.join(_ROOT, pattern)):
            try:
                os.remove(f)
            except OSError:
                pass
    print("  Stale lockfiles/heartbeats cleaned.")

    if kill_only:
        try:
            os.remove(MAINTENANCE_FLAG)
        except OSError:
            pass
        print("\nMaintenance mode: OFF")
        print("DONE (kill only)")
        return

    # Step 4: Start fresh supervisor
    print("\nStarting supervisor...")
    venv_pythonw = os.path.join(_ROOT, ".venv", "Scripts", "pythonw.exe")
    if not os.path.exists(venv_pythonw):
        venv_pythonw = os.path.join(_ROOT, ".venv", "bin", "python")
    run_py = os.path.join(_ROOT, "run.py")

    p = subprocess.Popen(
        [venv_pythonw, run_py],
        cwd=_ROOT,
        creationflags=(
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
        if os.name == "nt"
        else 0,
        close_fds=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"  Supervisor launched (PID {p.pid})")

    # Step 5: Wait for bots
    print("  Waiting 10s for bots to come up...")
    time.sleep(10)

    # Step 6: Remove maintenance flag
    try:
        os.remove(MAINTENANCE_FLAG)
    except OSError:
        pass
    print("Maintenance mode: OFF\n")

    # Step 7: Verify via lockfiles (immune to UV trampoline doubling)
    time.sleep(2)
    running = _count_unique_bots()
    print("Process status:")
    all_ok = True
    for name in ["supervisor", "claudebot", "codexbot", "kimibot", "selfbot"]:
        pid = running.get(name)
        if pid:
            print(f"  {name}: OK (PID {pid})")
        else:
            print(f"  {name}: MISSING!")
            all_ok = False

    print(f"\n{'SUCCESS' if all_ok else 'CHECK ABOVE'}")


if __name__ == "__main__":
    main()
