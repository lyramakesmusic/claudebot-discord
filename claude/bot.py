#!/usr/bin/env python3
"""
claudebot - Discord <-> Claude Code bridge

Architecture:
  #claude (main channel) = orchestrator Claude Code session (cwd ~/Documents)
  Any thread in #claude   = project Claude Code session (cwd ~/Documents/{thread_name})

The orchestrator handles everything: coding, project management, system queries.
It can request the bot to perform Discord actions (create threads, etc.) via
structured JSON in its response.

Mention the bot or reply to it to interact.
"""

import os
import sys
import re
import asyncio
import subprocess
import logging
from pathlib import Path
from datetime import datetime

import discord
from dotenv import load_dotenv
from claude.attachments import cleanup_message_attachments
from claude.attachments import collect_message_attachments
from claude.contexts import configure as configure_contexts_module
from claude.contexts import try_handle_context_command as try_handle_context_command_module
from claude.bridge import ClaudeBridge as BridgeClaudeBridge
from claude.bridge import configure as configure_bridge_module
from claude.image_gen import bg_generate_image as bg_generate_image_module
from claude.image_gen import configure as configure_image_gen_module
from claude.actions import configure as configure_actions_module
from claude.actions import execute_bot_actions as execute_bot_actions_module
from claude.memories import MEMORY_ACTION_RE
from claude.memories import configure as configure_memories_module
from claude.memories import format_memories_for_prompt as format_memories_for_prompt_module
from claude.memories import process_memory_actions as process_memory_actions_module
from claude.project_seed import configure as configure_project_seed_module
from claude.project_seed import seed_project as seed_project_module
from claude.prompts import build_system_context as build_system_context_module
from claude.prompts import build_thread_context as build_thread_context_module
from claude.prompts import configure as configure_prompts_module
from claude.reminders import PST as PST_MODULE
from claude.reminders import REMINDER_ACTION_RE
from claude.reminders import configure as configure_reminders_module
from claude.reminders import format_reminders_for_prompt as format_reminders_for_prompt_module
from claude.reminders import process_reminder_actions as process_reminder_actions_module
from claude.reminders import reminder_loop as reminder_loop_module
from claude.research import configure as configure_research_module
from claude.research import try_handle_research_command as try_handle_research_command_module
from claude.system_stats import configure as configure_system_stats_module
from claude.system_stats import system_stats as system_stats_module
from shared.bot_actions import extract_bot_actions as extract_bot_actions_module
from shared.config import OWNER_ID
from shared.discord_utils import guild_docs_dir as _guild_docs_dir
from shared.discord_utils import guild_slug as _guild_slug
from shared.discord_utils import is_guild_channel as _is_guild_channel
from shared.discord_utils import sanitize
from shared.discord_utils import split_message
from shared.state import BotState
from shared.usage import context_percent, fetch_plan_usage, format_reset_time

load_dotenv()

# ── Config ───────────────────────────────────────────────────────────────────

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
CLAUDE_CMD = os.getenv("CLAUDE_CMD", "claude")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "")  # blank = default
DOCUMENTS_DIR = Path.home() / "Documents"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = PROJECT_ROOT / "data" / "state.json"
DEFAULT_CWD = str(DOCUMENTS_DIR)
TYPING_INTERVAL = 8       # seconds between typing indicator refreshes

HOME_CHANNEL_ID = int(os.getenv("HOME_CHANNEL_ID", "1466772067968880772"))
PRIMARY_GUILD_ID: int = 0  # auto-detected from HOME_CHANNEL_ID in on_ready
CODEX_BOT_USER_ID = os.getenv("CODEX_BOT_USER_ID", "1473339153839034408")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
IMAGE_MODEL = "google/gemini-3-pro-image-preview"
GENERATED_IMAGES_DIR = PROJECT_ROOT / "data" / "generated_images"

# Suno music generation (see suno.py)
from integrations.suno import init_suno_worker, enqueue_music

# Council (multi-model research) — see council.py
from integrations.council import call_gpt, call_researcher
from integrations.council_prompt import build_opus_council_prompt

# Voice pipeline (see voice.py) — optional, needs websockets + voice_recv
# Patch voice_recv BEFORE importing voice.py (fixes OpusError: corrupted stream)
try:
    from integrations.voice.recv_patch import apply_patch
    apply_patch()
except Exception:
    pass
try:
    from integrations.voice import VoiceManager
except Exception as _voice_err:
    VoiceManager = None
    logging.getLogger("claudebot").warning(f"Voice disabled: {type(_voice_err).__name__}: {_voice_err}")

CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


# ── Hot Reload ────────────────────────────────────────────────────────────────

_BOT_FILE = Path(__file__)
_BOOT_MTIME = _BOT_FILE.stat().st_mtime


def _self_modified() -> bool:
    """Check if bot.py has been modified since startup."""
    try:
        return _BOT_FILE.stat().st_mtime != _BOOT_MTIME
    except Exception:
        return False

log = logging.getLogger("claudebot")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(PROJECT_ROOT / "logs" / "claudebot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)


# ── Discord Bot ──────────────────────────────────────────────────────────────

state = BotState(STATE_FILE)
bridge = BridgeClaudeBridge()
configure_bridge_module(log, CLAUDE_CMD, CLAUDE_MODEL, CREATE_FLAGS)
configure_system_stats_module(CREATE_FLAGS)
configure_image_gen_module(
    openrouter_api_key=OPENROUTER_API_KEY,
    image_model=IMAGE_MODEL,
    generated_images_dir=GENERATED_IMAGES_DIR,
    owner_id=OWNER_ID,
    logger=log,
)
configure_project_seed_module(
    state_obj=state,
    bridge_obj=bridge,
    build_thread_context_fn=build_thread_context_module,
    process_memory_actions_fn=process_memory_actions_module,
    process_reminder_actions_fn=process_reminder_actions_module,
    extract_bot_actions_fn=extract_bot_actions_module,
    split_message_fn=split_message,
    sanitize_fn=sanitize,
    logger=log,
    typing_interval=TYPING_INTERVAL,
)
configure_contexts_module(
    state_obj=state,
    bridge_obj=bridge,
    default_cwd=DEFAULT_CWD,
)
configure_research_module(
    state_obj=state,
    bridge_obj=bridge,
    build_opus_council_prompt_fn=build_opus_council_prompt,
    extract_bot_actions_fn=extract_bot_actions_module,
    execute_bot_actions_fn=execute_bot_actions_module,
    split_message_fn=split_message,
    sanitize_fn=sanitize,
    logger=log,
    typing_interval=TYPING_INTERVAL,
)
_processed_msgs: set[int] = set()  # message IDs we've already handled
_boot_time = datetime.utcnow()  # ignore messages from before we started

# ── Usage tracking ──────────────────────────────────────────────────────────
_last_token_usage: dict[str, dict] = {}     # ctx_key -> last turn's token breakdown

# ── Per-context processing lock (covers send + post-processing) ─────────────
_ctx_processing: set[str] = set()           # ctx_keys currently being handled
_ctx_pending: dict[str, str] = {}           # ctx_key -> queued user_msg to send next turn

# Council: GPT conversation history per thread (channel_id -> list of messages)
_council_gpt_history: dict[int, list[dict]] = {}

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.voice_states = True
intents.members = True

client = discord.Client(intents=intents)
voice_manager = None  # VoiceManager instance if voice deps available


def _configure_actions():
    configure_actions_module(
        state_obj=state,
        client_obj=client,
        voice_manager_obj=voice_manager,
        voice_manager_cls=VoiceManager,
        seed_project_cb=seed_project_module,
        bg_generate_image_cb=bg_generate_image_module,
        system_stats_fn=system_stats_module,
        enqueue_music_fn=enqueue_music,
        call_gpt_fn=call_gpt,
        call_researcher_fn=call_researcher,
        split_message_fn=split_message,
        sanitize_fn=sanitize,
        logger=log,
        bot_file=_BOT_FILE,
        project_root=PROJECT_ROOT,
        documents_dir=DOCUMENTS_DIR,
        create_flags=CREATE_FLAGS,
        council_gpt_history=_council_gpt_history,
    )


_configure_actions()


@client.event
async def on_ready():
    global PRIMARY_GUILD_ID
    log.info(f"Logged in as {client.user} (ID: {client.user.id})")

    # auto-detect primary guild from HOME_CHANNEL_ID
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
                # re-migrate any projects that got guild_id=0 before we knew the real ID
                for p in state._data.get("projects", {}).values():
                    if p.get("guild_id") == 0:
                        p["guild_id"] = PRIMARY_GUILD_ID
                state._save()
        except Exception:
            log.warning("Could not auto-detect primary guild from HOME_CHANNEL_ID")

    configure_memories_module(PROJECT_ROOT / "selfbot", PRIMARY_GUILD_ID, log)
    configure_reminders_module(PROJECT_ROOT / "selfbot" / "reminders.json", OWNER_ID, CREATE_FLAGS, log)
    configure_prompts_module(
        str(_BOT_FILE),
        CODEX_BOT_USER_ID,
        format_memories_for_prompt_module,
        format_reminders_for_prompt_module,
        PST_MODULE,
    )

    if not hasattr(client, "_reminder_task_started"):
        client._reminder_task_started = True
        asyncio.create_task(reminder_loop_module(client, HOME_CHANNEL_ID))

    # start suno music generation queue worker (one at a time)
    init_suno_worker()

    # start voice pipeline (if dependencies available)
    global voice_manager
    if VoiceManager is not None and voice_manager is None:
        try:
            log.info("[Voice] Initializing voice pipeline...")
            voice_manager = VoiceManager(client, bridge)
            await voice_manager.start()
            log.info(f"[Voice] Pipeline ready (running={voice_manager._running})")
        except Exception as _ve:
            import traceback
            _err = traceback.format_exc()
            log.exception("[Voice] Failed to initialize voice pipeline")
            (PROJECT_ROOT / "logs" / "voice_error.txt").write_text(_err)
            # keep voice_manager set so join_voice gives useful errors
    elif VoiceManager is None:
        log.warning("[Voice] VoiceManager not imported — voice disabled")

    _configure_actions()


@client.event
async def on_voice_state_update(member: discord.Member,
                                before: discord.VoiceState,
                                after: discord.VoiceState):
    if voice_manager:
        await voice_manager.on_voice_state_update(member, before, after)


@client.event
async def on_message(message: discord.Message):
    # ignore messages from before this process started (prevents double-response on restart)
    if message.created_at.replace(tzinfo=None) < _boot_time:
        return

    # deduplicate — Discord can replay messages on reconnect
    if message.id in _processed_msgs:
        return
    _processed_msgs.add(message.id)
    # keep the set from growing forever
    if len(_processed_msgs) > 200:
        _processed_msgs.clear()

    # ── Channel restriction: guild channels only (no DMs) ─────
    if not _is_guild_channel(message.channel):
        return

    # ── Should we respond? ───────────────────────────────────
    mentioned = client.user in message.mentions

    # Bots (including sibling codex) can talk to us via explicit @mention only.
    # Never auto-respond to a bot's reply — only mentions.
    if message.author.bot:
        if not mentioned:
            return
    else:
        # Humans: respond to mentions OR replies to our messages
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

    channel = message.channel
    channel_id = channel.id

    # Resolve guild docs config early so /research uses the same pathing.
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

    # ── Stop / interrupt shortcut ────────────────────────────
    raw_text = re.sub(rf"<@!?{client.user.id}>", "", (message.content or "")).strip().lower()
    if raw_text in ("stop", "abort", "cancel", "nevermind"):
        ctx_key = str(channel_id)
        pp = bridge.get_process(ctx_key)
        if pp and pp.is_busy:
            await pp.interrupt()
            await message.add_reaction("\U0001f6d1")  # stop sign
            return
        # fall through to audio stop if not busy
        if voice_manager and voice_manager._playback_tasks:
            await voice_manager.stop_playback()
            await message.add_reaction("\u23f9")
            return
        await message.add_reaction("\U0001f937")  # shrug — nothing to stop
        return
    if raw_text in ("skip", "shut up", "stfu", "stop audio", "stop music", "pause"):
        if voice_manager and voice_manager._playback_tasks:
            await voice_manager.stop_playback()
            await message.add_reaction("\u23f9")  # stop button emoji
            return

    # ── /research command — start a council research thread ──
    if await try_handle_research_command_module(message, raw_text, channel):
        return

    # ── Extract prompt ───────────────────────────────────────
    content = (message.content or "").strip()
    content = re.sub(rf"<@!?{client.user.id}>", "", content).strip()

    # ── Manual reload / restart commands ─────────────────────
    _cmd = content.lower().strip()
    if _cmd == "/usage":
        ctx_key = str(channel_id)
        lines = ["**Usage**"]
        usage = _last_token_usage.get(ctx_key)
        if usage:
            ctx_pct = context_percent(usage["total_tokens"])
            cache_hit = round((usage["cache_read_tokens"] / usage["total_tokens"]) * 100) if usage["total_tokens"] else 0
            lines.append(f"**Context:** {ctx_pct}%" if ctx_pct else "**Context:** unknown")
            lines.append(f"**Last turn:** {usage['total_tokens']:,} tokens ({cache_hit}% cached)")
            lines.append(f"-# {usage['cache_read_tokens']:,} cached, {usage['cache_creation_tokens']:,} new cache, {usage['input_tokens']:,} uncached")
        else:
            lines.append("No usage data yet for this context.")

        # fetch plan utilization
        try:
            plan = await fetch_plan_usage()
            if plan:
                lines.append("")
                lines.append("**Plan Usage:**")
                if plan["five_hour"]:
                    lines.append(f"5h window: **{plan['five_hour']['utilization']}%** (resets in {format_reset_time(plan['five_hour']['resets_at'])})")
                if plan["seven_day"]:
                    lines.append(f"7d window: **{plan['seven_day']['utilization']}%** (resets in {format_reset_time(plan['seven_day']['resets_at'])})")
                if plan.get("seven_day_opus") and plan["seven_day_opus"]["utilization"] > 0:
                    lines.append(f"-# 7d opus: {plan['seven_day_opus']['utilization']}%")
                if plan.get("seven_day_sonnet") and plan["seven_day_sonnet"]["utilization"] > 0:
                    lines.append(f"-# 7d sonnet: {plan['seven_day_sonnet']['utilization']}%")
        except Exception:
            pass

        await message.reply("\n".join(lines), mention_author=False)
        return

    if _cmd == "restart":
        # restart = kill supervisor + all bots, relaunch from scratch
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
        log.info("Manual reload requested — validating syntax")
        # validate bot.py syntax before reloading
        try:
            r = subprocess.run(
                [sys.executable, "-c",
                 f"compile(open(r'{_BOT_FILE}', encoding='utf-8').read(), 'bot.py', 'exec')"],
                capture_output=True, text=True, timeout=10,
                creationflags=CREATE_FLAGS,
            )
            if r.returncode != 0:
                err = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "unknown"
                await message.reply(f"Reload aborted — bad syntax:\n```\n{err[:500]}\n```", mention_author=False)
                return
        except Exception as e:
            await message.reply(f"Reload validation failed: {e}", mention_author=False)
            return
        await message.add_reaction("\u2705")
        await bridge.kill_all()
        await client.close()
        return

    # ── Context switching commands ────────────────────────────
    if await try_handle_context_command_module(message, content, channel_id):
        return

    # ── Collect attachments ──────────────────────────────────
    ATT_DIR = PROJECT_ROOT / "data" / "attachments"
    att_paths = await collect_message_attachments(message, ATT_DIR, log)

    # ── Auto-include preceding messages (mentions only, not replies) ──
    # Fetch recent messages since our last message (up to 5) so Claude
    # can see e.g. codex's diagnostics without copy-pasting.
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
            # any thread auto-maps to {guild_docs_dir}/{thread_name}
            label = thread_name
            folder = docs_dir / thread_name
            folder.mkdir(parents=True, exist_ok=True)
            cwd = str(folder)
            ctx_key = f"thread:{channel_id}"

    session_info = state.get_session(ctx_key)
    session_id = session_info["session_id"] if session_info else None

    prefix = ""

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

    # ── Build system prompt (used at process creation) ───────
    if is_orchestrator:
        ch_name = getattr(channel, "name", "claude")
        srv_name = guild.name if guild else ""
        sys_prompt = build_system_context_module(
            state.all_projects(guild_id),
            channel_name=ch_name,
            server_name=srv_name,
            docs_dir=str(docs_dir),
            guild_id=guild_id,
        )
    else:
        # council threads get the council-specific Opus prompt
        tp_data = state.find_project_by_thread(channel_id)
        is_council = tp_data[1].get("council", False) if tp_data else False
        if is_council:
            sys_prompt = build_opus_council_prompt()
        else:
            sys_prompt = build_thread_context_module()

    # ── Get or create persistent process ─────────────────────
    pp = await bridge.get_or_create(ctx_key, cwd, session_id, sys_prompt)

    # ── If Claude is busy, inject + interrupt so it reads immediately ──
    if pp.is_busy:
        log.info(f"Injecting mid-turn message into {ctx_key}: {user_msg[:80]}")
        await pp.inject(user_msg)
        await pp.interrupt()  # CTRL+C so Claude stops current tool and reads the injection
        cleanup_message_attachments(att_paths)
        return  # the existing turn handler will see it

    # ── If we're still post-processing (sending text/actions), queue for next turn ──
    if ctx_key in _ctx_processing:
        log.info(f"Post-processing in progress for {ctx_key}, queuing: {user_msg[:80]}")
        _ctx_pending[ctx_key] = user_msg  # latest message wins (overwrites previous)
        cleanup_message_attachments(att_paths)
        return

    # ── Run Claude Code ──────────────────────────────────────
    _ctx_processing.add(ctx_key)

    # typing indicator stays alive until we cancel it
    async def _keep_typing():
        try:
            while True:
                await channel.typing()
                await asyncio.sleep(TYPING_INTERVAL)
        except asyncio.CancelledError:
            pass

    typing_task = asyncio.create_task(_keep_typing())
    await channel.typing()

    current_text = ""   # latest accumulated text from Claude
    sent_text_len = 0   # how much of current_text we've already sent as messages
    tool_log = []

    async def _flush_unsent_text():
        """Send any intermediate text we haven't sent yet as a new message."""
        nonlocal sent_text_len
        if len(current_text) <= sent_text_len:
            return
        unsent = current_text[sent_text_len:]
        cleaned = MEMORY_ACTION_RE.sub("", unsent)
        cleaned = REMINDER_ACTION_RE.sub("", cleaned)
        cleaned, _ = extract_bot_actions_module(cleaned)
        cleaned = cleaned.strip()
        if cleaned:
            for chunk in split_message(sanitize(prefix + cleaned)):
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
        # flush any intermediate text Claude wrote before this tool call
        await _flush_unsent_text()
        # send tool status as its own message
        try:
            await channel.send(desc)
        except Exception as e:
            log.warning(f"Failed to send tool status: {e}")

    try:
        result = await pp.send(
            user_msg, on_text=on_text, on_tool=on_tool,
        )
    except Exception as e:
        log.exception("Claude bridge error")
        typing_task.cancel()
        cleanup_message_attachments(att_paths)
        await message.reply(f"{prefix}Error: {e}", mention_author=False)
        return

    log.info(f"bridge.send finished: error={result['error']} text_len={len(result.get('text',''))} tools={len(result.get('tools',[]))}")

    # typing stays alive until we've finished sending everything
    pending_reload = False
    try:
        # ── Save session ─────────────────────────────────────────
        if pp.session_id:
            state.set_session(ctx_key, pp.session_id, cwd, label)

        # ── Handle errors ────────────────────────────────────────
        if result["error"]:
            err = result.get("error_message") or "Unknown error"
            await message.reply(
                f"{prefix}Error:\n```\n{err[:1800]}\n```", mention_author=False
            )

            # Stale session or process died? Kill and retry
            is_stale = session_id and any(
                w in err.lower() for w in ("session", "resume", "not found", "invalid")
            )
            process_died = not pp.alive

            if is_stale or process_died:
                # preserve the session_id from the dead process so we can resume
                resume_id = None if is_stale else (pp.session_id or session_id)
                await bridge.kill_process(ctx_key)
                if is_stale:
                    state.clear_session(ctx_key)
                log.info(f"{'Stale session' if is_stale else 'Dead process'} for {ctx_key}, retrying (resume={resume_id is not None})")
                await channel.send(f"{prefix}{'Session expired' if is_stale else 'Process crashed'}, retrying...")

                # reset intermediate tracking
                current_text = ""
                sent_text_len = 0

                pp = await bridge.get_or_create(ctx_key, cwd, resume_id, sys_prompt)
                result = await pp.send(
                    user_msg, on_text=on_text, on_tool=on_tool,
                )

                if pp.session_id:
                    state.set_session(ctx_key, pp.session_id, cwd, label)
                if result["error"]:
                    err2 = result.get("error_message") or "Still failing"
                    await channel.send(f"{prefix}Retry failed:\n```\n{err2[:1800]}\n```")
                    return
            else:
                return

        # ── Process response ─────────────────────────────────────
        # Use current_text (our accumulated stream) since sent_text_len tracks it.
        # Fall back to result text only if we got nothing from streaming.
        text = current_text or result.get("text", "")
        if not text and sent_text_len == 0:
            await message.reply(f"{prefix}*(empty response)*", mention_author=False)
            return
        if not text:
            # all content was sent as intermediate messages, nothing left
            return

        # process memory and reminder actions (strips blocks from response)
        ch_name = getattr(channel, "name", "DM")
        srv_name = getattr(getattr(channel, "guild", None), "name", "DM")
        text = process_memory_actions_module(text, ch_name, srv_name, guild_id)
        text = process_reminder_actions_module(text, channel_id, ch_name, message.author.id)

        # extract bot actions from response
        cleaned_text, actions = extract_bot_actions_module(text)

        # ── Send Claude's text FIRST, before executing slow actions ──
        if sent_text_len > 0:
            # slice the RAW current_text, then clean — because sent_text_len
            # tracks position in the raw stream, not the processed text
            unsent_raw = current_text[sent_text_len:] if sent_text_len < len(current_text) else ""
            unsent = MEMORY_ACTION_RE.sub("", unsent_raw)
            unsent = REMINDER_ACTION_RE.sub("", unsent)
            final_cleaned, _ = extract_bot_actions_module(unsent)
            final_text = final_cleaned.strip()
        else:
            final_text = cleaned_text

        if final_text:
            text_chunks = split_message(sanitize(prefix + final_text))
            try:
                await message.reply(text_chunks[0], mention_author=False)
            except Exception:
                pass
            for chunk in text_chunks[1:]:
                try:
                    await channel.send(chunk)
                except Exception:
                    pass
            text_already_sent = True
        else:
            text_already_sent = False

        # ── Now execute bot actions (music/image gen can take minutes) ──
        action_results = []
        reply_files: list[discord.File] = []
        pending_reload = False
        universal = {"reload", "upload", "generate_image", "generate_music", "join_voice", "leave_voice", "play_audio", "play_url", "stop_audio", "switch_voice", "call_gpt", "call_researcher"}
        universal_actions = [a for a in actions if a.get("action") in universal]
        other_actions = [a for a in actions if a.get("action") not in universal]
        all_council_feedback: list[str] = []
        if universal_actions:
            uni_results, pending_reload, uni_files, uni_council = await execute_bot_actions_module(universal_actions, message, channel, guild_id, caller_ctx_key=ctx_key)
            action_results.extend(uni_results)
            reply_files.extend(uni_files)
            all_council_feedback.extend(uni_council)
        if other_actions and is_orchestrator:
            other_results, other_reload, other_files, other_council = await execute_bot_actions_module(other_actions, message, channel, guild_id, caller_ctx_key=ctx_key)
            action_results.extend(other_results)
            reply_files.extend(other_files)
            pending_reload = pending_reload or other_reload
            all_council_feedback.extend(other_council)

        # ── Split results: successes → user, errors → Claude Code for retry ──
        error_keywords = ("could not find", "not available", "skipped", "failed", "not currently", "error", "not running")
        user_results = [r for r in action_results if not any(k in r.lower() for k in error_keywords)]
        error_results = [r for r in action_results if any(k in r.lower() for k in error_keywords)]

        # Send success results to Discord
        remaining_parts = []
        if user_results:
            remaining_parts.append("\n".join(user_results))
        remaining = sanitize("\n\n".join(remaining_parts)) if remaining_parts else ""

        if reply_files or remaining:
            if remaining:
                chunks = split_message(remaining)
                try:
                    await channel.send(chunks[0], files=reply_files if reply_files else None)
                except Exception:
                    pass
                for chunk in chunks[1:]:
                    try:
                        await channel.send(chunk)
                    except Exception:
                        pass
                reply_files = []  # already sent
            elif reply_files:
                try:
                    await channel.send(None, files=reply_files)
                except Exception:
                    pass
                reply_files = []

        # Feed errors back to Claude Code so it can retry
        for _feedback_round in range(3):
            if not error_results or not pp.alive:
                break
            feedback = "[bot_action error — retry or inform user]\n" + "\n".join(error_results)
            log.info(f"[actions] Error feedback round {_feedback_round}: {feedback}")
            error_results = []

            fb_result = await pp.send(feedback)
            if not fb_result or fb_result.get("error"):
                break
            fb_text = fb_result.get("text", "")
            if not fb_text:
                break

            fb_text = process_memory_actions_module(fb_text, ch_name, srv_name, guild_id)
            fb_text = process_reminder_actions_module(fb_text, channel_id, ch_name, message.author.id)
            fb_cleaned, fb_actions = extract_bot_actions_module(fb_text)

            if fb_cleaned.strip():
                for chunk in split_message(sanitize(prefix + fb_cleaned.strip())):
                    try:
                        await channel.send(chunk)
                    except Exception:
                        pass

            if not fb_actions:
                break
            fb_res, fb_reload, fb_files, fb_council = await execute_bot_actions_module(fb_actions, message, channel, guild_id, caller_ctx_key=ctx_key)
            all_council_feedback.extend(fb_council)
            pending_reload = pending_reload or fb_reload
            # Split again
            user_fb = [r for r in fb_res if not any(k in r.lower() for k in error_keywords)]
            error_results = [r for r in fb_res if any(k in r.lower() for k in error_keywords)]
            if user_fb:
                fb_msg = "\n".join(user_fb)
                for chunk in split_message(sanitize(fb_msg)):
                    try:
                        await channel.send(chunk)
                    except Exception:
                        pass
            if fb_files:
                try:
                    await channel.send(None, files=fb_files)
                except Exception:
                    pass

        # ── Feed council responses back to Opus so it can continue ──
        if all_council_feedback and pp.alive:
            council_msg = "\n\n".join(all_council_feedback)
            log.info(f"Feeding council results back to Opus ({len(council_msg)} chars)")

            # loop: feed back → get response → execute actions → repeat
            for _council_round in range(10):  # safety cap
                council_result = await pp.send(council_msg)
                if not council_result or council_result.get("error"):
                    break
                c_text = council_result.get("text", "")
                if not c_text:
                    break

                c_text = process_memory_actions_module(c_text, ch_name, srv_name, guild_id)
                c_text = process_reminder_actions_module(c_text, channel_id, ch_name, message.author.id)
                c_cleaned, c_actions = extract_bot_actions_module(c_text)

                if c_cleaned.strip():
                    for chunk in split_message(sanitize(c_cleaned.strip())):
                        try:
                            await channel.send(chunk)
                        except Exception:
                            pass

                if not c_actions:
                    break

                c_res, c_reload, c_files, c_council = await execute_bot_actions_module(
                    c_actions, message, channel, guild_id, caller_ctx_key=ctx_key
                )
                pending_reload = pending_reload or c_reload
                if c_files:
                    try:
                        await channel.send(None, files=c_files)
                    except Exception:
                        pass

                # if there's more council feedback, continue the loop
                if c_council:
                    council_msg = "\n\n".join(c_council)
                else:
                    break

        if not text_already_sent and not user_results and not reply_files:
            if sent_text_len == 0:
                await message.reply(f"{prefix}*(empty response)*", mention_author=False)

        cost = result.get("cost_usd", 0)
        n_tools = len(result.get("tools", []))
        log.info(f"ctx={ctx_key} cost=${cost:.4f} tools={n_tools} actions={len(actions)}")

        # ── Usage footer ─────────────────────────────────────────
        total_tokens = result.get("total_tokens", 0)
        ctx_pct = context_percent(total_tokens)
        _last_token_usage[ctx_key] = {
            "input_tokens": result.get("input_tokens", 0),
            "cache_creation_tokens": result.get("cache_creation_tokens", 0),
            "cache_read_tokens": result.get("cache_read_tokens", 0),
            "total_tokens": total_tokens,
            "cost_usd": cost,
            "total_cost_usd": pp.total_cost,
        }
        if ctx_pct is not None:
            cache_hit = round((result.get("cache_read_tokens", 0) / total_tokens) * 100) if total_tokens else 0
            # format session runtime
            import time as _time
            elapsed = _time.time() - pp._created_at if pp._created_at else 0
            if elapsed >= 3600:
                h, rem = divmod(int(elapsed), 3600)
                m, s = divmod(rem, 60)
                runtime = f"{h}h {m}m {s}s"
            elif elapsed >= 60:
                m, s = divmod(int(elapsed), 60)
                runtime = f"{m}m {s}s"
            else:
                runtime = f"{int(elapsed)}s"
            footer = f"-# ctx {ctx_pct}% | {total_tokens:,} tokens ({cache_hit}% cached) | {runtime}"
            if ctx_pct >= 80:
                footer += "\n-# \u26a0 will autocompact soon!"
            try:
                await channel.send(footer)
            except Exception:
                pass
    finally:
        typing_task.cancel()
        cleanup_message_attachments(att_paths)
        _ctx_processing.discard(ctx_key)

    # ── Reload if requested by bot_action ──────────────────────
    if pending_reload:
        log.info("bot_action reload — restarting")
        await message.add_reaction("\u2705")
        await bridge.kill_all()
        await client.close()
        return

    # ── Notify if self-modified (reload is manual) ───────────
    if _self_modified():
        log.info("bot.py was modified during this run")
        await channel.send("bot.py was modified. Say `reload` to apply changes.")

    # ── Drain pending message (arrived during post-processing) ──
    pending_msg = _ctx_pending.pop(ctx_key, None)
    if pending_msg and pp.alive:
        log.info(f"Draining pending message for {ctx_key}: {pending_msg[:80]}")
        _ctx_processing.add(ctx_key)
        try:
            drain_result = await pp.send(pending_msg)
            if drain_result and not drain_result.get("error"):
                drain_text = drain_result.get("text", "")
                if drain_text:
                    drain_text = process_memory_actions_module(drain_text, ch_name, srv_name, guild_id)
                    drain_text = process_reminder_actions_module(drain_text, channel_id, ch_name, message.author.id)
                    drain_cleaned, _ = extract_bot_actions_module(drain_text)
                    if drain_cleaned.strip():
                        for chunk in split_message(sanitize(drain_cleaned.strip())):
                            try:
                                await channel.send(chunk)
                            except Exception:
                                pass
        except Exception:
            log.exception(f"Error draining pending message for {ctx_key}")
        finally:
            _ctx_processing.discard(ctx_key)


# ── Entry Point ──────────────────────────────────────────────────────────────

def main():
    if not DISCORD_TOKEN:
        print("Error: DISCORD_TOKEN not set in .env")
        print("Create a .env file with: DISCORD_TOKEN=your_discord_bot_token_here")
        raise SystemExit(1)

    try:
        r = subprocess.run(
            [CLAUDE_CMD, "--version"],
            capture_output=True, text=True, timeout=10,
            creationflags=CREATE_FLAGS,
        )
        print(f"Claude Code: {r.stdout.strip()}")
    except FileNotFoundError:
        print(f"'{CLAUDE_CMD}' not found. Is Claude Code installed and in PATH?")
        raise SystemExit(1)

    print("Starting claudebot...")
    client.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()



