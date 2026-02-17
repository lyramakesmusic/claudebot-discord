# Claudebot Refactor Plan (v2)

> **This replaces the previous plan.** The old plan shuffled code into files but didn't rethink the design. This version is a proper architectural refactor — clean separation of concerns, not just file reorganization.

## Guiding Principles

1. **Separation of concerns.** Each module does ONE thing. The Claude bridge knows nothing about Discord. The Discord layer knows nothing about Claude. The bot glue wires them together.

2. **Don't rewrite from scratch.** This is a refactor, not a rewrite. Every line of existing behavior must be preserved. Move code, don't reinvent it. If a function works, keep it working — just put it in the right place.

3. **No file should be a monolith.** If something is over ~400 lines, it probably does too many things. Split by responsibility, not by arbitrary line count.

4. **Clean dependency direction.** `shared/` depends on nothing project-specific. `bridges/` depends on `shared/`. `integrations/` depends on `shared/`. `bots/` depends on everything. Nothing depends on `bots/`.

5. **Each commit must boot.** After every commit, both bots must start up and respond to messages. No "part 1 of 3" commits that leave things broken.

---

## Architecture Overview

```
User on Discord
      │
      ▼
┌─────────────────┐     ┌──────────────────┐
│  claude/bot.py   │     │  codex/bot.py     │   ◄── Thin glue: wires Discord ↔ Bridge
│  (the glue)      │     │  (the glue)       │       Handles on_message, delegates everything
└────┬───┬───┬─────┘     └────┬───┬──────────┘
     │   │   │                │   │
     ▼   │   ▼                ▼   │
┌────────┐│┌──────────┐ ┌────────┐│
│ Bridge ││ │ Discord  │ │ Bridge ││   ◄── Bridge: speaks JSON-RPC to CLI process
│ (Claude)││ │  Layer   │ │(Codex) ││       Discord Layer: message splitting, typing, attachments
└────────┘│└──────────┘ └────────┘│
          │                       │
          ▼                       ▼
   ┌─────────────────────────────────┐
   │         shared/                  │   ◄── State, config, utilities — used by everyone
   └─────────────────────────────────┘
          │
          ▼
   ┌─────────────────────────────────┐
   │      integrations/               │   ◄── Suno, Gemini, Council, Voice — each self-contained
   └─────────────────────────────────┘
```

---

## Target Directory Structure

```
claudebot/
    run.py                          # Supervisor entry point

    shared/                         # Zero project-specific knowledge
        __init__.py
        config.py                   # Constants, env loading, paths
        state.py                    # BotState class (unified, used by both bots)
        discord_utils.py            # split_message, sanitize, guild_slug, guild_docs_dir,
                                    #   typing helpers, attachment download/cleanup
        bot_actions.py              # Regex extraction of bot_action blocks from text
        hotreload.py                # Mtime checking, syntax validation

    claude/                         # Claude bot package
        __init__.py
        bot.py                      # GLUE ONLY — Discord client, on_ready, on_message dispatcher
                                    #   This wires bridge + discord + actions together. Thin as possible.
        bridge.py                   # ClaudeBridge, _PersistentProcess, _TurnState
                                    #   ONLY knows how to talk to Claude Code CLI via JSON-RPC.
                                    #   No Discord imports. No bot actions. Just send prompt → get response.
        actions.py                  # Bot action dispatcher — maps action dicts to side effects
                                    #   (create_project, upload, generate_image, join_voice, etc.)
        prompts.py                  # System prompt builders — build_system_context, build_thread_context
        memories.py                 # Memory CRUD — load, save, process ```memory``` blocks, format for prompt
        reminders.py                # Reminder CRUD + firing loop + Windows toast

    codex/                          # Codex bot package
        __init__.py
        bot.py                      # GLUE ONLY — same pattern as claude/bot.py
        bridge.py                   # CodexAppServer — JSON-RPC over stdio to codex app-server
                                    #   No Discord imports. Just send prompt → get response.
        actions.py                  # Bot action dispatcher (just upload + reload)
        prompts.py                  # System prompt builders

    integrations/                   # External services — each self-contained
        __init__.py
        council.py                  # GPT/Gemini research calls (already modular, just move)
        council_prompt.py           # Council system prompt (already modular, just move)
        suno.py                     # Suno music generation (already modular, just move)
        voice/                      # Voice pipeline — split the 2,008-line monolith
            __init__.py             # Exports VoiceManager
            manager.py              # VoiceManager — orchestrates the pipeline, handles join/leave/turns
            stt.py                  # UserSTTSession, WhisperSTTSession, MultiUserSTTManager
            tts.py                  # StreamingTTS, KokoroTTS
            turn_detection.py       # SmartTurnDetector, SmartTurnManager, TurnCoordinator
            audio.py                # AudioResampler, VoiceEngineSink, TTSAudioSource
            llm.py                  # OpenRouterVoiceLLM
            recv_patch.py           # Jitter buffer fix (was voice_recv_patch.py)

    selfbot/                        # Leave alone — it works, it's 912 lines, it's fine
        self.py

    supervisor/                     # Supervisor as a proper package
        __init__.py
        supervisor.py               # Main loop, .env watching
        process.py                  # BotProcess class — per-bot lifecycle, crash tracking,
                                    #   independent restart, graceful shutdown
        health.py                   # Heartbeat checking, stuck detection
        forensics.py                # Crash logging, log snapshots

    data/                           # Runtime data (all gitignored)
        state.json
        codex_state.json
        attachments/
        codex_attachments/
        generated_images/
        generated_music/

    logs/                           # All logs (gitignored)

    scripts/                        # Utility scripts (restart.ps1, kill_all.py, etc.)
```

---

## What Goes Where — The Key Decisions

### The Bridges (claude/bridge.py, codex/bridge.py)

These are the **most important files to get right.** Each bridge is a clean interface to an AI CLI:

- **Input:** a prompt string, a system prompt, a working directory
- **Output:** streamed text, tool call notifications, a final result dict
- **Callbacks:** `on_text(full_text_so_far)`, `on_tool(description_str)`
- **No Discord imports.** No `discord.Message`, no `discord.Channel`, no bot actions.
- **No side effects** beyond talking to the subprocess.

The bridge is testable in isolation — you could use it from a CLI script, a web server, anything. Discord is just one consumer.

`claude/bridge.py` contains: `_TurnState`, `_PersistentProcess`, `ClaudeBridge`, `tool_description()` helper.

`codex/bridge.py` contains: `_TurnState`, `CodexAppServer` (the JSON-RPC client).

### The Bot Glue (claude/bot.py, codex/bot.py)

These are **thin dispatchers.** They:
1. Set up the Discord client
2. On message: figure out context (guild, channel, project, cwd)
3. Build the system prompt
4. Call the bridge with the user's message
5. Stream the response back to Discord
6. Extract and execute bot actions
7. Handle error/retry loops

They import from everything else but contain minimal logic of their own. If `on_message` is still 250+ lines after extraction, that's fine — it's a dispatcher, not a monolith. The difference is it delegates everything instead of implementing it inline.

### Discord Utilities (shared/discord_utils.py)

Everything that's "Discord helper but not bot-specific":
- `split_message()` — break text into 2000-char chunks
- `sanitize()` — prevent @everyone/@here
- `is_guild_channel()` — check if channel is in a guild
- `guild_slug()` / `guild_docs_dir()` — filesystem paths from guild info
- `download_attachments()` / `cleanup_attachments()` — attachment handling with PDF extraction
- Typing indicator helper

### Bot Actions (claude/actions.py, codex/actions.py)

The action dispatcher maps `{"action": "...", ...}` dicts to side effects. Claude's version is big (15+ actions); Codex's is tiny (upload + reload). These are the right place for Discord-aware side effects — they receive a `discord.Message` and a `discord.Channel` and do things.

### Memories & Reminders (claude/memories.py, claude/reminders.py)

These are Claude-only features. Each is a self-contained CRUD module:
- Parse ```memory```/```reminder``` blocks from response text
- Read/write JSON files in selfbot/ directory
- Format current state for inclusion in system prompts
- Reminders module also has the background firing loop + Windows toast

### Integrations

These are already mostly modular — just move them and split voice.py:

- **council.py** — just move, it's clean
- **council_prompt.py** — just move
- **suno.py** — just move
- **voice/** — split the 2,008-line file into the package described above. Each class or closely-related group of classes gets its own file. `VoiceManager` in `manager.py` orchestrates them all. The `__init__.py` re-exports `VoiceManager` so existing `from integrations.voice import VoiceManager` still works.

### Supervisor

Currently a 177-line script. Expand into a proper package:
- `process.py` — `BotProcess` class that handles one bot's lifecycle (start, poll, restart, terminate). Each bot gets independent crash tracking and backoff.
- `health.py` — heartbeat file monitoring. Each bot writes a heartbeat; supervisor checks staleness.
- `forensics.py` — on crash, log to `logs/crashes.jsonl` and snapshot recent log lines.
- `supervisor.py` — main loop that owns the `BotProcess` instances and watches `.env`.

---

## How To Execute This

### Order of operations

1. **Create directory structure** — mkdir, `__init__.py` files. Don't move any code yet. Commit.

2. **Extract `shared/`** — Pull out the pure utilities (discord_utils, bot_actions, hotreload, config, state). Update imports in both bots. Both bots still run from root as before. Commit.

3. **Extract bridges** — Pull `_TurnState`, `_PersistentProcess`, `ClaudeBridge` into `claude/bridge.py`. Pull `_TurnState`, `CodexAppServer` into `codex/bridge.py`. Strip out any Discord imports from these — they should be pure. Update the root bot files to import from the new locations. Commit.

4. **Extract Claude-specific modules** — memories, reminders, prompts, actions, image gen, system stats, project seeding, research handler, context switching. Each one: move the code, update imports, verify bot still starts. Can be one commit or several.

5. **Extract Codex-specific modules** — prompts, actions. Same pattern. Commit.

6. **Move integration files** — council, suno into `integrations/`. Split voice.py into `integrations/voice/` package. Update imports. Commit.

7. **Slim down root bot files** — What's left in root `bot.py` and `codex_bot.py` becomes `claude/bot.py` and `codex/bot.py`. Root files become thin wrappers (`from claude.bot import main; main()`). Commit.

8. **Build supervisor package** — Expand run.py into `supervisor/`. Add BotProcess class, health checks, crash forensics, graceful shutdown. Update run.py to import from supervisor package. Commit.

9. **Move runtime files** — state.json → data/, logs → logs/, scripts → scripts/. Update paths. Update .gitignore. Commit.

10. **Cleanup** — delete old root files that were moved, update CLAUDE.md. Commit.

### Critical rules

- **After every commit, both bots must boot and respond.** No exceptions.
- **Don't rewrite logic.** Move code, adjust imports, fix references. The functions themselves should be identical to what exists today.
- **Don't simplify, optimize, or "improve" code while moving it.** That's a separate task. This refactor is purely structural.
- **Don't delete the root bot.py and codex_bot.py** — turn them into thin wrappers that import from the packages. The supervisor launches these root files.
- **Don't touch selfbot/self.py** — it works, leave it alone.
- **Test after each step** by running `python -c "from <module> import <thing>"` for each new module, then booting the bots.

### What "done" looks like

- No file over ~500 lines (except maybe the bot.py glue files, which are dispatchers)
- You can understand what any file does from its name alone
- The bridges are testable without Discord
- Adding a new integration doesn't require touching bot.py
- Adding a new bot action is one function in actions.py
- The supervisor handles crashes independently per bot
- Runtime files (logs, state, data) are organized, not scattered in root
