#!/usr/bin/env python3
"""Tiny Discord bot backed by OpenRouter chat completions."""

import asyncio
import json
import logging
import os
import random
import re
import subprocess
import sys
from collections import OrderedDict, defaultdict
from datetime import datetime
from pathlib import Path
from time import perf_counter

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env", override=True)

from shared.discord_utils import is_guild_channel, sanitize, split_message

STATE_FILE = PROJECT_ROOT / "data" / "openrouter_state.json"
LOG_FILE = PROJECT_ROOT / "logs" / "openrouterbot.log"

DISCORD_TOKEN = os.getenv("OPENROUTER_DISCORD_TOKEN", "")
BOT_USER_ID = int(os.getenv("OPENROUTER_BOT_USER_ID", "0") or "0")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_URL = os.getenv("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions")
OPENROUTER_GENERATION_URL = os.getenv("OPENROUTER_GENERATION_URL", "https://openrouter.ai/api/v1/generation")
DEFAULT_MODEL = os.getenv("OPENROUTER_BOT_MODEL", "").strip()
MAX_CONTEXT_MSGS = int(os.getenv("OPENROUTER_CONTEXT_MSGS", "220"))
MAX_CONTEXT_CHARS = int(os.getenv("OPENROUTER_CONTEXT_CHARS", "800000"))
RECENT_CONTEXT_MSGS = int(os.getenv("OPENROUTER_RECENT_CONTEXT_MSGS", "50"))
COLD_START_CONTEXT_MSGS = int(os.getenv("OPENROUTER_COLD_START_CONTEXT_MSGS", "200"))
FULL_CONTEXT_MSGS = int(os.getenv("OPENROUTER_FULL_CONTEXT_MSGS", "1000"))
FULL_CONTEXT_CHARS = int(os.getenv("OPENROUTER_FULL_CONTEXT_CHARS", "3000000"))
USE_ROLLING_CONTEXT = os.getenv("OPENROUTER_USE_ROLLING_CONTEXT", "0").lower() in ("1", "true", "yes", "on")
MAX_IMAGE_CONTEXT = int(os.getenv("OPENROUTER_MAX_IMAGE_CONTEXT", "12"))
TEMPERATURE = float(os.getenv("OPENROUTER_TEMPERATURE", "0.8"))
TYPING_INTERVAL = 8
RETRY_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}
PREFILL_REACTION = os.getenv("OPENROUTER_PREFILL_REACTION", "\U0001f4d6")
THINKING_REACTION = os.getenv("OPENROUTER_THINKING_REACTION", "\U0001f9e0")
WRITING_REACTION = os.getenv("OPENROUTER_WRITING_REACTION", "\u270d\ufe0f")

SYSTEM_PROMPT = os.getenv(
    "OPENROUTER_SYSTEM_PROMPT",
    "You are openrouter, a lightweight Discord bot. Reply directly and conversationally. "
    "Assistant-role messages in your context are your own previous replies as openrouter; "
    "treat them as conversational continuity, not as a transcript to summarize. "
    "Do not recap the room or announce that you are catching up unless someone asks for that. "
    "You have no tools. Do not claim to have inspected files, images, voice, or external state "
    "unless it is included in the prompt. Keep answers compact unless asked for detail.",
)

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
log = logging.getLogger("openrouterbot")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

_boot_time = datetime.utcnow()
_tree_synced = False
_processed_msgs: set[int] = set()
_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_history: dict[int, list[dict]] = defaultdict(list)
_models: dict[int, str] = defaultdict(lambda: DEFAULT_MODEL)
_spent_by_channel: dict[int, float] = defaultdict(float)


class ReasoningCache:
    def __init__(self, maxsize: int = 100):
        self._cache: OrderedDict[int, str] = OrderedDict()
        self._maxsize = maxsize

    def store(self, message_id: int, reasoning: str | None) -> None:
        reasoning = (reasoning or "").strip()
        if not reasoning:
            return
        self._cache.pop(message_id, None)
        while len(self._cache) >= self._maxsize:
            self._cache.popitem(last=False)
        self._cache[message_id] = reasoning

    def get(self, message_id: int) -> str | None:
        reasoning = self._cache.get(message_id)
        if reasoning is not None:
            self._cache.move_to_end(message_id)
        return reasoning


reasoning_cache = ReasoningCache()


def _load_state() -> None:
    try:
        data = json.loads(STATE_FILE.read_text("utf-8"))
    except Exception:
        return
    for key, model in (data.get("models") or {}).items():
        try:
            _models[int(key)] = str(model)
        except Exception:
            pass
    for key, cost in (data.get("spent_by_channel") or {}).items():
        try:
            _spent_by_channel[int(key)] = float(cost)
        except Exception:
            pass


def _save_state() -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "models": {str(k): v for k, v in _models.items()},
        "spent_by_channel": {str(k): v for k, v in _spent_by_channel.items()},
    }
    STATE_FILE.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _set_env_value(key: str, value: str) -> None:
    env_path = PROJECT_ROOT / ".env"
    try:
        text = env_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    line = f"{key}={value}"
    if re.search(rf"^{re.escape(key)}=.*$", text, flags=re.MULTILINE):
        text = re.sub(rf"^{re.escape(key)}=.*$", line, text, flags=re.MULTILINE)
    else:
        text = text.rstrip() + ("\n" if text.strip() else "") + line + "\n"
    env_path.write_text(text, encoding="utf-8")


def _compile_self() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "py_compile", str(PROJECT_ROOT / "openrouter_bot.py"), str(Path(__file__))],
        check=True,
        capture_output=True,
        text=True,
    )


def _spawn_restart() -> None:
    subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / "openrouter_bot.py")],
        cwd=str(PROJECT_ROOT),
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS if os.name == "nt" else 0,
        close_fds=True,
    )


def _ctx_key(channel: discord.abc.Messageable) -> int:
    return int(getattr(channel, "id", 0))


def _strip_mention(content: str) -> str:
    if client.user:
        content = re.sub(rf"<@!?{client.user.id}>", "", content)
    return content.strip()


def _content_chars(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                total += len(part.get("text") or "")
        return total
    return len(str(content))


def _trim_history(channel_id: int) -> None:
    hist = _history[channel_id][-MAX_CONTEXT_MSGS:]
    while sum(_content_chars(m.get("content", "")) for m in hist) > MAX_CONTEXT_CHARS and len(hist) > 2:
        hist = hist[2:]
    _history[channel_id] = hist


def _add_history(channel_id: int, role: str, content) -> None:
    if isinstance(content, str):
        content = _strip_reasoning_text(content)
    if not content:
        return
    _history[channel_id].append({"role": role, "content": content})
    _trim_history(channel_id)


async def _safe_add_reaction(message: discord.Message, emoji: str) -> None:
    if not emoji:
        return
    try:
        await message.add_reaction(emoji)
    except Exception:
        log.exception("Failed to add reaction %r", emoji)


async def _safe_remove_reaction(message: discord.Message, emoji: str) -> None:
    if not emoji or not client.user:
        return
    try:
        await message.remove_reaction(emoji, client.user)
    except Exception:
        pass


def _build_user_content(text: str, image_parts: list[dict]) -> str | list[dict]:
    if image_parts:
        return [{"type": "text", "text": text}, *image_parts[:MAX_IMAGE_CONTEXT]]
    return text


def _strip_reasoning_text(text: str | None) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<reasoning>.*?</reasoning>", "", text, flags=re.DOTALL | re.IGNORECASE)
    return text.strip()


def _parse_model_spec(model_spec: str) -> tuple[str, dict | None]:
    model_spec = model_spec.strip()
    if "::" not in model_spec:
        return model_spec, None
    model, route = (part.strip() for part in model_spec.split("::", 1))
    if not route:
        return model, None
    route_lower = route.lower()
    if route_lower in ("cheap", "price"):
        return model, {"sort": "price"}
    if route_lower in ("fast", "throughput"):
        return model, {"sort": "throughput"}
    if route_lower == "latency":
        return model, {"sort": "latency"}
    return model, {"order": [route]}


def _is_public_reasoning_reveal(text: str) -> bool:
    text = (text or "").lstrip()
    return bool(
        re.match(
            r"\*\*(?:OpenRouter reasoning|Reasoning)(?: \d+/\d+)?\*\* \(requested by <@!?\d+>\):",
            text,
        )
    )


def _usage_line(model: str, usage: dict | None, cost: float | None, channel_id: int) -> str:
    usage = usage or {}
    prompt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    completion = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    total = usage.get("total_tokens") or (prompt + completion if prompt or completion else 0)
    cache = (
        (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        or (usage.get("input_tokens_details") or {}).get("cached_tokens")
        or 0
    )
    if cost is not None:
        cost_part = f"${cost:.3f}"
    else:
        cost_part = "cost not returned"
    token_part = f"{total:,} tok ({prompt:,}i/{cache:,}c/{completion:,}o)" if total else "usage unavailable"
    return f"-# {model} | {token_part} | {cost_part}"


def _reasoning_chunks(reasoning: str) -> list[str]:
    display = sanitize((reasoning or "").strip()).replace("```", "`\u200b``")
    return split_message(display or "[empty reasoning]", 1750)


async def _send_reasoning_ephemeral(
    interaction: discord.Interaction,
    reasoning: str,
    view: discord.ui.View | None = None,
) -> None:
    chunks = _reasoning_chunks(reasoning)
    total = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        title = "Reasoning" if total == 1 else f"Reasoning {i}/{total}"
        kwargs = {"view": view} if i == 1 and view is not None else {}
        content = f"**{title}:**\n```text\n{chunk}\n```"
        if i == 1:
            await interaction.response.send_message(content, ephemeral=True, **kwargs)
        else:
            await interaction.followup.send(content, ephemeral=True)


async def _send_reasoning_public(
    channel: discord.abc.Messageable,
    reasoning: str,
    requested_by: discord.User | discord.Member,
) -> None:
    chunks = _reasoning_chunks(reasoning)
    total = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        title = "OpenRouter reasoning" if total == 1 else f"OpenRouter reasoning {i}/{total}"
        await channel.send(f"**{title}** (requested by {requested_by.mention}):\n```text\n{chunk}\n```")


class ShowToEveryoneView(discord.ui.View):
    def __init__(self, reasoning: str, channel: discord.abc.Messageable):
        super().__init__(timeout=300)
        self.reasoning = reasoning
        self.channel = channel

    @discord.ui.button(label="Show to Everyone", style=discord.ButtonStyle.secondary)
    async def show_to_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await _send_reasoning_public(self.channel, self.reasoning, interaction.user)
        await interaction.followup.send("Posted!", ephemeral=True)
        self.stop()


class ShowReasoningButton(discord.ui.View):
    def __init__(self, reasoning: str):
        super().__init__(timeout=None)
        self.reasoning = reasoning

    @discord.ui.button(label="Show Reasoning", style=discord.ButtonStyle.secondary)
    async def show(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = ShowToEveryoneView(reasoning=self.reasoning, channel=interaction.channel)
        await _send_reasoning_ephemeral(interaction, self.reasoning, view=view)


@tree.context_menu(name="Show Reasoning")
async def show_reasoning(interaction: discord.Interaction, message: discord.Message):
    if not client.user or message.author.id != client.user.id:
        await interaction.response.send_message("Not my message.", ephemeral=True)
        return
    reasoning = reasoning_cache.get(message.id)
    if reasoning is None:
        await interaction.response.send_message("No reasoning cached.", ephemeral=True)
        return
    view = ShowToEveryoneView(reasoning=reasoning, channel=message.channel)
    await _send_reasoning_ephemeral(interaction, reasoning, view=view)


async def _fetch_generation_cost(headers: dict, generation_id: str | None) -> float | None:
    if not generation_id:
        return None
    url = f"{OPENROUTER_GENERATION_URL}?id={generation_id}"
    payload = None
    for attempt, delay in enumerate([0.0, 1.0, 2.0, 5.0], start=1):
        if delay:
            await asyncio.sleep(delay)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                async with session.get(url, headers=headers) as resp:
                    text = await resp.text()
                    if resp.status == 200:
                        payload = json.loads(text)
                        break
                    if resp.status == 404 and attempt < 4:
                        continue
                    log.info("OpenRouter generation lookup failed %s: %s", resp.status, text[:300])
                    return None
        except Exception:
            log.exception("OpenRouter generation lookup failed")
            return None
    if payload is None:
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = payload if isinstance(payload, dict) else {}
    for key in ("total_cost", "cost", "usage_cost"):
        if data.get(key) is not None:
            try:
                return float(data[key])
            except Exception:
                pass
    return None


async def _call_openrouter(
    channel_id: int,
    live_messages: list[dict] | None = None,
    on_thinking_start=None,
    on_writing_start=None,
) -> tuple[str, str | None, dict | None, float | None, bool]:
    model_spec = _models[channel_id]
    model, provider = _parse_model_spec(model_spec)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if USE_ROLLING_CONTEXT:
        messages.extend(_history[channel_id])
    if live_messages:
        messages.extend(live_messages)
    prompt_chars = sum(_content_chars(m.get("content", "")) for m in messages)
    role_counts: dict[str, int] = defaultdict(int)
    for msg in messages:
        role_counts[str(msg.get("role") or "?")] += 1
    role_tail = ",".join(str(m.get("role") or "?") for m in messages[-12:])
    payload = {
        "model": model,
        "messages": messages,
        "temperature": TEMPERATURE,
        "stream": True,
        "stream_options": {"include_usage": True},
        "reasoning": {"exclude": False},
        "usage": {"include": True},
    }
    if provider:
        payload["provider"] = provider
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://discord.com",
        "X-Title": "hehe openrouter bot",
    }
    timeout = aiohttp.ClientTimeout(total=None)
    started = perf_counter()
    success_attempt = 0
    log.info(
        "OpenRouter request start model=%s provider=%s spec=%s messages=%d roles=%s tail=%s chars=%d rolling=%s",
        model,
        provider,
        model_spec,
        len(messages),
        dict(role_counts),
        role_tail,
        prompt_chars,
        USE_ROLLING_CONTEXT,
    )
    async with aiohttp.ClientSession(timeout=timeout) as session:
        last_error = None
        last_status = None
        data = None
        usage: dict = {}
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        reasoning_chars = 0
        reasoning_started = False
        writing_started = False
        first_event_elapsed = None
        first_reasoning_elapsed = None
        first_content_elapsed = None
        generation_id = None
        attempt = 0
        while True:
            async with session.post(OPENROUTER_URL, headers=headers, json=payload) as resp:
                if resp.status == 200:
                    success_attempt = attempt + 1
                    buffer = ""
                    event_count = 0
                    async for chunk in resp.content.iter_any():
                        if not chunk:
                            continue
                        if first_event_elapsed is None:
                            first_event_elapsed = perf_counter() - started
                        buffer += chunk.decode("utf-8", errors="replace")
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()
                            if not line or not line.startswith("data:"):
                                continue
                            raw_data = line[5:].strip()
                            if raw_data == "[DONE]":
                                continue
                            try:
                                event = json.loads(raw_data)
                            except json.JSONDecodeError:
                                log.info("OpenRouter stream JSON parse failed: %s", raw_data[:300])
                                continue
                            data = event
                            event_count += 1
                            if event.get("id"):
                                generation_id = event.get("id")
                            if event.get("usage"):
                                usage = event.get("usage") or usage
                            choices = event.get("choices") or []
                            if not choices:
                                continue
                            delta = choices[0].get("delta") or choices[0].get("message") or {}
                            reasoning_delta = (
                                delta.get("reasoning")
                                or delta.get("reasoning_content")
                                or delta.get("thinking")
                            )
                            if reasoning_delta:
                                reasoning_text = str(reasoning_delta)
                                reasoning_parts.append(reasoning_text)
                                reasoning_chars += len(reasoning_text)
                            if reasoning_delta and not reasoning_started and not writing_started:
                                reasoning_started = True
                                first_reasoning_elapsed = perf_counter() - started
                                if on_thinking_start:
                                    await on_thinking_start()
                            content_delta = delta.get("content")
                            if content_delta:
                                if not writing_started:
                                    writing_started = True
                                    first_content_elapsed = perf_counter() - started
                                    if on_writing_start:
                                        await on_writing_start()
                                content_parts.append(content_delta)
                    if not content_parts and not reasoning_parts and not usage:
                        delay = min(2.0 * (2 ** attempt), 45.0) + random.uniform(0.0, 1.5)
                        log.info(
                            "OpenRouter empty 200 stream, retry %d in %.2fs events=%d elapsed=%.2fs",
                            attempt + 1,
                            delay,
                            event_count,
                            perf_counter() - started,
                        )
                        await asyncio.sleep(delay)
                        attempt += 1
                        continue
                    break
                text = await resp.text()
                last_status = resp.status
                last_error = f"OpenRouter error {resp.status}: {text[:1000]}"
                if resp.status not in RETRY_STATUSES:
                    if resp.status == 429:
                        return (
                            "429 - rate limited upstream",
                            None,
                            None,
                            None,
                            True,
                        )
                    return last_error, None, None, None, False
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else None
                except ValueError:
                    delay = None
                if delay is None:
                    cap = 45.0 if resp.status == 429 else 20.0
                    delay = min(2.0 * (2 ** attempt), cap) + random.uniform(0.0, 1.5)
                log.info(
                    "OpenRouter transient %s, retry %d in %.2fs",
                    resp.status,
                    attempt + 1,
                    delay,
                )
            await asyncio.sleep(delay)
            attempt += 1
    content = _strip_reasoning_text("".join(content_parts))
    reasoning = _strip_reasoning_text("".join(reasoning_parts)) or None
    if not content and reasoning:
        content = reasoning
        reasoning = None
    cost_raw = usage["cost"] if "cost" in usage else usage.get("total_cost")
    try:
        cost = float(cost_raw) if cost_raw is not None else None
    except Exception:
        cost = None
    if cost is None:
        cost = await _fetch_generation_cost(headers, generation_id or ((data or {}).get("id") if isinstance(data, dict) else None))
    if cost is None and model.endswith(":free"):
        cost = 0.0
    elapsed = perf_counter() - started
    cache = (
        (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        or (usage.get("input_tokens_details") or {}).get("cached_tokens")
        or 0
    )
    prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    completion_tokens = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    log.info(
        "OpenRouter success model=%s provider=%s spec=%s attempt=%d elapsed=%.2fs first_event=%s first_reasoning=%s first_content=%s chars=%d prompt=%s cached=%s completion=%s reasoning_chars=%d visible_chars=%d cost=%s",
        model,
        provider,
        model_spec,
        success_attempt,
        elapsed,
        f"{first_event_elapsed:.2f}s" if first_event_elapsed is not None else None,
        f"{first_reasoning_elapsed:.2f}s" if first_reasoning_elapsed is not None else None,
        f"{first_content_elapsed:.2f}s" if first_content_elapsed is not None else None,
        prompt_chars,
        prompt_tokens,
        cache,
        completion_tokens,
        reasoning_chars,
        len(content),
        cost,
    )
    return content or "[empty response]", reasoning, usage, cost, False


def _is_image_attachment(att: discord.Attachment) -> bool:
    ctype = (att.content_type or "").lower()
    name = (att.filename or "").lower()
    return ctype.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))


def _image_part(url: str) -> dict:
    return {"type": "image_url", "image_url": {"url": url}}


def _attachment_text(msg: discord.Message, text: str) -> str:
    if msg.attachments:
        names = ", ".join(a.filename or "attachment" for a in msg.attachments)
        text = f"{text}\n[attachments: {names}]".strip()
    return text


async def _discord_transcript_context(
    message: discord.Message,
    limit: int,
    stop_at_self: bool,
    max_chars: int | None = None,
) -> tuple[str, list[dict]]:
    lines: list[str] = []
    images: list[dict] = []
    try:
        async for hist_msg in message.channel.history(limit=limit, before=message):
            if stop_at_self and hist_msg.author == client.user:
                break
            if hist_msg.id in _processed_msgs:
                continue
            text = (hist_msg.content or "").strip()
            if hist_msg.author == client.user and (text.startswith("-# ") or _is_public_reasoning_reveal(text)):
                continue
            text = _attachment_text(hist_msg, text)
            if hist_msg.attachments:
                for att in hist_msg.attachments:
                    if len(images) >= MAX_IMAGE_CONTEXT:
                        break
                    if _is_image_attachment(att):
                        images.append(_image_part(att.url))
            if not text:
                continue
            author = hist_msg.author.display_name or hist_msg.author.name
            lines.append(f"{author}: {text}")
            if max_chars and sum(len(x) for x in lines) >= max_chars:
                break
    except Exception:
        log.exception("Failed to fetch Discord context")
    lines.reverse()
    images.reverse()
    if max_chars:
        while lines and sum(len(x) for x in lines) > max_chars:
            lines.pop(0)
    text = "\n".join(lines)
    return text, images


async def _recent_context(message: discord.Message) -> tuple[str, list[dict]]:
    text, images = await _discord_transcript_context(
        message,
        limit=RECENT_CONTEXT_MSGS + 8,
        stop_at_self=True,
    )
    if text:
        text = "[Recent messages since my last message]\n" + text
    return text, images


async def _full_context(message: discord.Message) -> tuple[str, list[dict]]:
    text, images = await _discord_transcript_context(
        message,
        limit=FULL_CONTEXT_MSGS,
        stop_at_self=False,
        max_chars=FULL_CONTEXT_CHARS,
    )
    if text:
        text = "[Discord transcript context]\n" + text
    return text, images


async def _full_context_messages(message: discord.Message) -> tuple[list[dict], list[dict]]:
    raw_items: list[tuple[str, str | list[dict]]] = []
    image_count = 0
    total_chars = 0
    try:
        async for hist_msg in message.channel.history(limit=FULL_CONTEXT_MSGS, before=message):
            text = (hist_msg.content or "").strip()
            if hist_msg.author == client.user and (text.startswith("-# ") or _is_public_reasoning_reveal(text)):
                continue
            text = _attachment_text(hist_msg, text)
            if not text:
                continue
            if hist_msg.author == client.user:
                role = "assistant"
                content = text
            else:
                author = hist_msg.author.display_name or hist_msg.author.name
                user_text = f"{author}: {text}"
                image_parts: list[dict] = []
                if hist_msg.attachments and image_count < MAX_IMAGE_CONTEXT:
                    for att in hist_msg.attachments:
                        if image_count >= MAX_IMAGE_CONTEXT:
                            break
                        if _is_image_attachment(att):
                            image_parts.append(_image_part(att.url))
                            image_count += 1
                role = "user"
                content = _build_user_content(user_text, image_parts)
            raw_items.append((role, content))
            total_chars += _content_chars(content)
            if total_chars >= FULL_CONTEXT_CHARS:
                break
    except Exception:
        log.exception("Failed to fetch full Discord context messages")
        return [], []
    raw_items.reverse()

    recovered: list[dict] = []
    pending_lines: list[str] = []
    pending_images: list[dict] = []

    def flush_user_dump() -> None:
        if not pending_lines and not pending_images:
            return
        text = "[Discord room transcript]\n" + "\n".join(pending_lines)
        recovered.append({"role": "user", "content": _build_user_content(text, pending_images)})
        pending_lines.clear()
        pending_images.clear()

    for role, content in raw_items:
        if role == "assistant":
            flush_user_dump()
            recovered.append({"role": "assistant", "content": content})
            continue
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    pending_lines.append(part.get("text") or "")
                elif isinstance(part, dict) and part.get("type") == "image_url":
                    pending_images.append(part)
        else:
            pending_lines.append(str(content))
    flush_user_dump()

    while recovered and sum(_content_chars(m.get("content", "")) for m in recovered) > FULL_CONTEXT_CHARS:
        recovered.pop(0)
    return recovered, []


async def _hydrate_context_from_discord(message: discord.Message, channel_id: int) -> None:
    if _history[channel_id]:
        return
    recovered: list[dict] = []
    try:
        async for hist_msg in message.channel.history(limit=COLD_START_CONTEXT_MSGS, before=message):
            text = (hist_msg.content or "").strip()
            text = _attachment_text(hist_msg, text)
            if not text:
                continue
            if hist_msg.author == client.user:
                # Skip our accounting footers; keep actual response text.
                if text.startswith("-# ") or _is_public_reasoning_reveal(text):
                    continue
                role = "assistant"
                content = text
            else:
                role = "user"
                author = hist_msg.author.display_name or hist_msg.author.name
                content = f"{author}: {text}"
            recovered.append({"role": role, "content": content})
    except Exception:
        log.exception("Failed to hydrate context from Discord")
        return
    recovered.reverse()
    _history[channel_id] = recovered[-MAX_CONTEXT_MSGS:]
    _trim_history(channel_id)


async def _send_long(
    channel: discord.abc.Messageable,
    text: str,
    reply_to: discord.Message | None = None,
    view: discord.ui.View | None = None,
) -> discord.Message | None:
    chunks = split_message(sanitize(text), 1900)
    first_sent = None
    for i, chunk in enumerate(chunks):
        kwargs = {"view": view} if i == 0 and view is not None else {}
        if i == 0 and reply_to is not None:
            sent = await reply_to.reply(chunk, mention_author=False, **kwargs)
        else:
            sent = await channel.send(chunk, **kwargs)
        if first_sent is None:
            first_sent = sent
        log.info(
            "Discord sent OpenRouter chunk %d/%d message_id=%s chars=%d has_view=%s preview=%r",
            i + 1,
            len(chunks),
            sent.id,
            len(chunk),
            bool(kwargs.get("view")),
            chunk[:120],
        )
    return first_sent


@client.event
async def on_ready():
    global _tree_synced
    log.info("Logged in as %s (ID: %s)", client.user, client.user.id if client.user else "?")
    if _tree_synced:
        return
    try:
        for guild in client.guilds:
            tree.copy_global_to(guild=guild)
            await tree.sync(guild=guild)
        _tree_synced = True
        log.info("Synced OpenRouter app commands for %d guild(s)", len(client.guilds))
    except Exception:
        log.exception("Failed to sync OpenRouter app commands")


@client.event
async def on_message(message: discord.Message):
    received_at = perf_counter()
    if message.created_at.replace(tzinfo=None) < _boot_time:
        return
    if message.id in _processed_msgs:
        return
    _processed_msgs.add(message.id)
    if len(_processed_msgs) > 500:
        _processed_msgs.clear()
    if not is_guild_channel(message.channel) or message.author == client.user:
        return

    channel_id = _ctx_key(message.channel)
    raw = (message.content or "").strip()
    content = _strip_mention(raw)
    cmd = content.strip()

    if cmd.startswith(".set-model "):
        global DEFAULT_MODEL
        model = cmd.split(None, 1)[1].strip()
        parsed_model, parsed_provider = _parse_model_spec(model)
        if "/" not in parsed_model or not parsed_model or model.endswith("::"):
            await message.reply("model should look like `provider/model-slug` or `provider/model-slug::openrouter-provider`", mention_author=False)
            return
        try:
            _compile_self()
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or exc.stdout or str(exc)).strip()
            await message.reply(f"set-model blocked: compile failed\n```text\n{err[:1500]}\n```", mention_author=False)
            return
        DEFAULT_MODEL = model
        os.environ["OPENROUTER_BOT_MODEL"] = model
        _set_env_value("OPENROUTER_BOT_MODEL", model)
        _models[channel_id] = model
        _save_state()
        await message.reply(f"model set to `{model}`, saved to `.env`, restarting", mention_author=False)
        _spawn_restart()
        await asyncio.sleep(0.5)
        os._exit(0)

    if cmd == ".model":
        await message.reply(f"current model: `{_models[channel_id]}`", mention_author=False)
        return

    if cmd == ".reset":
        _history[channel_id].clear()
        await message.reply("context cleared", mention_author=False)
        return

    if cmd in ("reload", ".reload"):
        try:
            _compile_self()
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or exc.stdout or str(exc)).strip()
            await message.reply(f"reload blocked: compile failed\n```text\n{err[:1500]}\n```", mention_author=False)
            return
        await message.add_reaction("\u2705")
        _spawn_restart()
        await asyncio.sleep(0.5)
        os._exit(0)

    mentioned = client.user in message.mentions if client.user else False
    replying_to_us = False
    if message.reference:
        ref = message.reference.resolved
        if ref is None:
            try:
                ref = await message.channel.fetch_message(message.reference.message_id)
            except Exception:
                ref = None
        replying_to_us = isinstance(ref, discord.Message) and ref.author == client.user

    if not mentioned and not replying_to_us:
        return
    if not OPENROUTER_API_KEY:
        await message.reply("OPENROUTER_API_KEY is not set", mention_author=False)
        return

    prefill_reacted = False
    await _safe_add_reaction(message, PREFILL_REACTION)
    prefill_reacted = True

    context_started = perf_counter()
    full_messages, _ = await _full_context_messages(message)
    context_elapsed = perf_counter() - context_started
    if not content and not message.attachments:
        has_prior_user_context = any(
            m.get("role") == "user" and _content_chars(m.get("content", "")) > 0
            for m in full_messages
        )
        if not has_prior_user_context:
            await _safe_remove_reaction(message, PREFILL_REACTION)
            return

    att_note = ""
    current_images: list[dict] = []
    if message.attachments:
        names = ", ".join(a.filename or "attachment" for a in message.attachments)
        att_note = f"\n\n[attachments: {names}]"
        for att in message.attachments:
            if len(current_images) >= MAX_IMAGE_CONTEXT:
                break
            if _is_image_attachment(att):
                current_images.append(_image_part(att.url))
    prompt_text = content or "[ping]"
    current_text = f"{message.author.display_name}: {prompt_text}{att_note}".strip()
    current_user_content = _build_user_content(current_text, current_images)
    live_messages = [*full_messages, {"role": "user", "content": current_user_content}]
    durable_user_content = _build_user_content(current_text, current_images)
    live_chars = sum(_content_chars(m.get("content", "")) for m in live_messages)
    assistant_turns = sum(1 for m in full_messages if m.get("role") == "assistant")
    user_turns = sum(1 for m in full_messages if m.get("role") == "user")
    log.info(
        "OpenRouter context built elapsed=%.2fs since_receive=%.2fs messages=%d user=%d assistant=%d chars=%d current_images=%d",
        context_elapsed,
        perf_counter() - received_at,
        len(live_messages),
        user_turns,
        assistant_turns,
        live_chars,
        len(current_images),
    )

    async with _locks[channel_id]:
        if USE_ROLLING_CONTEXT:
            await _hydrate_context_from_discord(message, channel_id)
        thinking_reacted = False
        writing_reacted = False

        async def mark_thinking() -> None:
            nonlocal prefill_reacted, thinking_reacted
            if not thinking_reacted and not writing_reacted:
                if prefill_reacted:
                    await _safe_remove_reaction(message, PREFILL_REACTION)
                    prefill_reacted = False
                await _safe_add_reaction(message, THINKING_REACTION)
                thinking_reacted = True

        async def mark_writing() -> None:
            nonlocal prefill_reacted, thinking_reacted, writing_reacted
            if prefill_reacted:
                await _safe_remove_reaction(message, PREFILL_REACTION)
                prefill_reacted = False
            if thinking_reacted:
                await _safe_remove_reaction(message, THINKING_REACTION)
                thinking_reacted = False
            if not writing_reacted:
                await _safe_add_reaction(message, WRITING_REACTION)
                writing_reacted = True

        try:
            async with message.channel.typing():
                reply, reasoning, usage, cost, suppress_footer = await _call_openrouter(
                    channel_id,
                    live_messages,
                    on_thinking_start=mark_thinking,
                    on_writing_start=mark_writing,
                )
        except Exception as exc:
            await _safe_remove_reaction(message, PREFILL_REACTION)
            await _safe_remove_reaction(message, THINKING_REACTION)
            await _safe_remove_reaction(message, WRITING_REACTION)
            log.exception("OpenRouter call failed")
            await message.reply(f"OpenRouter call failed: {type(exc).__name__}: {exc}", mention_author=False)
            return
        if not writing_reacted:
            await mark_writing()
        try:
            _add_history(channel_id, "user", durable_user_content)
            _add_history(channel_id, "assistant", reply)
            view = ShowReasoningButton(reasoning) if reasoning else None
            log.info(
                "Sending OpenRouter reply visible_chars=%d reasoning_chars=%d suppress_footer=%s preview=%r",
                len(reply),
                len(reasoning or ""),
                suppress_footer,
                reply[:160],
            )
            sent = await _send_long(message.channel, reply, reply_to=message, view=view)
            if sent and reasoning:
                reasoning_cache.store(sent.id, reasoning)
            if not suppress_footer:
                footer = _usage_line(_models[channel_id], usage, cost, channel_id)
                await message.channel.send(footer)
                log.info("Discord sent OpenRouter footer chars=%d", len(footer))
        except Exception:
            log.exception("Failed to send OpenRouter response")
            await message.reply("OpenRouter response generated, but Discord send failed. Check log.", mention_author=False)
        finally:
            await _safe_remove_reaction(message, PREFILL_REACTION)
            await _safe_remove_reaction(message, THINKING_REACTION)
            await _safe_remove_reaction(message, WRITING_REACTION)


def main():
    _load_state()
    if not DISCORD_TOKEN:
        raise SystemExit("OPENROUTER_DISCORD_TOKEN is not set")
    if not DEFAULT_MODEL:
        raise SystemExit("OPENROUTER_BOT_MODEL is not set")
    client.run(DISCORD_TOKEN)
