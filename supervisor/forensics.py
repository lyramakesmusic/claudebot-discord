"""Crash forensics helpers for supervisor."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def capture_crash_info(name: str, pid: int | None, exit_code: int, root: Path):
    """Record crash metadata and snapshot recent bot logs."""
    crash_time = datetime.now().isoformat()
    logs_dir = root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    entry = {
        "bot": name,
        "exit_code": exit_code,
        "timestamp": crash_time,
        "pid": pid,
    }
    with open(logs_dir / "crashes.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    log_file_map = {
        "claudebot": logs_dir / "claudebot.log",
        "codexbot": logs_dir / "codexbot.log",
        "selfbot": logs_dir / "selfbot.log",
        "supervisor": logs_dir / "supervisor.log",
    }
    log_file = log_file_map.get(name, logs_dir / f"{name}.log")
    if log_file.exists():
        try:
            lines = log_file.read_text("utf-8", errors="replace").splitlines()[-50:]
            snap_name = f"crash_{name}_{crash_time.replace(':', '-')}.txt"
            (logs_dir / snap_name).write_text("\n".join(lines), "utf-8")
        except Exception:
            pass
