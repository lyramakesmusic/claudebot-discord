"""Small adapter plugin that delegates to bot-provided legacy callbacks."""

from shared.plugin import Plugin
from shared.plugin import PluginContext


class LegacyPlugin(Plugin):
    def __init__(
        self,
        *,
        name: str,
        actions: list[str] | None = None,
        events: list[str] | None = None,
        commands: list[str] | None = None,
        prompt_callback_key: str | None = None,
        action_callback_key: str = "legacy_dispatch",
        event_callback_key: str = "legacy_event",
        command_callback_key: str = "legacy_command",
    ):
        self.name = name
        self.actions = actions or []
        self.events = events or []
        self.commands = commands or []
        self._prompt_callback_key = prompt_callback_key
        self._action_callback_key = action_callback_key
        self._event_callback_key = event_callback_key
        self._command_callback_key = command_callback_key
        self._ctx: PluginContext | None = None

    async def setup(self, ctx: PluginContext) -> None:
        self._ctx = ctx

    async def handle_action(self, action: dict, message, channel, guild_id, **kwargs) -> dict:
        if not self._ctx:
            return {}
        callback = self._ctx.extra.get(self._action_callback_key)
        if not callback:
            return {}
        return await callback(action, message, channel, guild_id, plugin=self, **kwargs)

    async def on_event(self, event: str, *args, **kwargs) -> None:
        if not self._ctx:
            return
        callback = self._ctx.extra.get(self._event_callback_key)
        if callback:
            await callback(event, *args, plugin=self, **kwargs)

    async def handle_command(self, cmd: str, message, channel) -> bool:
        if not self._ctx:
            return False
        callback = self._ctx.extra.get(self._command_callback_key)
        if not callback:
            return False
        return bool(await callback(cmd, message, channel, plugin=self))

    def build_prompt_section(self) -> str | None:
        if not self._ctx or not self._prompt_callback_key:
            return None
        callback = self._ctx.extra.get(self._prompt_callback_key)
        if not callback:
            return None
        try:
            return callback(plugin=self)
        except Exception:
            self._ctx.log.exception(f"Plugin prompt callback failed: {self.name}")
            return None

