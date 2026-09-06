"""Reasoning scratchpad for the Claude bot.

A place to do working — reason through a problem, hold turn-state, jot the number
to recheck. Unlike memories (surfaced back into the system prompt every session),
scratch is never retrieved and never returned to Discord: there's no load/format
function, so blocks are append-only and one-way. Not letters to the void, not a
journal — just scratch paper. You reason in it.
"""

import logging
import re
from datetime import datetime
from pathlib import Path

SCRATCH_ACTION_RE = re.compile(r"```scratch\s*\n(.*?)\n```", re.DOTALL)

_log = logging.getLogger(__name__)
_scratch_dir = Path("selfbot")


def configure(scratch_dir: Path = None, logger=None):
    global _scratch_dir, _log
    if scratch_dir is not None:
        _scratch_dir = scratch_dir
    if logger is not None:
        _log = logger


def scratch_file(guild_id: int = None) -> Path:
    if guild_id is None:
        return _scratch_dir / "scratch.txt"
    return _scratch_dir / f"scratch_{guild_id}.txt"


def process_scratch_actions(text: str, channel_name: str, server_name: str,
                            guild_id: int = None) -> str:
    """Append any ```scratch blocks to the write-only scratch file, then strip
    them from the text so they never reach Discord. Nothing is ever read back."""
    matches = list(SCRATCH_ACTION_RE.finditer(text))
    if not matches:
        return text
    path = scratch_file(guild_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().isoformat(timespec="seconds")
        with open(path, "a", encoding="utf-8") as f:
            for m in matches:
                content = m.group(1).strip("\n")
                f.write(f"\n--- {ts}  [{server_name}/{channel_name}] ---\n{content}\n")
        _log.info(f"Scratch appended: {len(matches)} block(s) -> {path.name}")
    except Exception as e:
        _log.warning(f"Scratch write failed: {e}")
    return SCRATCH_ACTION_RE.sub("", text).strip()
