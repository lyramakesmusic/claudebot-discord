from pathlib import Path

import discord

from shared.plugin import Plugin
from shared.plugin import PluginContext


class UploadPlugin(Plugin):
    name = "upload"
    actions = ["upload"]

    def __init__(self):
        self._ctx: PluginContext | None = None

    async def setup(self, ctx: PluginContext) -> None:
        self._ctx = ctx

    async def handle_action(self, action: dict, message, channel, guild_id, **kwargs) -> dict:
        file_path = str(action.get("path", "")).strip()
        caption = str(action.get("caption", "")).strip()
        if not file_path:
            return {"results": ["(upload skipped - no path given)"]}

        p = Path(file_path).expanduser()
        if not p.exists():
            return {"results": [f"Upload failed - file not found: `{file_path}`"]}
        if not p.is_file():
            return {"results": [f"Upload failed - not a file: `{file_path}`"]}

        size_mb = p.stat().st_size / (1024 * 1024)
        if size_mb > 500:
            return {"results": [f"Upload failed - file too large ({size_mb:.1f} MB, max 500 MB)"]}

        try:
            f = discord.File(str(p), filename=p.name)
            await channel.send(caption or None, file=f)
            if self._ctx:
                self._ctx.log.info(f"Uploaded {p.name} ({size_mb:.1f} MB)")
            return {"results": []}
        except Exception as exc:
            return {"results": [f"Upload failed: {exc}"]}


plugin = UploadPlugin()
