from claude.memories import MEMORY_ACTION_RE
from claude.memories import configure as configure_memories
from claude.memories import process_memory_actions
from shared.plugin import Plugin
from shared.plugin import PluginContext


class MemoriesPlugin(Plugin):
    name = "memories"

    async def setup(self, ctx: PluginContext) -> None:
        primary_guild_id = int(ctx.extra.get("primary_guild_id", 0) or 0)
        configure_memories(ctx.project_root / "selfbot", primary_guild_id, ctx.log)

    def strip_text(self, text: str, **kwargs) -> str:
        return MEMORY_ACTION_RE.sub("", text)

    async def process_text(self, text: str, **kwargs) -> str:
        if not text:
            return text
        channel_name = str(kwargs.get("channel_name") or "?")
        server_name = str(kwargs.get("server_name") or "?")
        guild_id = kwargs.get("guild_id")
        return process_memory_actions(text, channel_name, server_name, guild_id)


plugin = MemoriesPlugin()
