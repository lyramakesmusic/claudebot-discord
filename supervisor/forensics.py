"""Crash forensics helpers for supervisor.

When a bot dies, capture everything we might need to diagnose it later.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path


def _system_snapshot() -> dict:
    """Capture system resource state at crash time."""
    info = {}
    try:
        import psutil
        vm = psutil.virtual_memory()
        info["memory"] = {
            "total_gb": round(vm.total / (1024**3), 1),
            "available_gb": round(vm.available / (1024**3), 1),
            "percent_used": vm.percent,
        }
        info["cpu_percent"] = psutil.cpu_percent(interval=0.1)
        disk = psutil.disk_usage("C:\\")
        info["disk"] = {
            "total_gb": round(disk.total / (1024**3), 1),
            "free_gb": round(disk.free / (1024**3), 1),
            "percent_used": disk.percent,
        }
    except Exception as e:
        info["error"] = str(e)
    return info


def _process_snapshot(pid: int | None) -> dict:
    """Capture info about the crashed process (if still queryable) and its children."""
    info = {}
    if not pid:
        return info
    try:
        import psutil
        try:
            proc = psutil.Process(pid)
            info["name"] = proc.name()
            info["cmdline"] = proc.cmdline()
            info["create_time"] = proc.create_time()
            info["num_threads"] = proc.num_threads()
            try:
                mem = proc.memory_info()
                info["rss_mb"] = round(mem.rss / (1024**2), 1)
                info["vms_mb"] = round(mem.vms / (1024**2), 1)
            except Exception:
                pass
            # children that might be orphaned
            children = proc.children(recursive=True)
            if children:
                info["children"] = [
                    {"pid": c.pid, "name": c.name(), "cmdline": c.cmdline()[:3]}
                    for c in children
                ]
        except psutil.NoSuchProcess:
            info["status"] = "already_dead"
    except ImportError:
        pass
    return info


def _sibling_processes(root: Path) -> list[dict]:
    """Snapshot all claudebot-related processes still running."""
    results = []
    try:
        import psutil
        my_pid = os.getpid()
        for p in psutil.process_iter(["pid", "name", "cmdline", "create_time", "memory_info"]):
            try:
                cmdline = " ".join(p.info["cmdline"] or [])
                if "claudebot" not in cmdline.lower():
                    continue
                if p.info["pid"] == my_pid:
                    continue
                if "bash" in cmdline.lower():
                    continue
                mem = p.info.get("memory_info")
                results.append({
                    "pid": p.info["pid"],
                    "cmdline": cmdline[:200],
                    "rss_mb": round(mem.rss / (1024**2), 1) if mem else None,
                    "age_s": round(time.time() - p.info["create_time"], 0),
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except ImportError:
        pass
    return results


def _heartbeat_state(root: Path) -> dict:
    """Read all heartbeat/eventloop files to see what was alive."""
    state = {}
    data_dir = root / "data"
    for pattern in ("heartbeat_*.json", "eventloop_*.json", "supervisor_heartbeat.json"):
        for hb in data_dir.glob(pattern):
            try:
                raw = json.loads(hb.read_text("utf-8"))
                age = time.time() - float(raw.get("timestamp", 0))
                state[hb.name] = {"age_s": round(age, 1), "data": raw}
            except Exception:
                state[hb.name] = {"error": "unreadable"}
    return state


def _lockfile_state(root: Path) -> dict:
    """Read all lockfiles."""
    state = {}
    data_dir = root / "data"
    for lock in data_dir.glob("*.lock"):
        try:
            pid_str = lock.read_text("utf-8").strip()
            pid = int(pid_str)
            alive = False
            try:
                import psutil
                alive = psutil.pid_exists(pid)
            except ImportError:
                pass
            state[lock.name] = {"pid": pid, "alive": alive}
        except Exception:
            state[lock.name] = {"error": "unreadable"}
    return state


def capture_crash_info(
    name: str,
    pid: int | None,
    exit_code: int,
    root: Path,
    uptime: float = 0.0,
    stderr: str = "",
):
    """Record comprehensive crash metadata and snapshot recent bot logs.

    This is our black box. When a bot dies mysteriously, this file is
    the first thing we read.
    """
    crash_time = datetime.now()
    crash_ts = crash_time.isoformat()
    logs_dir = root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # -- Structured crash record (append to crashes.jsonl) --
    entry = {
        "bot": name,
        "exit_code": exit_code,
        "timestamp": crash_ts,
        "pid": pid,
        "uptime_s": round(uptime, 1),
        "system": _system_snapshot(),
        "heartbeats": _heartbeat_state(root),
        "lockfiles": _lockfile_state(root),
        "siblings": _sibling_processes(root),
    }
    try:
        with open(logs_dir / "crashes.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass

    # -- Human-readable crash report --
    log_file_map = {
        "claudebot": logs_dir / "claudebot.log",
        "codexbot": logs_dir / "codexbot.log",
        "kimibot": logs_dir / "kimibot.log",
        "selfbot": logs_dir / "selfbot.log",
        "supervisor": logs_dir / "supervisor.log",
    }
    log_file = log_file_map.get(name, logs_dir / f"{name}.log")

    report_lines = [
        f"=== CRASH REPORT: {name} ===",
        f"Time:      {crash_ts}",
        f"PID:       {pid}",
        f"Exit code: {exit_code}",
        f"Uptime:    {uptime:.1f}s",
        "",
    ]

    # System state
    sys_snap = entry["system"]
    if "memory" in sys_snap:
        mem = sys_snap["memory"]
        report_lines.append(f"Memory:    {mem['available_gb']:.1f} / {mem['total_gb']:.1f} GB free ({mem['percent_used']}% used)")
    if "cpu_percent" in sys_snap:
        report_lines.append(f"CPU:       {sys_snap['cpu_percent']}%")
    if "disk" in sys_snap:
        d = sys_snap["disk"]
        report_lines.append(f"Disk:      {d['free_gb']:.1f} / {d['total_gb']:.1f} GB free ({d['percent_used']}% used)")
    report_lines.append("")

    # Heartbeat state
    report_lines.append("--- Heartbeats at crash time ---")
    for hb_name, hb_data in entry["heartbeats"].items():
        if "age_s" in hb_data:
            report_lines.append(f"  {hb_name}: age={hb_data['age_s']}s")
        else:
            report_lines.append(f"  {hb_name}: {hb_data}")
    report_lines.append("")

    # Lockfiles
    report_lines.append("--- Lockfiles ---")
    for lf_name, lf_data in entry["lockfiles"].items():
        if "pid" in lf_data:
            report_lines.append(f"  {lf_name}: pid={lf_data['pid']} alive={lf_data['alive']}")
        else:
            report_lines.append(f"  {lf_name}: {lf_data}")
    report_lines.append("")

    # Sibling processes
    report_lines.append("--- Running claudebot processes ---")
    for sib in entry["siblings"]:
        report_lines.append(f"  PID {sib['pid']:>7} rss={sib['rss_mb']}MB age={sib['age_s']:.0f}s {sib['cmdline'][:120]}")
    report_lines.append("")

    # Full stderr
    if stderr:
        report_lines.append("--- stderr (full) ---")
        report_lines.append(stderr)
        report_lines.append("")

    # Bot log tail (last 200 lines)
    if log_file.exists():
        try:
            lines = log_file.read_text("utf-8", errors="replace").splitlines()
            tail = lines[-200:]
            report_lines.append(f"--- {log_file.name} (last {len(tail)} lines) ---")
            report_lines.extend(tail)
        except Exception as e:
            report_lines.append(f"--- Failed to read {log_file.name}: {e} ---")

    # Write crash report
    try:
        snap_name = f"crash_{name}_{crash_ts.replace(':', '-')}.txt"
        (logs_dir / snap_name).write_text("\n".join(report_lines), "utf-8")
    except Exception:
        pass
