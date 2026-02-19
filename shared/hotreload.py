"""Hot reload detection and syntax validation."""

import subprocess
import sys
from pathlib import Path

from shared.config import CREATE_FLAGS


def check_self_modified(bot_file: Path, boot_mtime: float) -> bool:
    """Check if a bot file has been modified since startup."""
    try:
        return bot_file.stat().st_mtime != boot_mtime
    except Exception:
        return False


def validate_syntax(bot_file: Path) -> tuple[bool, str | None]:
    """Validate Python syntax of a file. Returns (ok, error_message)."""
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                f"import py_compile; py_compile.compile({str(bot_file)!r}, doraise=True)",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=CREATE_FLAGS,
        )
        if result.returncode != 0:
            return False, result.stderr.strip() or result.stdout.strip()
        return True, None
    except subprocess.TimeoutExpired:
        return False, "Syntax check timed out"
    except Exception as exc:
        return False, str(exc)
