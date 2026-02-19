"""Bot action dispatcher for Claude bot."""

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

import discord

state = None
client = None
voice_manager = None
VoiceManager = None
_seed_project = None
_bg_generate_image = None
_system_stats = None
enqueue_music = None
call_gpt = None
call_researcher = None
split_message = None
sanitize = None
log = None
_BOT_FILE = None
PROJECT_ROOT = None
DOCUMENTS_DIR = Path.home() / "Documents"
CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
_council_gpt_history = None


def configure(
    *,
    state_obj,
    client_obj,
    voice_manager_obj,
    voice_manager_cls,
    seed_project_cb,
    bg_generate_image_cb,
    system_stats_fn,
    enqueue_music_fn,
    call_gpt_fn,
    call_researcher_fn,
    split_message_fn,
    sanitize_fn,
    logger,
    bot_file: Path,
    project_root: Path,
    documents_dir: Path,
    create_flags: int,
    council_gpt_history: dict,
):
    global state, client, voice_manager, VoiceManager, _seed_project, _bg_generate_image, _system_stats
    global enqueue_music, call_gpt, call_researcher, split_message, sanitize, log, _BOT_FILE
    global PROJECT_ROOT, DOCUMENTS_DIR, CREATE_FLAGS, _council_gpt_history
    state = state_obj
    client = client_obj
    voice_manager = voice_manager_obj
    VoiceManager = voice_manager_cls
    _seed_project = seed_project_cb
    _bg_generate_image = bg_generate_image_cb
    _system_stats = system_stats_fn
    enqueue_music = enqueue_music_fn
    call_gpt = call_gpt_fn
    call_researcher = call_researcher_fn
    split_message = split_message_fn
    sanitize = sanitize_fn
    log = logger
    _BOT_FILE = bot_file
    PROJECT_ROOT = project_root
    DOCUMENTS_DIR = documents_dir
    CREATE_FLAGS = create_flags
    _council_gpt_history = council_gpt_history

async def execute_bot_actions(
    actions: list[dict],
    message: discord.Message,
    channel: discord.abc.Messageable,
    guild_id: int = 0,
    caller_ctx_key: str = None,
) -> tuple[list[str], bool, list[discord.File], list[str]]:
    """Execute bot actions. Returns (status_messages, should_reload, files_to_attach, council_feedback)."""
    # resolve guild docs dir for project creation
    guild_config = state.get_guild_config(guild_id)
    guild_docs = Path(guild_config["docs_dir"]) if guild_config else DOCUMENTS_DIR

    results = []
    should_reload = False
    files_to_attach: list[discord.File] = []
    council_feedback: list[str] = []  # full responses to feed back to Opus
    for act in actions:
        action = act.get("action")

        if action == "create_project":
            name = act.get("name", "").strip()
            if not name:
                results.append("(project creation skipped — no name given)")
                continue
            name = re.sub(r"[^\w\-]", "-", name).strip("-")
            existing = state.get_project(name, guild_id)
            if existing:
                results.append(f"**{name}** already exists → <#{existing['thread_id']}>")
                continue
            folder = guild_docs / name
            folder.mkdir(parents=True, exist_ok=True)
            try:
                thread = await message.create_thread(
                    name=name, auto_archive_duration=10080
                )
                state.set_project(name, str(folder), thread.id, guild_id)
                results.append(
                    f"Created project **{name}** → <#{thread.id}>\n"
                    f"Folder: `{folder}`"
                )

                # if a seed message was provided, kick off the project session
                seed_msg = act.get("message", "").strip()
                if seed_msg:
                    asyncio.create_task(
                        _seed_project(thread, name, str(folder), seed_msg, guild_id)
                    )

            except Exception as e:
                results.append(f"Failed to create project thread: {e}")

        elif action == "system_stats":
            stats = await asyncio.get_event_loop().run_in_executor(None, _system_stats)
            results.append(stats)

        elif action == "reload":
            # validate syntax before reloading
            try:
                r = subprocess.run(
                    [sys.executable, "-c",
                     f"compile(open(r'{_BOT_FILE}', encoding='utf-8').read(), 'bot.py', 'exec')"],
                    capture_output=True, text=True, timeout=10,
                    creationflags=CREATE_FLAGS,
                )
                if r.returncode != 0:
                    err = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "unknown"
                    results.append(f"Reload aborted — bad syntax:\n```\n{err[:500]}\n```")
                else:
                    should_reload = True
            except Exception as e:
                results.append(f"Reload validation failed: {e}")

        elif action == "full_restart":
            # nuclear option: kill supervisor + all bots, relaunch from scratch
            restart_script = PROJECT_ROOT / "scripts" / "restart.ps1"
            if not restart_script.exists():
                results.append("restart.ps1 not found")
                continue
            try:
                await channel.send("Full restart in progress...")
                subprocess.Popen(
                    ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(restart_script)],
                    creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
                    close_fds=True,
                )
                # we'll be dead in a moment
                await asyncio.sleep(1)
                os._exit(0)
            except Exception as e:
                results.append(f"Full restart failed: {e}")

        elif action == "upload":
            file_path = act.get("path", "").strip()
            caption = act.get("caption", "").strip()
            if not file_path:
                results.append("(upload skipped — no path given)")
                continue
            p = Path(file_path).expanduser()
            if not p.exists():
                results.append(f"Upload failed — file not found: `{file_path}`")
                continue
            size_mb = p.stat().st_size / (1024 * 1024)
            if size_mb > 500:
                results.append(f"Upload failed — file too large ({size_mb:.1f} MB, max 500 MB)")
                continue
            try:
                f = discord.File(str(p), filename=p.name)
                await channel.send(caption or None, file=f)
                log.info(f"Uploaded {p.name} ({size_mb:.1f} MB)")
            except Exception as e:
                results.append(f"Upload failed: {e}")

        elif action == "generate_image":
            prompt = act.get("prompt", "").strip()
            if not prompt:
                results.append("(image generation skipped — no prompt given)")
                continue
            ref_images = act.get("reference_images", [])
            caption = act.get("caption", "").strip()
            log.info(f"Generating image (async): {prompt[:80]}")
            asyncio.create_task(_bg_generate_image(channel, prompt, ref_images or None, caption, message.author.id))

        elif action == "generate_music":
            style = act.get("style", "").strip()
            if not style:
                results.append("(music generation skipped — no style given)")
                continue
            lyrics = act.get("lyrics", "").strip()
            title = act.get("title", "").strip()
            log.info(f"Generating music (async): style={style[:60]} lyrics={bool(lyrics)}")
            enqueue_music(channel, style, lyrics, title)

        elif action == "join_voice":
            channel_ref = act.get("channel", "").strip()
            if not channel_ref:
                results.append("(join_voice skipped — no channel given)")
                continue
            log.info(f"[join_voice] VoiceManager={VoiceManager}, voice_manager={voice_manager}, running={getattr(voice_manager, '_running', '?')}")
            if not voice_manager or not voice_manager._running:
                if VoiceManager is None:
                    reason = "voice deps not installed"
                elif not voice_manager:
                    reason = "on_ready hasn't run yet"
                else:
                    reason = "pipeline failed to start (check logs)"
                results.append(f"Voice not available: {reason}")
                continue
            vc = await _resolve_voice_channel(channel_ref, message.guild)
            if not vc:
                available = [ch.name for ch in message.guild.voice_channels] if message.guild else []
                results.append(f"Could not find voice channel: {channel_ref}. Available: {', '.join(available) or 'none'}")
                continue
            await voice_manager.join_channel(vc, caller_ctx_key=caller_ctx_key)
            results.append(f"Joined voice channel **{vc.name}**")

        elif action == "leave_voice":
            if not voice_manager:
                reason = "VoiceManager import failed" if VoiceManager is None else "on_ready hasn't run yet"
                results.append(f"Voice not available: {reason}")
                continue
            if voice_manager.voice_client and voice_manager.voice_client.is_connected():
                ch_name = voice_manager.voice_client.channel.name if voice_manager.voice_client.channel else "?"
                await voice_manager._leave_channel()
                results.append(f"Left voice channel **{ch_name}**")
            else:
                results.append("Not currently in a voice channel")

        elif action == "play_audio":
            path = act.get("path", "").strip()
            volume = float(act.get("volume", 1.0))
            if not path:
                results.append("play_audio: no path given")
                continue
            if not voice_manager or not voice_manager.voice_client or not voice_manager.voice_client.is_connected():
                results.append("play_audio: not in a voice channel")
                continue
            result_msg = await voice_manager.play_file(path, volume=volume)
            results.append(result_msg)

        elif action == "play_url":
            url = act.get("url", "").strip()
            volume = float(act.get("volume", 0.5))
            if not url:
                results.append("play_url: no URL given")
                continue
            if not voice_manager or not voice_manager.voice_client or not voice_manager.voice_client.is_connected():
                results.append("play_url: not in a voice channel")
                continue
            result_msg = await voice_manager.play_url(url, volume=volume)
            results.append(result_msg)

        elif action == "stop_audio":
            if voice_manager:
                await voice_manager.stop_playback()
                results.append("Playback stopped")
            else:
                results.append("Voice not available")

        elif action == "switch_voice":
            voice_name = act.get("voice", "").strip()
            if not voice_name:
                results.append("switch_voice: no voice name given")
                continue
            if not voice_manager:
                results.append("Voice not available")
                continue
            result_msg = await voice_manager.switch_voice(voice_name)
            results.append(result_msg)

        elif action == "call_gpt":
            gpt_msg = act.get("message", "").strip()
            if not gpt_msg:
                results.append("call_gpt: no message given")
                continue
            # post Opus's message to the thread
            for chunk in split_message(f"**[to GPT]** {gpt_msg}"):
                try:
                    await channel.send(chunk)
                except Exception:
                    pass
            # build/extend conversation history for this thread
            ch_id = channel.id
            if ch_id not in _council_gpt_history:
                _council_gpt_history[ch_id] = []
            _council_gpt_history[ch_id].append({"role": "user", "content": gpt_msg})
            # call GPT
            gpt_result = await call_gpt(_council_gpt_history[ch_id])
            if gpt_result["error"]:
                await channel.send(f"**[GPT error]** {gpt_result['error'][:500]}")
                results.append(f"GPT error: {gpt_result['error'][:200]}")
            else:
                gpt_content = gpt_result["content"]
                _council_gpt_history[ch_id].append({"role": "assistant", "content": gpt_content})
                cost_note = f"\n-# Cost: ${gpt_result['cost']:.4f}" if gpt_result["cost"] else ""
                for chunk in split_message(f"**[GPT to Opus]** {gpt_content}{cost_note}"):
                    try:
                        await channel.send(chunk)
                    except Exception:
                        pass
                results.append(f"[GPT responded — {len(gpt_content)} chars]")
                council_feedback.append(f"[GPT to Opus] {gpt_content}")

        elif action == "call_researcher":
            query = act.get("query", "").strip()
            if not query:
                results.append("call_researcher: no query given")
                continue
            context = act.get("context", "").strip()
            # post the research request
            await channel.send(f"**[Research query]** {query[:500]}")
            # call Gemini
            research_result = await call_researcher(query, context)
            if research_result["error"]:
                await channel.send(f"**[Research error]** {research_result['error'][:500]}")
                results.append(f"Research error: {research_result['error'][:200]}")
            else:
                research_content = research_result["content"]
                cost_note = f"\n-# Cost: ${research_result['cost']:.4f}" if research_result["cost"] else ""
                for chunk in split_message(f"**[Gemini — deep research]** {research_content}{cost_note}"):
                    try:
                        await channel.send(chunk)
                    except Exception:
                        pass
                results.append(f"[Research complete — {len(research_content)} chars]")
                council_feedback.append(f"[Gemini — deep research] {research_content}")

        else:
            log.warning(f"Unknown bot_action: {action}")

    return results, should_reload, files_to_attach, council_feedback


async def _resolve_voice_channel(ref: str, guild: discord.Guild) -> discord.VoiceChannel | None:
    """Resolve a voice channel from an ID, name, or Discord URL."""
    if not guild:
        return None
    # Try as raw channel ID
    try:
        ch_id = int(ref)
        ch = guild.get_channel(ch_id)
        if isinstance(ch, discord.VoiceChannel):
            return ch
        # Try fetching from any guild the bot is in
        for g in client.guilds:
            ch = g.get_channel(ch_id)
            if isinstance(ch, discord.VoiceChannel):
                return ch
    except ValueError:
        pass
    # Try extracting channel ID from Discord URL
    url_match = re.search(r"/channels/\d+/(\d+)", ref)
    if url_match:
        try:
            ch_id = int(url_match.group(1))
            for g in client.guilds:
                ch = g.get_channel(ch_id)
                if isinstance(ch, discord.VoiceChannel):
                    return ch
        except ValueError:
            pass
    # Try matching by name (case-insensitive)
    ref_lower = ref.lower()
    for ch in guild.voice_channels:
        if ch.name.lower() == ref_lower:
            return ch
    # Fuzzy: check if ref is contained in channel name
    for ch in guild.voice_channels:
        if ref_lower in ch.name.lower():
            return ch
    return None

