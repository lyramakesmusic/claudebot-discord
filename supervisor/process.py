"""Per-bot process lifecycle for supervisor."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class BotProcess:
    """Manage a single subprocess with restart/backoff bookkeeping."""

    name: str
    start_fn: Callable[[], subprocess.Popen]
    rapid_window: int
    max_rapid_restarts: int
    restart_delay: int
    backoff_delay: int
    proc: subprocess.Popen | None = None
    recent_crashes: list[float] = field(default_factory=list)
    start_time: float = 0.0
    disabled: bool = False
    _consecutive_fast_crashes: int = 0  # crashes where uptime < 10s

    def start(self):
        if self.disabled:
            return
        self.proc = self.start_fn()
        self.start_time = time.time()

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    @property
    def uptime(self) -> float:
        if not self.proc or not self.is_alive():
            return 0.0
        return time.time() - self.start_time

    def poll(self) -> int | None:
        if self.disabled:
            return None  # don't report exits for disabled bots
        return self.proc.poll() if self.proc else None

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def register_crash_backoff(self):
        """Register a crash and apply backoff. Never disables — always restarts."""
        now = time.time()
        uptime = now - self.start_time if self.start_time else 0

        if uptime < 10:
            self._consecutive_fast_crashes += 1
        else:
            self._consecutive_fast_crashes = 0

        self.recent_crashes = [t for t in self.recent_crashes if now - t < self.rapid_window]
        self.recent_crashes.append(now)

        # Escalating backoff: more crashes = longer wait, but always restart
        if self._consecutive_fast_crashes >= self.max_rapid_restarts:
            delay = 60  # 1 minute cooldown after repeated instant crashes
        elif len(self.recent_crashes) >= self.max_rapid_restarts:
            delay = self.backoff_delay
            self.recent_crashes.clear()
        else:
            delay = self.restart_delay

        time.sleep(delay)

    def enable(self):
        """Re-enable a disabled bot (e.g. after a fix is deployed)."""
        self.disabled = False
        self._consecutive_fast_crashes = 0
        self.recent_crashes.clear()

    def terminate(self, timeout: int = 10):
        if not self.proc:
            return
        _terminate_tree(self.proc, timeout=timeout)


def _terminate_tree(proc: subprocess.Popen, timeout: int = 10):
    """Terminate a process tree.

    On Windows: uses psutil to kill only the target process tree.
    NEVER uses CTRL_BREAK_EVENT — that signal hits the entire process group
    and can kill the supervisor, sibling bots, and unrelated processes.
    """
    if proc is None:
        return
    try:
        import psutil

        try:
            parent = psutil.Process(proc.pid)
            children = parent.children(recursive=True)
            for child in children:
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            parent.kill()
            parent.wait(timeout=timeout)
        except psutil.NoSuchProcess:
            pass  # already dead
        except Exception:
            # fallback: just kill the process directly
            try:
                proc.kill()
                proc.wait(timeout=timeout)
            except Exception:
                pass
    except ImportError:
        # psutil not available — direct kill only
        try:
            proc.kill()
            proc.wait(timeout=timeout)
        except Exception:
            pass
