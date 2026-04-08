from claude.research import configure as configure_research
from claude.research import try_handle_research_command
from integrations.council_prompt import build_opus_council_prompt
from shared.bot_actions import extract_bot_actions
from shared.discord_utils import sanitize
from shared.discord_utils import split_message
from shared.plugin import Plugin
from shared.plugin import PluginContext


class ResearchPlugin(Plugin):
    name = "research"
    commands = ["/research"]

    async def setup(self, ctx: PluginContext) -> None:
        execute_actions = ctx.extra.get("dispatch_actions_cb")
        legacy_dispatch = ctx.extra.get("legacy_dispatch")
        if execute_actions is None and callable(legacy_dispatch):
            async def _dispatch_actions(actions, message, channel, guild_id=0, caller_ctx_key=None):
                results: list[str] = []
                pending_reload = False
                files = []
                council_feedback = []
                for act in actions:
                    payload = await legacy_dispatch(
                        act,
                        message,
                        channel,
                        guild_id,
                        caller_ctx_key=caller_ctx_key,
                    )
                    results.extend(payload.get("results", []) or [])
                    pending_reload = pending_reload or bool(payload.get("reload", False))
                    files.extend(payload.get("files", []) or [])
                    council_feedback.extend(payload.get("council_feedback", []) or [])
                return results, pending_reload, files, council_feedback
            execute_actions = _dispatch_actions
        if execute_actions is None:
            ctx.log.warning("research plugin loaded without action dispatcher; action blocks will be skipped")
            async def _noop_dispatch(actions, message, channel, guild_id=0, caller_ctx_key=None):
                return [], False, [], []
            execute_actions = _noop_dispatch
        configure_research(
            state_obj=ctx.state,
            bridge_obj=ctx.bridge,
            build_opus_council_prompt_fn=build_opus_council_prompt,
            extract_bot_actions_fn=extract_bot_actions,
            execute_bot_actions_fn=execute_actions,
            split_message_fn=split_message,
            sanitize_fn=sanitize,
            logger=ctx.log,
            typing_interval=int(ctx.extra.get("typing_interval", 8)),
        )

    async def handle_command(self, cmd: str, message, channel) -> bool:
        return await try_handle_research_command(message, cmd, channel)


plugin = ResearchPlugin()
