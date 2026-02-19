"""Memory notebook management for Claude bot."""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

MEMORY_ACTION_RE = re.compile(r"```memory\s*\n(.*?)\n```", re.DOTALL)

_log = logging.getLogger(__name__)
_memories_dir = Path("selfbot")
_primary_guild_id = 0


def configure(memories_dir: Path, primary_guild_id: int, logger=None):
    global _memories_dir, _primary_guild_id, _log
    _memories_dir = memories_dir
    _primary_guild_id = primary_guild_id
    if logger is not None:
        _log = logger


def memories_file(guild_id: int = None) -> Path:
    if guild_id is None or guild_id == _primary_guild_id:
        return _memories_dir / "memories.json"
    return _memories_dir / f"memories_{guild_id}.json"


def load_memories(guild_id: int = None) -> list[dict]:
    path = memories_file(guild_id)
    if path.exists():
        try:
            return json.loads(path.read_text("utf-8"))
        except Exception:
            _log.warning(f"Corrupt memories file: {path}")
    return []


def save_memories(memories: list[dict], guild_id: int = None):
    path = memories_file(guild_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(memories, indent=2), "utf-8")
    tmp.replace(path)


def _next_memory_id(memories: list[dict]) -> int:
    if not memories:
        return 1
    return max(m.get("id", 0) for m in memories) + 1


def process_memory_actions(text: str, channel_name: str, server_name: str, guild_id: int = None) -> str:
    matches = list(MEMORY_ACTION_RE.finditer(text))
    if not matches:
        return text

    memories = load_memories(guild_id)

    for m in matches:
        try:
            action = json.loads(m.group(1))
        except json.JSONDecodeError:
            _log.warning(f"Bad memory JSON: {m.group(1)[:100]}")
            continue

        act = action.get("action")

        if act == "save":
            entry = {
                "id": _next_memory_id(memories),
                "text": action.get("text", ""),
                "tags": action.get("tags", []),
                "created": datetime.now().isoformat(),
                "source": {"channel": channel_name, "server": server_name},
            }
            memories.append(entry)
            _log.info(f"Memory saved: #{entry['id']} - {entry['text'][:60]}")

        elif act == "delete":
            mid = action.get("id")
            before = len(memories)
            memories = [m for m in memories if m.get("id") != mid]
            if len(memories) < before:
                _log.info(f"Memory deleted: #{mid}")

        elif act == "update":
            mid = action.get("id")
            for entry in memories:
                if entry.get("id") == mid:
                    if "text" in action:
                        entry["text"] = action["text"]
                    if "tags" in action:
                        entry["tags"] = action["tags"]
                    entry["updated"] = datetime.now().isoformat()
                    _log.info(f"Memory updated: #{mid}")
                    break

    save_memories(memories, guild_id)
    return MEMORY_ACTION_RE.sub("", text).strip()


def format_memories_for_prompt(guild_id: int = None) -> str:
    memories = load_memories(guild_id)
    if not memories:
        return "(no memories saved yet)"
    lines = []
    for m in memories:
        tags = ", ".join(m.get("tags", [])) if m.get("tags") else "untagged"
        source = m.get("source", {})
        where = source.get("server", "?")
        if source.get("channel"):
            where += f"/{source['channel']}"
        lines.append(f"  #{m['id']} [{tags}] {m['text']}  (from {where}, {m.get('created', '?')[:10]})")
    return "\n".join(lines)
