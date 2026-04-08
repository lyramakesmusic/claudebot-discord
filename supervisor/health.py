"""Health utilities for supervisor."""

from __future__ import annotations

import json
import time
from pathlib import Path


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
