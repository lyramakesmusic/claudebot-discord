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

    def start(self):
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
        return self.proc.poll() if self.proc else None

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def register_crash_backoff(self):
        now = time.time()
        self.recent_crashes = [t for t in self.recent_crashes if now - t < self.rapid_window]
        self.recent_crashes.append(now)
        if len(self.recent_crashes) >= self.max_rapid_restarts:
            time.sleep(self.backoff_delay)
            self.recent_crashes.clear()
        else:
            time.sleep(self.restart_delay)

    def terminate(self, timeout: int = 10):
        if not self.proc:
            return
        _terminate_tree(self.proc, timeout=timeout)


def _terminate_tree(proc: subprocess.Popen, timeout: int = 10):
    """Terminate a process tree with graceful-first behavior."""
    if proc is None:
        return
    try:
        if os.name == "nt":
            try:
                os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
                proc.wait(timeout=timeout)
                return
            except Exception:
                pass
        else:
            try:
                proc.terminate()
                proc.wait(timeout=timeout)
                return
            except Exception:
                pass

        try:
            import psutil

            parent = psutil.Process(proc.pid)
            children = parent.children(recursive=True)
            for child in children:
                child.kill()
            parent.kill()
            parent.wait(timeout=timeout)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=timeout)
            except Exception:
                pass
    except Exception:
        pass
