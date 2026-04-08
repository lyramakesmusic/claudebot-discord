import asyncio
import os
import re
import subprocess
from pathlib import Path

import discord

from shared.hotreload import validate_syntax
from shared.plugin import Plugin
from shared.plugin import PluginContext


class ProjectMgmtPlugin(Plugin):
    name = "project_mgmt"
    actions = ["create_project", "create_thread", "seed_project", "reload", "full_restart"]

    def __init__(self):
        self._ctx: PluginContext | None = None

    async def setup(self, ctx: PluginContext) -> None:
        self._ctx = ctx

    async def handle_action(self, action: dict, message, channel, guild_id, **kwargs) -> dict:
        action_name = str(action.get("action", "")).strip()
        if action_name in {"create_project", "create_thread"}:
            return await self._create_project(action, message, guild_id)
        if action_name == "seed_project":
            return await self._seed_existing_project(action, channel, guild_id)
        if action_name == "reload":
            return self._reload_current_bot()
        if action_name == "full_restart":
            return await self._full_restart(channel)
        return {"results": []}

    async def _create_project(self, action: dict, message, guild_id: int) -> dict:
        if self._ctx is None:
            return {"results": ["Project plugin not initialized"]}

        name = str(action.get("name", "")).strip()
        if not name:
            return {"results": ["(project creation skipped - no name given)"]}
        name = re.sub(r"[^\w\-]", "-", name).strip("-")
        if not name:
            return {"results": ["(project creation skipped - invalid name)"]}

        existing = self._ctx.state.get_project(name, guild_id)
        if existing:
            return {"results": [f"**{name}** already exists -> <#{existing['thread_id']}>"]}

        guild_config = self._ctx.state.get_guild_config(guild_id)
        guild_docs = Path(guild_config["docs_dir"]) if guild_config else self._ctx.documents_dir
        folder = guild_docs / name
        folder.mkdir(parents=True, exist_ok=True)

        try:
            thread = await message.create_thread(name=name, auto_archive_duration=10080)
            self._ctx.state.set_project(name, str(folder), thread.id, guild_id)
            results = [f"Created project **{name}** -> <#{thread.id}>\nFolder: `{folder}`"]

            seed_msg = str(action.get("message", "")).strip()
            seed_cb = self._ctx.extra.get("seed_project_cb")
            if seed_msg and callable(seed_cb):
                asyncio.create_task(seed_cb(thread, name, str(folder), seed_msg, guild_id))
            return {"results": results}
        except Exception as exc:
            return {"results": [f"Failed to create project thread: {exc}"]}

    async def _seed_existing_project(self, action: dict, channel, guild_id: int) -> dict:
        if self._ctx is None:
            return {"results": ["Project plugin not initialized"]}
        seed_cb = self._ctx.extra.get("seed_project_cb")
        if not callable(seed_cb):
            return {"results": ["seed_project is unavailable in this bot"]}
        if not isinstance(channel, discord.Thread):
            return {"results": ["seed_project requires running inside a project thread"]}

        seed_msg = str(action.get("message", "")).strip()
        if not seed_msg:
            return {"results": ["seed_project: no message given"]}

        tp = self._ctx.state.find_project_by_thread(channel.id)
        if not tp:
            return {"results": ["seed_project: current thread is not mapped to a project"]}
        label, proj = tp
        asyncio.create_task(seed_cb(channel, label, proj["folder"], seed_msg, guild_id))
        return {"results": [f"Seeding project **{label}**..."]}

    def _reload_current_bot(self) -> dict:
        if self._ctx is None:
            return {"results": ["Reload unavailable: plugin not initialized"]}
        bot_file = self._ctx.extra.get("bot_file")
        if not bot_file:
            return {"results": ["Reload unavailable: bot file path missing"]}
        ok, err = validate_syntax(Path(bot_file))
        if not ok:
            return {"results": [f"Reload aborted - bad syntax:\n```\n{(err or 'unknown')[:500]}\n```"]}
        return {"results": [], "reload": True}

    async def _full_restart(self, channel) -> dict:
        if self._ctx is None:
            return {"results": ["Full restart unavailable: plugin not initialized"]}
        restart_script = self._ctx.project_root / "scripts" / "restart.ps1"
        if not restart_script.exists():
            return {"results": ["restart.ps1 not found"]}

        try:
            await channel.send("Full restart in progress...")
            creationflags = int(self._ctx.extra.get("create_flags", 0))
            creationflags |= int(getattr(subprocess, "DETACHED_PROCESS", 0))
            subprocess.Popen(
                ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(restart_script)],
                creationflags=creationflags,
                close_fds=True,
            )
            await asyncio.sleep(1)
            os._exit(0)
        except Exception as exc:
            return {"results": [f"Full restart failed: {exc}"]}


plugin = ProjectMgmtPlugin()
