#!/usr/bin/env python3
"""
codex_bot — Discord <-> OpenAI Codex CLI bridge

Uses `codex app-server` over stdio for a persistent JSON-RPC connection.
One app-server process, multiple conversations (one per Discord channel/thread).
Streaming text deltas, auto-approved command execution, interrupt support.

Mention the bot or reply to it to interact.
"""

import os
import sys
import re
import json
import asyncio
import subprocess
import logging
import time
from pathlib import Path
from datetime import datetime

import discord
from dotenv import load_dotenv
from codex.actions import configure as configure_actions
from codex.actions import execute_bot_actions as dispatch_bot_actions
from codex.bridge import CodexAppServer as BridgeCodexAppServer
from codex.bridge import configure as configure_bridge
from codex.prompts import build_system_context as build_system_context_prompt
from codex.prompts import build_thread_context as build_thread_context_prompt
from shared.bot_actions import extract_bot_actions
from shared.discord_utils import guild_docs_dir as _guild_docs_dir
from shared.discord_utils import guild_slug as _guild_slug
from shared.discord_utils import is_guild_channel as _is_guild_channel
from shared.discord_utils import sanitize
from shared.discord_utils import split_message
from shared.state import BotState

load_dotenv()

# ── Config ───────────────────────────────────────────────────────────────────

DISCORD_TOKEN = os.getenv("CODEX_DISCORD_TOKEN", "")
BOT_USER_ID = int(os.getenv("CODEX_BOT_USER_ID", "0"))
CLAUDE_BOT_USER_ID = os.getenv("BOT_USER_ID", "1466773230147604651")
_codex_default = "codex.cmd" if os.name == "nt" else "codex"
CODEX_CMD = os.getenv("CODEX_CMD", _codex_default)
CODEX_MODEL = os.getenv("CODEX_MODEL", "")  # blank = use server default
DOCUMENTS_DIR = Path.home() / "Documents"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = PROJECT_ROOT / "data" / "codex_state.json"
TYPING_INTERVAL = 8

HOME_CHANNEL_ID = int(os.getenv("HOME_CHANNEL_ID", "0"))
PRIMARY_GUILD_ID: int = 0

CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# ── Hot Reload ────────────────────────────────────────────────────────────────

_BOT_FILE = Path(__file__)
_BOOT_MTIME = _BOT_FILE.stat().st_mtime


def _self_modified() -> bool:
    try:
        return _BOT_FILE.stat().st_mtime != _BOOT_MTIME
    except Exception:
        return False

log = logging.getLogger("codexbot")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(PROJECT_ROOT / "logs" / "codexbot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)


# ── Discord Bot ──────────────────────────────────────────────────────────────

state = BotState(STATE_FILE)
configure_bridge(state, log, CODEX_CMD, CODEX_MODEL, DOCUMENTS_DIR, CREATE_FLAGS)
bridge = BridgeCodexAppServer()
_processed_msgs: set[int] = set()
_boot_time = datetime.utcnow()
_injected_inputs: dict[str, list[dict]] = {}
_injected_lock = asyncio.Lock()

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

client = discord.Client(intents=intents)
configure_actions(state, log, _BOT_FILE, DOCUMENTS_DIR)


async def _heartbeat_loop():
    """Write heartbeat file every 30 seconds."""
    hb_file = PROJECT_ROOT / "data" / "heartbeat_codex.json"
    while not client.is_closed():
        try:
            hb_file.parent.mkdir(parents=True, exist_ok=True)
            hb_file.write_text(
                json.dumps({"timestamp": time.time(), "pid": os.getpid()}),
                "utf-8",
            )
        except Exception:
            pass
        await asyncio.sleep(30)


async def _queue_injected_input(ctx_key: str, prompt: str, attachments: list[str]) -> int:
    async with _injected_lock:
        q = _injected_inputs.setdefault(ctx_key, [])
        q.append({"prompt": prompt, "attachments": attachments})
        return len(q)


async def _pop_injected_inputs(ctx_key: str) -> list[dict]:
    async with _injected_lock:
        return _injected_inputs.pop(ctx_key, [])


@client.event
async def on_ready():
    global PRIMARY_GUILD_ID
    log.info(f"Logged in as {client.user} (ID: {client.user.id})")

    if PRIMARY_GUILD_ID == 0 and HOME_CHANNEL_ID:
        try:
            ch = client.get_channel(HOME_CHANNEL_ID)
            if ch is None:
                ch = await client.fetch_channel(HOME_CHANNEL_ID)
            if ch and hasattr(ch, "guild") and ch.guild:
                PRIMARY_GUILD_ID = ch.guild.id
                log.info(f"Primary guild: {ch.guild.name} ({PRIMARY_GUILD_ID})")
                if not state.get_guild_config(PRIMARY_GUILD_ID):
                    state.set_guild_config(
                        PRIMARY_GUILD_ID, HOME_CHANNEL_ID,
                        _guild_slug(ch.guild), str(DOCUMENTS_DIR),
                    )
                for p in state._data.get("projects", {}).values():
                    if p.get("guild_id") == 0:
                        p["guild_id"] = PRIMARY_GUILD_ID
                state._save()
        except Exception:
            log.warning("Could not auto-detect primary guild from HOME_CHANNEL_ID")

    # Start the app-server
    try:
        await bridge.start()
        log.info("Codex app-server ready")
    except Exception as e:
        log.error(f"Failed to start codex app-server: {e}")
    if not hasattr(client, "_heartbeat_task_started"):
        client._heartbeat_task_started = True
        asyncio.create_task(_heartbeat_loop())


@client.event
async def on_message(message: discord.Message):
    if message.created_at.replace(tzinfo=None) < _boot_time:
        return

    if message.id in _processed_msgs:
        return
    _processed_msgs.add(message.id)
    if len(_processed_msgs) > 200:
        _processed_msgs.clear()

    if not _is_guild_channel(message.channel):
        return

    # ── Should we respond? ───────────────────────────────────
    mentioned = client.user in message.mentions

    if message.author.bot:
        if not mentioned:
            return
    else:
        replying_to_us = False
        if message.reference:
            ref = message.reference.resolved
            if ref is None:
                try:
                    ref = await message.channel.fetch_message(message.reference.message_id)
                except Exception:
                    ref = None
            if ref and isinstance(ref, discord.Message) and ref.author == client.user:
                replying_to_us = True

        if not mentioned and not replying_to_us:
            return

    # ── Extract prompt ───────────────────────────────────────
    content = (message.content or "").strip()
    content = re.sub(rf"<@!?{client.user.id}>", "", content).strip()

    # ── Collect attachments ──────────────────────────────────
    ATT_DIR = PROJECT_ROOT / "data" / "codex_attachments"
    ATT_DIR.mkdir(exist_ok=True)
    att_paths = []
    for att in message.attachments:
        try:
            data = await att.read()
            safe_name = re.sub(r"[^\w.\-]", "_", att.filename or "file")
            att_path = ATT_DIR / f"{att.id}_{safe_name}"
            att_path.write_bytes(data)
            att_paths.append((att.filename or safe_name, str(att_path).replace("\\", "/")))
        except Exception:
            log.warning(f"Failed to download attachment {att.filename}")

    channel = message.channel
    channel_id = channel.id

    # ── Auto-include preceding messages (mentions only, not replies) ──
    # Fetch recent messages since our last message (up to 5) so Codex
    # can see e.g. Claude's diagnostics without copy-pasting.
    prev_context = None
    if mentioned:
        try:
            recent: list[str] = []
            async for hist_msg in channel.history(limit=20, before=message):
                if hist_msg.author == client.user:
                    break  # stop at our last message
                msg_text = (hist_msg.content or "").strip()
                if msg_text:
                    author = hist_msg.author.display_name or hist_msg.author.name
                    recent.append(f"[{author}]\n{msg_text}")
                if len(recent) >= 5:
                    break
            if recent:
                recent.reverse()  # chronological order
                prev_context = "\n\n".join(recent)
        except Exception:
            pass

    if not content and not att_paths and not prev_context:
        return

    # ── Manual reload / restart commands ─────────────────────
    _cmd = content.lower().strip()
    if _cmd == "/usage":
        await message.reply("Usage tracking not implemented for Codex yet.", mention_author=False)
        return

    if _cmd == "restart":
        restart_script = PROJECT_ROOT / "scripts" / "restart.ps1"
        if not restart_script.exists():
            await message.reply("restart.ps1 not found", mention_author=False)
            return
        await message.add_reaction("\u2705")
        await channel.send("Full restart in progress...")
        subprocess.Popen(
            ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(restart_script)],
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
            close_fds=True,
        )
        await asyncio.sleep(1)
        os._exit(0)

    if _cmd == "reload":
        log.info("Manual reload requested - validating")
        try:
            r = subprocess.run(
                [sys.executable, "-c",
                 f"compile(open(r'{_BOT_FILE}', encoding='utf-8').read(), 'codex/bot.py', 'exec')"],
                capture_output=True, text=True, timeout=10,
                creationflags=CREATE_FLAGS,
            )
            if r.returncode != 0:
                err = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "unknown"
                await message.reply(f"Reload aborted - bad syntax:\n```\n{err[:500]}\n```", mention_author=False)
                return
        except Exception as e:
            await message.reply(f"Reload validation failed: {e}", mention_author=False)
            return
        await message.add_reaction("\u2705")
        await bridge.kill()
        await asyncio.sleep(1)
        os._exit(0)

    # Snapshot mtime so we only warn if codex modifies the file *during this turn*
    try:
        _turn_start_mtime = _BOT_FILE.stat().st_mtime
    except Exception:
        _turn_start_mtime = 0

    # ── Check app-server health ──────────────────────────────
    if not bridge._alive:
        try:
            await bridge.start()
        except Exception as e:
            await message.reply(f"Codex app-server failed to start: {e}", mention_author=False)
            return

    # ── Resolve guild context ────────────────────────────────
    guild = message.guild
    guild_id = guild.id

    guild_config = state.get_guild_config(guild_id)
    if not guild_config:
        slug = _guild_slug(guild)
        docs_dir = _guild_docs_dir(guild_id, guild)
        docs_dir.mkdir(parents=True, exist_ok=True)
        home_ch = channel.id if not isinstance(channel, discord.Thread) else getattr(channel, "parent_id", channel.id)
        state.set_guild_config(guild_id, home_ch, slug, str(docs_dir))
        guild_config = state.get_guild_config(guild_id)

    docs_dir = Path(guild_config["docs_dir"])

    # ── Resolve context ──────────────────────────────────────
    ctx_key = str(channel_id)
    cwd = str(docs_dir)
    label = None
    is_orchestrator = True

    is_thread = isinstance(channel, discord.Thread)
    if is_thread:
        is_orchestrator = False
        thread_name = channel.name
        tp = state.find_project_by_thread(channel_id)
        if tp:
            label, proj = tp
            cwd = proj["folder"]
            ctx_key = f"proj:{label}"
        else:
            label = thread_name
            folder = docs_dir / thread_name
            folder.mkdir(parents=True, exist_ok=True)
            cwd = str(folder)
            ctx_key = f"thread:{channel_id}"

    # ── Build user prompt ────────────────────────────────────
    username = message.author.display_name or message.author.name
    ch_name = getattr(channel, "name", "DM")

    prompt_text = content if content else "(see attachments)"
    if att_paths:
        names = [name for name, _ in att_paths]
        paths = [path for _, path in att_paths]
        att_note = f"[uploaded {len(att_paths)} attachment{'s' if len(att_paths) > 1 else ''}: {', '.join(names)}]"
        path_note = "\n".join(f"  {path}" for path in paths)
        prompt_text = f"{prompt_text}\n\n{att_note}\nSaved to:\n{path_note}"

    if prev_context:
        if content or att_paths:
            user_msg = f"[Recent messages in channel]\n{prev_context}\n\n{username}: {prompt_text}"
        else:
            user_msg = f"{username} pinged you with no message. Here are the recent messages for context:\n\n{prev_context}"
    else:
        user_msg = f"{username}: {prompt_text}"

    # ── Build system prompt ──────────────────────────────────
    if is_orchestrator:
        ch_name = getattr(channel, "name", "codex")
        srv_name = guild.name if guild else ""
        sys_prompt = build_system_context_prompt(
            CLAUDE_BOT_USER_ID,
            channel_name=ch_name,
            server_name=srv_name,
            docs_dir=str(docs_dir),
        )
    else:
        sys_prompt = build_thread_context_prompt()

    cleanup_paths = [p for _, p in att_paths]

    # ── If busy, queue + inject ──────────────────────────────
    if bridge.is_busy(ctx_key):
        await _queue_injected_input(ctx_key, user_msg, cleanup_paths)
        await bridge.interrupt(ctx_key)
        return

    # ── Run turn ─────────────────────────────────────────────

    async def _keep_typing():
        try:
            while True:
                await channel.typing()
                await asyncio.sleep(TYPING_INTERVAL)
        except asyncio.CancelledError:
            pass

    typing_task = asyncio.create_task(_keep_typing())
    await channel.typing()

    current_text = ""
    sent_text_len = 0
    tool_log = []

    async def _flush_unsent_text():
        nonlocal sent_text_len
        if len(current_text) <= sent_text_len:
            return
        unsent = current_text[sent_text_len:]
        cleaned, _ = extract_bot_actions(unsent)
        cleaned = cleaned.strip()
        if cleaned:
            for chunk in split_message(sanitize(cleaned)):
                try:
                    await channel.send(chunk)
                except Exception:
                    pass
        sent_text_len = len(current_text)

    async def on_text(full_text: str):
        nonlocal current_text
        current_text = full_text

    async def on_tool(desc: str):
        tool_log.append(desc)
        log.info(f"tool: {desc}")
        # Flush any intermediate text before each tool status update.
        await _flush_unsent_text()
        try:
            await channel.send(desc)
        except Exception as e:
            log.warning(f"Failed to send tool status: {e}")

    next_prompt = user_msg
    pending_reload = False
    try:
        while next_prompt:
            current_text = ""
            sent_text_len = 0
            tool_log = []
            try:
                result = await bridge.send_turn(
                    ctx_key, cwd, next_prompt,
                    system_prompt=sys_prompt,
                    on_text=on_text, on_tool=on_tool,
                )
            except Exception as e:
                log.exception("Codex bridge error")
                await message.reply(f"Error: {e}", mention_author=False)
                return

            # Persist conversation_id for resume across restarts
            conv_id = result.get("conversation_id")
            if conv_id:
                state.set_session(ctx_key, conv_id, cwd, label)

            log.info(f"turn finished: error={result['error']} text_len={len(result.get('text',''))} "
                     f"tools={len(result.get('tools',[]))} current_text_len={len(current_text)} "
                     f"sent_text_len={sent_text_len}")

            if result["error"]:
                err = result.get("error_message") or "Unknown error"
                await message.reply(f"Error:\n```\n{err[:1800]}\n```", mention_author=False)
                return

            text = current_text or result.get("text", "")
            if text:
                cleaned_text, actions = extract_bot_actions(text)

                if sent_text_len > 0:
                    unsent_raw = current_text[sent_text_len:] if sent_text_len < len(current_text) else ""
                    final_cleaned, _ = extract_bot_actions(unsent_raw)
                    final_text = final_cleaned.strip()
                else:
                    final_text = cleaned_text

                if final_text:
                    text_chunks = split_message(sanitize(final_text))
                    try:
                        await message.reply(text_chunks[0], mention_author=False)
                    except Exception as e:
                        log.warning(f"Failed to reply: {e}")
                        try:
                            await channel.send(text_chunks[0])
                        except Exception as e2:
                            log.warning(f"Fallback send also failed: {e2}")
                    for chunk in text_chunks[1:]:
                        try:
                            await channel.send(chunk)
                        except Exception as e:
                            log.warning(f"Failed to send chunk: {e}")

                if actions:
                    action_results, pending_reload = await dispatch_bot_actions(actions, message, channel, guild_id)
                    if action_results:
                        remaining = sanitize("\n".join(action_results))
                        for chunk in split_message(remaining):
                            try:
                                await channel.send(chunk)
                            except Exception:
                                pass
                    if pending_reload:
                        break

            injected = await _pop_injected_inputs(ctx_key)
            if not injected:
                await asyncio.sleep(0)
                injected = await _pop_injected_inputs(ctx_key)
                if not injected:
                    break
            cleanup_paths.extend(
                p for item in injected for p in item.get("attachments", []) if p
            )
            next_prompt = "\n\n".join(item["prompt"] for item in injected if item.get("prompt"))

    finally:
        typing_task.cancel()
        for p in cleanup_paths:
            try:
                os.unlink(p)
            except OSError:
                pass

    if pending_reload:
        log.info("bot_action reload — restarting")
        await message.add_reaction("\u2705")
        await bridge.kill()
        await asyncio.sleep(1)
        os._exit(0)

    if _BOT_FILE.stat().st_mtime != _turn_start_mtime:
        log.info("codex_bot.py was modified during this turn")
        await channel.send("codex_bot.py was modified. Say `reload` to apply changes.")


# ── Entry Point ──────────────────────────────────────────────────────────────

def main():
    if not DISCORD_TOKEN:
        print("Error: CODEX_DISCORD_TOKEN not set in .env")
        raise SystemExit(1)

    try:
        r = subprocess.run(
            [CODEX_CMD, "--version"],
            capture_output=True, text=True, timeout=10,
            creationflags=CREATE_FLAGS,
        )
        print(f"Codex CLI: {r.stdout.strip()}")
    except FileNotFoundError:
        print(f"'{CODEX_CMD}' not found. Is Codex installed and in PATH?")
        raise SystemExit(1)

    print("Starting codex_bot (app-server mode)...")
    client.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
