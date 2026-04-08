"""PID lockfile for single-instance enforcement.

Each bot and the supervisor acquire a named OS mutex on startup.
On Windows: uses kernel32 CreateMutexW (atomic, auto-released on process death).
On Unix: uses fcntl.flock on a lockfile (auto-released on process death).
The PID lockfile is kept as a convenience for other processes to read.
"""

import atexit
import logging
import os
import sys
from pathlib import Path

_LOCK_DIR = Path(__file__).resolve().parent.parent / "data"
_log = logging.getLogger(__name__)

# Keep references alive so they aren't GC'd
_held_mutexes: dict[str, object] = {}


def _is_pid_alive(pid: int) -> bool:
    """Check if a PID is still running (Windows + Unix)."""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except (OSError, PermissionError):
        return False


def acquire_lock(name: str) -> bool:
    """Try to acquire a singleton lock for the given bot name.

    On Windows: uses a named mutex (Global\\claudebot_{name}).
    On Unix: uses fcntl.flock on a lockfile.
    Both are atomic and auto-release when the process dies.

    Returns True if lock acquired (we should run).
    Returns False if another instance is already running (we should exit).
    """
    _LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _LOCK_DIR / f"{name}.lock"
    my_pid = os.getpid()

    if os.name == "nt":
        import ctypes
        import ctypes.wintypes

        # Must use WinDLL with use_last_error=True to capture GetLastError
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p,           # lpMutexAttributes
            ctypes.wintypes.BOOL,      # bInitialOwner
            ctypes.wintypes.LPCWSTR,   # lpName
        ]

        ERROR_ALREADY_EXISTS = 183
        mutex_name = f"Global\\claudebot_{name}"

        # CreateMutexW: if the mutex already exists, it returns a handle but
        # GetLastError() == ERROR_ALREADY_EXISTS. If it's new, we own it.
        handle = kernel32.CreateMutexW(None, True, mutex_name)
        last_error = ctypes.get_last_error()
        _log.info("lockfile[%s] pid=%d mutex=%r handle=%s last_error=%d",
                   name, my_pid, mutex_name, handle, last_error)

        if handle == 0 or handle is None:
            _log.warning("lockfile[%s] CreateMutexW returned null handle", name)
            return False

        if last_error == ERROR_ALREADY_EXISTS:
            # Another process owns this mutex — we're a duplicate
            _log.warning("lockfile[%s] mutex already exists — another instance running", name)
            kernel32.CloseHandle(handle)
            return False

        _log.info("lockfile[%s] mutex acquired successfully", name)
        # We own the mutex — keep handle alive
        _held_mutexes[name] = handle

    else:
        # Unix: use flock
        import fcntl
        flock_path = _LOCK_DIR / f"{name}.flock"
        fd = os.open(str(flock_path), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            os.close(fd)
            return False
        _held_mutexes[name] = fd

    # Write PID file for other processes to read
    try:
        lock_path.write_text(str(my_pid))
    except OSError:
        pass

    # Register cleanup
    def _release():
        try:
            if lock_path.exists() and lock_path.read_text().strip() == str(my_pid):
                lock_path.unlink()
        except Exception:
            pass
        ref = _held_mutexes.pop(name, None)
        if ref is not None:
            if os.name == "nt":
                import ctypes
                try:
                    ctypes.windll.kernel32.ReleaseMutex(ref)
                    ctypes.windll.kernel32.CloseHandle(ref)
                except Exception:
                    pass
            else:
                try:
                    os.close(ref)
                except Exception:
                    pass

    atexit.register(_release)
    return True


LOCKFILE_EXIT_CODE = 78  # Distinctive: "already running, don't restart me"
LOCKFILE_STDERR_MARKER = "mutex already exists"  # Fallback detection via stderr


def acquire_or_exit(name: str):
    """Acquire lock or exit immediately if duplicate."""
    if not acquire_lock(name):
        lock_path = _LOCK_DIR / f"{name}.lock"
        try:
            old_pid = int(lock_path.read_text().strip())
        except Exception:
            old_pid = "?"
        print(f"[{name}] Another instance is already running (PID {old_pid}). Exiting.")
        sys.exit(LOCKFILE_EXIT_CODE)


def read_lock_pid(name: str) -> int | None:
    """Read the PID from a lockfile, or None if no valid lock."""
    lock_path = _LOCK_DIR / f"{name}.lock"
    if not lock_path.exists():
        return None
    try:
        pid = int(lock_path.read_text().strip())
        if _is_pid_alive(pid):
            return pid
    except (ValueError, OSError):
        pass
    return None
