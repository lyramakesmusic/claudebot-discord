from integrations.suno import enqueue_music
from integrations.suno import init_suno_worker
from shared.plugin import Plugin
from shared.plugin import PluginContext


class SunoPlugin(Plugin):
    name = "suno"
    actions = ["generate_music"]

    async def setup(self, ctx: PluginContext) -> None:
        init_suno_worker()

    async def handle_action(self, action: dict, message, channel, guild_id, **kwargs) -> dict:
        style = str(action.get("style", "")).strip()
        if not style:
            return {"results": ["(music generation skipped - no style given)"]}
        lyrics = str(action.get("lyrics", "")).strip()
        title = str(action.get("title", "")).strip()
        model = str(action.get("model", "")).strip()
        enqueue_music(channel, style, lyrics, title, model)
        return {"results": []}


plugin = SunoPlugin()
