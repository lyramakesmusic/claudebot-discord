"""Discord utility functions shared by both bots."""

import re
from pathlib import Path

import discord

from shared.config import DOCUMENTS_DIR, MAX_DISCORD_LEN


def split_message(text: str, limit: int = MAX_DISCORD_LEN) -> list[str]:
    """Split text into Discord-safe chunks, breaking at newlines/spaces."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        idx = text.rfind("\n", 0, limit)
        if idx < limit // 3:
            idx = text.rfind(" ", 0, limit)
        if idx < limit // 3:
            idx = limit
        chunks.append(text[:idx])
        text = text[idx:].lstrip("\n")
    return chunks


def sanitize(text: str) -> str:
    """Prevent accidental @everyone/@here pings."""
    return text.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")


def is_guild_channel(channel: discord.abc.Messageable) -> bool:
    """True if the channel is in a guild (not a DM)."""
    return getattr(channel, "guild", None) is not None


def guild_slug(guild: discord.Guild) -> str:
    """Filesystem-safe slug from guild name."""
    return re.sub(r"[^\w\-]", "-", guild.name).strip("-").lower()[:50]


def guild_docs_dir(
    guild_id: int,
    guild: discord.Guild = None,
    primary_guild_id: int = 0,
) -> Path:
    """Primary guild -> ~/Documents. Others -> ~/Documents/{slug}/."""
    if guild_id == primary_guild_id:
        return DOCUMENTS_DIR
    slug = guild_slug(guild) if guild else str(guild_id)
    return DOCUMENTS_DIR / slug
