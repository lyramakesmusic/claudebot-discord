"""Unified BotState JSON-backed persistent state for sessions, projects, and guilds."""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class BotState:
    """JSON-backed state for sessions and projects. Survives restarts."""

    def __init__(self, path: Path, primary_guild_id: int = 0):
        self.path = path
        self.primary_guild_id = primary_guild_id
        self._data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text("utf-8"))
                if "guilds" not in data:
                    data["guilds"] = {}
                for p in data.get("projects", {}).values():
                    if "guild_id" not in p:
                        p["guild_id"] = self.primary_guild_id
                return data
            except Exception:
                log.warning("Corrupt state file, starting fresh")
        return {"sessions": {}, "projects": {}, "guilds": {}, "contexts": {}}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), "utf-8")
        tmp.replace(self.path)

    def get_session(self, key: str) -> Optional[dict]:
        return self._data["sessions"].get(key)

    def set_session(self, key: str, session_id: str, cwd: str, project: str = None):
        self._data["sessions"][key] = {
            "session_id": session_id,
            "cwd": cwd,
            "project": project,
            "updated": datetime.now().isoformat(),
        }
        self._save()

    def clear_session(self, key: str):
        self._data["sessions"].pop(key, None)
        self._save()

    def _contexts(self) -> dict:
        return self._data.setdefault("contexts", {})

    def save_context(self, ctx_key: str, name: str, session_id: str, cwd: str):
        bucket = self._contexts().setdefault(ctx_key, {})
        bucket[name] = {
            "session_id": session_id,
            "cwd": cwd,
            "saved": datetime.now().isoformat(),
        }
        self._save()

    def get_context(self, ctx_key: str, name: str) -> Optional[dict]:
        return self._contexts().get(ctx_key, {}).get(name)

    def list_contexts(self, ctx_key: str) -> dict:
        return dict(self._contexts().get(ctx_key, {}))

    def delete_context(self, ctx_key: str, name: str):
        bucket = self._contexts().get(ctx_key, {})
        bucket.pop(name, None)
        self._save()

    @staticmethod
    def scan_disk_sessions(cwd: str) -> list[dict]:
        normalized = cwd.replace("\\", "/").rstrip("/")
        workspace = normalized.replace(":", "-").replace("/", "-")
        projects_dir = Path.home() / ".claude" / "projects" / workspace
        if not projects_dir.is_dir():
            return []

        results = []
        for f in projects_dir.glob("*.jsonl"):
            if f.name.startswith("agent-"):
                continue
            session_id = f.stem
            size_kb = f.stat().st_size / 1024
            mtime = datetime.fromtimestamp(f.stat().st_mtime)

            summary = ""
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("type") == "summary":
                            summary = obj.get("summary", "")
                            break
                        if obj.get("type") == "user":
                            msg = obj.get("message", {})
                            content = msg.get("content", "")
                            text = content if isinstance(content, str) else ""
                            lines = text.split("\n")
                            for ln in reversed(lines):
                                ln = ln.strip()
                                if ln and not ln.startswith("[") and not ln.startswith("=") and not ln.startswith("-") and not ln.startswith("`"):
                                    summary = ln[:120]
                                    break
                            if not summary:
                                summary = text[:120]
                            break
            except Exception:
                pass

            results.append({
                "session_id": session_id,
                "timestamp": mtime.isoformat(),
                "size_kb": round(size_kb),
                "summary": summary,
            })

        results.sort(key=lambda x: x["timestamp"], reverse=True)
        return results

    def get_project(self, name: str, guild_id: int = None) -> Optional[dict]:
        p = self._data["projects"].get(name)
        if p and guild_id is not None and p.get("guild_id") != guild_id:
            return None
        return p

    def set_project(self, name: str, folder: str, thread_id: int, guild_id: int = 0, council: bool = False):
        self._data["projects"][name] = {
            "folder": folder,
            "thread_id": thread_id,
            "guild_id": guild_id,
            "created": datetime.now().isoformat(),
            "council": council,
        }
        self._save()

    def find_project_by_thread(self, thread_id: int) -> Optional[tuple]:
        for name, p in self._data["projects"].items():
            if p.get("thread_id") == thread_id:
                return name, p
        return None

    def all_projects(self, guild_id: int = None) -> dict:
        projects = self._data.get("projects", {})
        if guild_id is not None:
            return {n: p for n, p in projects.items() if p.get("guild_id") == guild_id}
        return dict(projects)

    def get_guild_config(self, guild_id: int) -> Optional[dict]:
        return self._data.get("guilds", {}).get(str(guild_id))

    def set_guild_config(self, guild_id: int, home_channel_id: int, slug: str, docs_dir: str):
        self._data.setdefault("guilds", {})[str(guild_id)] = {
            "home_channel_id": home_channel_id,
            "slug": slug,
            "docs_dir": docs_dir,
        }
        self._save()
