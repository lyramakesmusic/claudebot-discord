"""Council research-thread bootstrap handler for Claude bot."""

import asyncio
import logging
import re
from pathlib import Path

import discord

_state = None
_bridge = None
_build_opus_council_prompt = lambda topic=None: ""
_extract_bot_actions = lambda text: (text, [])
_execute_bot_actions = None
_split_message = lambda text: [text]
_sanitize = lambda text: text
_log = logging.getLogger(__name__)
_typing_interval = 8


def configure(
    *,
    state_obj,
    bridge_obj,
    build_opus_council_prompt_fn,
    extract_bot_actions_fn,
    execute_bot_actions_fn,
    split_message_fn,
    sanitize_fn,
    logger,
    typing_interval: int,
):
    global _state, _bridge, _build_opus_council_prompt, _extract_bot_actions, _execute_bot_actions
    global _split_message, _sanitize, _log, _typing_interval
    _state = state_obj
    _bridge = bridge_obj
    _build_opus_council_prompt = build_opus_council_prompt_fn
    _extract_bot_actions = extract_bot_actions_fn
    _execute_bot_actions = execute_bot_actions_fn
    _split_message = split_message_fn
    _sanitize = sanitize_fn
    _log = logger
    _typing_interval = typing_interval


async def try_handle_research_command(
    message: discord.Message, raw_text: str, channel: discord.abc.Messageable
) -> bool:
    """Handle /research command in non-thread channels. Returns True if handled."""
    research_match = re.match(r"^/research\s+(.+)", raw_text, re.IGNORECASE | re.DOTALL)
    if not research_match or isinstance(channel, discord.Thread):
        return False

    topic = research_match.group(1).strip()
    if not topic:
        await message.reply("Usage: `/research <topic>`", mention_author=False)
        return True

    thread_name = re.sub(r"[^\w\- ]", "", topic)[:80].strip() or "research"
    guild_id = message.guild.id if message.guild else 0
    guild_config = _state.get_guild_config(guild_id)
    docs_dir = Path(guild_config["docs_dir"]) if guild_config and guild_config.get("docs_dir") else (Path.home() / "Documents")
    folder = docs_dir / re.sub(r"[^\w\-]", "-", thread_name).strip("-")
    folder.mkdir(parents=True, exist_ok=True)

    try:
        thread = await message.create_thread(name=thread_name, auto_archive_duration=10080)
        _state.set_project(
            re.sub(r"[^\w\-]", "-", thread_name).strip("-"),
            str(folder),
            thread.id,
            guild_id,
            council=True,
        )

        sys_prompt = _build_opus_council_prompt(topic)
        ctx_key = f"proj:{re.sub(r'[^\w-]', '-', thread_name).strip('-')}"
        pp = await _bridge.get_or_create(ctx_key, str(folder), None, sys_prompt)

        async def _keep_typing():
            try:
                while True:
                    await thread.typing()
                    await asyncio.sleep(_typing_interval)
            except asyncio.CancelledError:
                pass

        typing_task = asyncio.create_task(_keep_typing())
        try:
            await thread.typing()
            seed = f"lyra: {topic}"
            result = await pp.send(seed)
        finally:
            typing_task.cancel()

        if pp.session_id:
            _state.set_session(ctx_key, pp.session_id, str(folder), thread_name)

        if result["error"]:
            await thread.send(f"Error: {result.get('error_message', '?')[:1800]}")
            return True

        response = result.get("text", "").strip()
        if response:
            response, actions = _extract_bot_actions(response)
            for chunk in _split_message(_sanitize(response)):
                try:
                    await thread.send(chunk)
                except Exception:
                    pass
            if actions:
                await _execute_bot_actions(actions, message, thread, guild_id, caller_ctx_key=ctx_key)
    except Exception as exc:
        _log.exception("Failed to start council thread")
        await message.reply(f"Failed to start research thread: {exc}", mention_author=False)

    return True
