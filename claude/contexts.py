"""Session context-switch command handling for Claude bot."""

import re
from datetime import datetime

import discord

_state = None
_bridge = None
_default_cwd = ""


def configure(*, state_obj, bridge_obj, default_cwd: str):
    global _state, _bridge, _default_cwd
    _state = state_obj
    _bridge = bridge_obj
    _default_cwd = default_cwd


async def try_handle_context_command(
    message: discord.Message, content: str, channel_id: int
) -> bool:
    """Handle .new-context/.list-contexts/.resume-context. Returns True if handled."""
    lower = content.lower().strip()
    if not (
        lower.startswith(".new-context")
        or lower.startswith(".list-contexts")
        or lower.startswith(".resume-context")
    ):
        return False

    ctx_key = str(channel_id)
    cur = _state.get_session(ctx_key)
    cwd = cur["cwd"] if cur and cur.get("cwd") else _default_cwd

    if lower.startswith(".new-context"):
        name = content[len(".new-context") :].strip()
        if not name:
            name = f"ctx-{datetime.now().strftime('%H%M%S')}"
        name = re.sub(r"[^\w\-.]", "_", name)

        if cur and cur.get("session_id"):
            _state.save_context(ctx_key, name, cur["session_id"], cur["cwd"])
            await _bridge.kill_process(ctx_key)
            _state.clear_session(ctx_key)
            await message.reply(
                f"Saved current context as **{name}**. Starting fresh session.\n"
                f"Use `.resume-context {name}` to return.",
                mention_author=False,
            )
        else:
            _state.clear_session(ctx_key)
            await _bridge.kill_process(ctx_key)
            await message.reply(
                "No active session to save. Starting fresh.",
                mention_author=False,
            )
        return True

    if lower.startswith(".list-contexts"):
        disk_sessions = _state.scan_disk_sessions(cwd)
        named_contexts = _state.list_contexts(ctx_key)
        active_id = cur["session_id"] if cur and cur.get("session_id") else None

        if not disk_sessions and not named_contexts and not active_id:
            await message.reply("No sessions found for this channel.", mention_author=False)
            return True

        named_ids = {info["session_id"]: name for name, info in named_contexts.items()}
        lines = []
        for ds in disk_sessions[:15]:
            sid = ds["session_id"]
            short = sid[:8]
            age = ds["timestamp"][:16]
            size = ds["size_kb"]
            summary = ds["summary"][:80] if ds["summary"] else ""

            if sid == active_id:
                label = "**active**"
            elif sid in named_ids:
                label = f"**{named_ids[sid]}**"
            else:
                label = f"`{short}`"

            line = f"{label} — {age} · {size} KB"
            if summary:
                line += f"\n> {summary}"
            lines.append(line)

        await message.reply("\n".join(lines), mention_author=False)
        return True

    arg = content[len(".resume-context") :].strip()
    if not arg:
        await message.reply("Usage: `.resume-context <name or session_id>`", mention_author=False)
        return True

    target = _state.get_context(ctx_key, arg)
    if target:
        target_id = target["session_id"]
        target_cwd = target["cwd"]
        _state.delete_context(ctx_key, arg)
    else:
        disk_sessions = _state.scan_disk_sessions(cwd)
        match = None
        for ds in disk_sessions:
            if ds["session_id"] == arg or ds["session_id"].startswith(arg):
                match = ds
                break
        if not match:
            named = _state.list_contexts(ctx_key)
            avail = ", ".join(f"**{name}**" for name in named) if named else "none"
            await message.reply(
                f"No context named **{arg}** and no session ID starting with `{arg[:12]}`.\n"
                f"Named contexts: {avail}\n"
                f"Use `.list-contexts` to see all sessions with their IDs.",
                mention_author=False,
            )
            return True
        target_id = match["session_id"]
        target_cwd = cwd

    if cur and cur.get("session_id") and cur["session_id"] != target_id:
        auto_name = f"auto-{cur['session_id'][:8]}"
        _state.save_context(ctx_key, auto_name, cur["session_id"], cur["cwd"])

    await _bridge.kill_process(ctx_key)
    _state.set_session(ctx_key, target_id, target_cwd)
    await message.reply(f"Resumed session `{target_id[:8]}...`", mention_author=False)
    return True

