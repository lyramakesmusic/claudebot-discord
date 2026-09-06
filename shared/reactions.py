"""Emoji reactions as prompt context.

Gateway reaction events are recorded (message id -> emoji -> who) so the bots can
annotate recent messages with who reacted, without any extra API calls. Names are
known for reactions added since boot; older reactions fall back to counts from the
message object. Reactions never trigger a response — they only enrich context.
"""

from collections import OrderedDict

import discord

_MAX_TRACKED = 5000
_by_msg: "OrderedDict[int, dict[str, set[str]]]" = OrderedDict()


def _bucket(message_id: int) -> dict[str, set[str]]:
    b = _by_msg.get(message_id)
    if b is None:
        b = _by_msg[message_id] = {}
        while len(_by_msg) > _MAX_TRACKED:
            _by_msg.popitem(last=False)
    else:
        _by_msg.move_to_end(message_id)
    return b


def _name(client: discord.Client, payload: discord.RawReactionActionEvent) -> str:
    member = getattr(payload, "member", None)
    if member is not None:
        return member.display_name or member.name
    guild = client.get_guild(payload.guild_id) if payload.guild_id else None
    m = guild.get_member(payload.user_id) if guild else None
    if m is not None:
        return m.display_name or m.name
    u = client.get_user(payload.user_id)
    return (u.display_name or u.name) if u else str(payload.user_id)


def register(client: discord.Client) -> None:
    """Attach reaction listeners to a discord.Client (call once, before run)."""

    async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
        if client.user and payload.user_id == client.user.id:
            return
        _bucket(payload.message_id).setdefault(str(payload.emoji), set()).add(_name(client, payload))

    async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
        b = _by_msg.get(payload.message_id)
        if not b:
            return
        b.get(str(payload.emoji), set()).discard(_name(client, payload))

    async def on_raw_reaction_clear(payload: discord.RawReactionClearEvent):
        _by_msg.pop(payload.message_id, None)

    client.event(on_raw_reaction_add)
    client.event(on_raw_reaction_remove)
    client.event(on_raw_reaction_clear)


def format_reactions(message: discord.Message) -> str:
    """One-line summary like '👍 lyra, kaya · 💀 qvz · 🔥×3', or '' if none."""
    if not message.reactions:
        return ""
    known = _by_msg.get(message.id, {})
    parts = []
    for r in message.reactions:
        emoji = str(r.emoji)
        label = emoji if isinstance(r.emoji, str) else f":{r.emoji.name}:"
        names = sorted(known.get(emoji, ()))
        extra = r.count - len(names)
        if names:
            who = ", ".join(names) + (f" +{extra}" if extra > 0 else "")
            parts.append(f"{label} {who}")
        else:
            parts.append(f"{label}×{r.count}" if r.count > 1 else label)
    return " · ".join(parts)
