import re

import discord

from shared.plugin import Plugin
from shared.plugin import PluginContext


class VoicePlugin(Plugin):
    name = "voice"
    actions = ["join_voice", "leave_voice", "play_audio", "play_url", "stop_audio", "switch_voice"]
    events = ["on_voice_state_update"]

    def __init__(self):
        self._ctx: PluginContext | None = None
        self._manager = None
        self._voice_cls = None

    async def setup(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._voice_cls = ctx.extra.get("voice_manager_cls")
        manager_getter = ctx.extra.get("get_voice_manager")
        self._manager = manager_getter() if callable(manager_getter) else None
        # Voice pipeline is lazy-loaded on first join_voice to avoid blocking
        # the event loop at startup (transformers/ONNX imports take 10+ seconds)
        ctx.log.info("[Voice] Plugin registered (lazy init — will start on first join)")

    async def _ensure_manager(self) -> bool:
        """Lazy-init the voice manager on first use. Returns True if ready."""
        if self._manager is not None and self._manager._running:
            return True
        if self._voice_cls is None:
            return False
        try:
            self._ctx.log.info("[Voice] Initializing voice pipeline (first use)...")
            manager = self._voice_cls(self._ctx.client, self._ctx.bridge)
            await manager.start()
            self._manager = manager
            setter = self._ctx.extra.get("set_voice_manager")
            if callable(setter):
                setter(manager)
            self._ctx.log.info(f"[Voice] Pipeline ready (running={manager._running})")
            return True
        except Exception:
            self._ctx.log.exception("[Voice] Failed to initialize voice pipeline")
            return False

    def _get_manager(self):
        if self._ctx is None:
            return None
        getter = self._ctx.extra.get("get_voice_manager")
        if callable(getter):
            self._manager = getter()
        return self._manager

    async def on_event(self, event: str, *args, **kwargs) -> None:
        if event != "on_voice_state_update":
            return
        manager = self._get_manager()
        if manager:
            member, before, after = args
            await manager.on_voice_state_update(member, before, after)

    async def handle_action(self, action: dict, message, channel, guild_id, **kwargs) -> dict:
        action_name = str(action.get("action", "")).strip()
        manager = self._get_manager()

        if action_name == "join_voice":
            channel_ref = str(action.get("channel", "")).strip()
            if not channel_ref:
                return {"results": ["(join_voice skipped - no channel given)"]}
            if self._voice_cls is None:
                return {"results": ["Voice not available: voice dependencies are not installed"]}
            if not await self._ensure_manager():
                return {"results": ["Voice not available: pipeline failed to start (check logs)"]}
            manager = self._get_manager()
            vc = await self._resolve_voice_channel(channel_ref, message.guild)
            if not vc:
                available = [ch.name for ch in message.guild.voice_channels] if message.guild else []
                return {
                    "results": [
                        f"Could not find voice channel: {channel_ref}. "
                        f"Available: {', '.join(available) or 'none'}"
                    ]
                }
            await manager.join_channel(vc, caller_ctx_key=kwargs.get("caller_ctx_key"))
            return {"results": [f"Joined voice channel **{vc.name}**"]}

        if action_name == "leave_voice":
            if not manager:
                return {"results": ["Voice not available"]}
            if manager.voice_client and manager.voice_client.is_connected():
                ch_name = manager.voice_client.channel.name if manager.voice_client.channel else "?"
                await manager._leave_channel()
                return {"results": [f"Left voice channel **{ch_name}**"]}
            return {"results": ["Not currently in a voice channel"]}

        if action_name == "play_audio":
            path = str(action.get("path", "")).strip()
            try:
                volume = float(action.get("volume", 1.0))
            except (TypeError, ValueError):
                return {"results": ["play_audio: invalid volume"]}
            if not path:
                return {"results": ["play_audio: no path given"]}
            if not manager or not manager.voice_client or not manager.voice_client.is_connected():
                return {"results": ["play_audio: not in a voice channel"]}
            result_msg = await manager.play_file(path, volume=volume)
            return {"results": [result_msg]}

        if action_name == "play_url":
            url = str(action.get("url", "")).strip()
            try:
                volume = float(action.get("volume", 0.5))
            except (TypeError, ValueError):
                return {"results": ["play_url: invalid volume"]}
            if not url:
                return {"results": ["play_url: no URL given"]}
            if not manager or not manager.voice_client or not manager.voice_client.is_connected():
                return {"results": ["play_url: not in a voice channel"]}
            result_msg = await manager.play_url(url, volume=volume)
            return {"results": [result_msg]}

        if action_name == "stop_audio":
            if manager:
                await manager.stop_playback()
                return {"results": ["Playback stopped"]}
            return {"results": ["Voice not available"]}

        if action_name == "switch_voice":
            voice_name = str(action.get("voice", "")).strip()
            if not voice_name:
                return {"results": ["switch_voice: no voice name given"]}
            if not manager:
                return {"results": ["Voice not available"]}
            result_msg = await manager.switch_voice(voice_name)
            return {"results": [result_msg]}

        return {"results": []}

    async def _resolve_voice_channel(self, ref: str, guild: discord.Guild) -> discord.VoiceChannel | None:
        if not guild:
            return None
        try:
            ch_id = int(ref)
            ch = guild.get_channel(ch_id)
            if isinstance(ch, discord.VoiceChannel):
                return ch
            if self._ctx:
                for g in self._ctx.client.guilds:
                    ch = g.get_channel(ch_id)
                    if isinstance(ch, discord.VoiceChannel):
                        return ch
        except ValueError:
            pass

        url_match = re.search(r"/channels/\d+/(\d+)", ref)
        if url_match and self._ctx:
            try:
                ch_id = int(url_match.group(1))
                for g in self._ctx.client.guilds:
                    ch = g.get_channel(ch_id)
                    if isinstance(ch, discord.VoiceChannel):
                        return ch
            except ValueError:
                pass

        ref_lower = ref.lower()
        for ch in guild.voice_channels:
            if ch.name.lower() == ref_lower:
                return ch
        for ch in guild.voice_channels:
            if ref_lower in ch.name.lower():
                return ch
        return None


plugin = VoicePlugin()
