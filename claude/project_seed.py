"""Project thread seeding for Claude bot actions."""

import asyncio
import logging

import discord

_state = None
_bridge = None
_build_thread_context = lambda: ""
_process_memory_actions = lambda text, channel_name, server_name, guild_id=None: text
_process_reminder_actions = lambda text, channel_id, channel_name, requester_id=0: text
_extract_bot_actions = lambda text: (text, [])
_split_message = lambda text: [text]
_sanitize = lambda text: text
_log = logging.getLogger(__name__)
_typing_interval = 8


def configure(
    *,
    state_obj,
    bridge_obj,
    build_thread_context_fn,
    process_memory_actions_fn,
    process_reminder_actions_fn,
    extract_bot_actions_fn,
    split_message_fn,
    sanitize_fn,
    logger,
    typing_interval: int,
):
    global _state, _bridge, _build_thread_context, _process_memory_actions, _process_reminder_actions
    global _extract_bot_actions, _split_message, _sanitize, _log, _typing_interval
    _state = state_obj
    _bridge = bridge_obj
    _build_thread_context = build_thread_context_fn
    _process_memory_actions = process_memory_actions_fn
    _process_reminder_actions = process_reminder_actions_fn
    _extract_bot_actions = extract_bot_actions_fn
    _split_message = split_message_fn
    _sanitize = sanitize_fn
    _log = logger
    _typing_interval = typing_interval


async def seed_project(thread: discord.Thread, project_name: str, cwd: str, seed_msg: str, guild_id: int = 0):
    """Send a seed prompt to a new project thread and post Claude's first reply."""
    ctx_key = f"proj:{project_name}"
    sys_prompt = _build_thread_context()

    try:
        await thread.send(f"**Project initialized with:**\n{seed_msg}")
    except Exception:
        pass

    async def _keep_typing():
        try:
            while True:
                await thread.typing()
                await asyncio.sleep(_typing_interval)
        except asyncio.CancelledError:
            pass

    typing_task = asyncio.create_task(_keep_typing())
    await thread.typing()

    try:
        pp = await _bridge.get_or_create(ctx_key, cwd, None, sys_prompt)
        result = await pp.send(seed_msg)
    except Exception as exc:
        typing_task.cancel()
        _log.exception(f"Seed project error for {project_name}")
        try:
            await thread.send(f"Error starting project: {exc}")
        except Exception:
            pass
        return
    finally:
        typing_task.cancel()

    if pp.session_id:
        _state.set_session(ctx_key, pp.session_id, cwd, project_name)

    if result["error"]:
        err = result.get("error_message") or "Unknown error"
        try:
            await thread.send(f"Error:\n```\n{err[:1800]}\n```")
        except Exception:
            pass
        return

    response = result.get("text", "").strip()
    if not response:
        return

    srv_name = getattr(thread.guild, "name", "") if thread.guild else ""
    response = _process_memory_actions(response, thread.name, srv_name, guild_id)
    response = _process_reminder_actions(response, thread.id, thread.name)
    response, _ = _extract_bot_actions(response)

    if response:
        for chunk in _split_message(_sanitize(response)):
            try:
                await thread.send(chunk)
            except Exception:
                pass

    cost = result.get("cost_usd", 0)
    _log.info(f"Seeded project {project_name}: cost=${cost:.4f}")

