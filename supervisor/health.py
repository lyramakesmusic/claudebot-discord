"""Heartbeat health utilities for supervisor."""

from __future__ import annotations

import json
import time
from pathlib import Path

HEARTBEAT_TIMEOUT = 120


def check_heartbeat(root: Path, name: str) -> bool:
    """Return True when heartbeat is fresh or not yet created."""
    hb_file = root / "data" / f"heartbeat_{name}.json"
    if not hb_file.exists():
        return True
    try:
        data = json.loads(hb_file.read_text("utf-8"))
        age = time.time() - float(data.get("timestamp", 0))
        return age < HEARTBEAT_TIMEOUT
    except Exception:
        return True


def write_supervisor_heartbeat(root: Path, bots: dict):
    """Write supervisor + child process status."""
    data = {
        "timestamp": time.time(),
        "bots": {
            name: {
                "pid": bp.pid,
                "alive": bp.is_alive(),
                "uptime": bp.uptime,
            }
            for name, bp in bots.items()
        },
    }
    hb_file = root / "data" / "supervisor_heartbeat.json"
    hb_file.parent.mkdir(parents=True, exist_ok=True)
    hb_file.write_text(json.dumps(data, indent=2), "utf-8")
