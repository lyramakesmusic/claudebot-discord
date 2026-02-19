"""System resource snapshot helpers."""

import subprocess

import psutil

_create_flags = 0


def configure(create_flags: int):
    global _create_flags
    _create_flags = create_flags


def system_stats() -> str:
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    lines = [
        f"**CPU:** {cpu}%",
        f"**RAM:** {mem.used / 1073741824:.1f} / {mem.total / 1073741824:.1f} GB ({mem.percent}%)",
    ]
    try:
        r = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=_create_flags,
        )
        if r.returncode == 0:
            for i, gpu_line in enumerate(r.stdout.strip().splitlines()):
                parts = [p.strip() for p in gpu_line.split(",")]
                if len(parts) >= 4:
                    lines.append(
                        f"**GPU {i}:** {parts[0]} - {parts[1]}/{parts[2]} MB VRAM ({parts[3]}% util)"
                    )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        lines.append("**GPU:** N/A")
    return "\n".join(lines)
