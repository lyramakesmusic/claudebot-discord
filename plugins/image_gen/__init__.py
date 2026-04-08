import asyncio

from claude.image_gen import bg_generate_image
from claude.image_gen import configure as configure_image_gen
from shared.plugin import Plugin
from shared.plugin import PluginContext


class ImageGenPlugin(Plugin):
    name = "image_gen"
    actions = ["generate_image"]

    def __init__(self):
        self._ctx: PluginContext | None = None
        self._enabled = True

    async def setup(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        api_key = (ctx.env.get("OPENROUTER_API_KEY") or "").strip()
        image_model = (ctx.env.get("IMAGE_MODEL") or "google/gemini-3-pro-image-preview").strip()
        generated_images_dir = ctx.project_root / "data" / "generated_images"
        configure_image_gen(
            openrouter_api_key=api_key,
            image_model=image_model,
            generated_images_dir=generated_images_dir,
            owner_id=ctx.owner_id,
            logger=ctx.log,
        )
        if not api_key:
            self._enabled = False
            ctx.log.warning("image_gen plugin loaded without OPENROUTER_API_KEY")

    async def handle_action(self, action: dict, message, channel, guild_id, **kwargs) -> dict:
        if not self._enabled:
            return {"results": ["Image generation unavailable: OPENROUTER_API_KEY is not set"]}
        prompt = str(action.get("prompt", "")).strip()
        if not prompt:
            return {"results": ["(image generation skipped - no prompt given)"]}
        ref_images = action.get("reference_images", [])
        if not isinstance(ref_images, list):
            ref_images = []
        caption = str(action.get("caption", "")).strip()
        requester_id = getattr(getattr(message, "author", None), "id", 0)
        ctx_key = kwargs.get("caller_ctx_key")
        bridge = self._ctx.bridge if self._ctx else None
        asyncio.create_task(bg_generate_image(
            channel, prompt, ref_images or None, caption, requester_id,
            trigger_msg=message, ctx_key=ctx_key, bridge=bridge,
        ))
        return {"results": []}


plugin = ImageGenPlugin()
