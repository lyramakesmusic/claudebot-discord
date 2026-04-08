import asyncio

from claude.system_stats import configure as configure_system_stats
from claude.system_stats import system_stats
from shared.plugin import Plugin
from shared.plugin import PluginContext


class SystemStatsPlugin(Plugin):
    name = "system_stats"
    actions = ["system_stats"]

    def __init__(self):
        self._ctx: PluginContext | None = None

    async def setup(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        configure_system_stats(int(ctx.extra.get("create_flags", 0)))

    async def handle_action(self, action: dict, message, channel, guild_id, **kwargs) -> dict:
        loop = asyncio.get_running_loop()
        stats = await loop.run_in_executor(None, system_stats)
        return {"results": [stats]}


plugin = SystemStatsPlugin()
