from integrations.midjourney import enqueue_midjourney
from integrations.midjourney import init_mj_worker
from integrations.midjourney import shutdown_mj_browser
from shared.plugin import Plugin
from shared.plugin import PluginContext


class MidjourneyPlugin(Plugin):
    name = "midjourney"
    actions = ["generate_midjourney"]

    def __init__(self):
        self._ctx: PluginContext | None = None

    async def setup(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        init_mj_worker()

    async def teardown(self) -> None:
        shutdown_mj_browser()

    async def handle_action(self, action: dict, message, channel, guild_id, **kwargs) -> dict:
        prompt = str(action.get("prompt", "")).strip()
        if not prompt:
            return {"results": ["(midjourney generation skipped - no prompt given)"]}
        caption = str(action.get("caption", "")).strip()
        ctx_key = kwargs.get("caller_ctx_key")
        bridge = self._ctx.bridge if self._ctx else None
        enqueue_midjourney(channel, prompt, caption, ctx_key=ctx_key, bridge=bridge, message=message)
        return {"results": []}


plugin = MidjourneyPlugin()
