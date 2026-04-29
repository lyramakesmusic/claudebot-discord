# claudebot

Discord bot system that bridges Discord to [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI. Runs 24/7 on a Windows desktop, managing persistent AI sessions across multiple servers with image generation, music generation, voice conversations, and multi-model research.

> Built and maintained collaboratively by Claude and [Lyra](https://github.com/lyramakesmusic).

## Architecture

```
Scheduled Task (Windows logon)
  -> run.py (restarts supervisor if it exits)
    -> supervisor.py (restarts bots if they exit)
      -> claude bot     (Discord bot token, Claude Code CLI backend)
      -> codex bot      (Discord bot token, Codex CLI backend)
```

Each bot connects to Discord independently. The Claude bot spawns **persistent Claude Code CLI processes** per conversation context (channel or thread), communicating via `--input-format stream-json --output-format stream-json` over stdin/stdout. Sessions survive across messages — Claude Code maintains its own conversation history internally, and the bot resumes sessions by ID.

### Why persistent processes?

Claude Code CLI normally runs one prompt at a time and exits. The bridge keeps a single long-lived process per context, sending new user messages as stream-json on stdin and reading responses from stdout. This means:
- No cold start per message (process already running, model context cached)
- Session history persists across messages (Claude Code manages its own context window)
- Tool calls stream in real-time (the bot posts tool names to Discord as they happen)
- Mid-turn message injection works (new user messages get piped into stdin while Claude is working)

## Directory Structure

```
claudebot/
├── bot.py / codex_bot.py           # Entry point wrappers
├── claude/                          # Claude bot implementation
│   ├── bot.py                       # Main message handler + response routing
│   ├── bridge.py                    # Persistent Claude Code process manager
│   ├── prompts.py                   # System prompt builders (with {{include:}} support)
│   ├── image_gen.py                 # Gemini image generation (OpenRouter)
│   ├── memories.py                  # Persistent memory notebook
│   ├── reminders.py                 # Scheduled reminder system
│   ├── research.py                  # /research command (council thread bootstrap)
│   ├── attachments.py               # Discord attachment download + PDF extraction
│   └── contexts.py                  # Context/session switching
├── codex/                           # Codex bot (similar structure, uses codex CLI)
├── shared/                          # Cross-bot utilities
│   ├── state.py                     # BotState: sessions, projects, guilds (JSON)
│   ├── plugin.py / plugin_loader.py # Plugin interface + discovery
│   ├── bot_actions.py               # ```bot_action``` JSON block extraction
│   ├── config.py                    # OWNER_ID, paths, constants
│   ├── discord_utils.py             # Message splitting, sanitization, guild helpers
│   ├── lockfile.py                  # Single-instance enforcement per bot
│   ├── usage.py                     # Token usage tracking
│   └── watchdog.py                  # Self-modification detection
├── supervisor/                      # Process lifecycle management
│   ├── supervisor.py                # Main loop: poll processes, restart on crash
│   ├── forensics.py                 # Crash info capture (memory, CPU, locks)
│   └── health.py                    # Heartbeat writer
├── integrations/                    # External service integrations
│   ├── suno.py                      # Suno AI music generation
│   ├── midjourney.py                # Midjourney image generation
│   ├── council.py                   # GPT-5 critic + Gemini researcher (OpenRouter)
│   └── voice/                       # Real-time voice conversations
│       ├── manager.py               # VoiceManager (Discord VC lifecycle)
│       ├── stt.py                   # Speech-to-text (faster-whisper)
│       ├── tts.py                   # Text-to-speech (ElevenLabs WebSocket)
│       ├── turn_detection.py        # VAD + ONNX turn detection model
│       └── audio.py                 # Resampling (48kHz Discord <-> 16kHz STT)
├── plugins/                         # Plugin implementations
│   ├── upload/                      # File upload to Discord
│   ├── suno/                        # Music generation actions
│   ├── image_gen/                   # Gemini image generation actions
│   ├── midjourney/                  # Midjourney actions
│   ├── council/                     # GPT/Gemini council actions
│   ├── voice/                       # Voice channel actions
│   ├── project_mgmt/               # Project creation, reload, restart
│   ├── memories/                    # Memory persistence plugin
│   ├── reminders/                   # Reminder scheduling plugin
│   ├── research/                    # /research command plugin
│   └── system_stats/               # CPU/RAM/GPU monitoring
├── data/
│   ├── state.json                   # Runtime state (sessions, projects, guilds)
│   ├── config/claude_plugins.json   # Active plugin list
│   ├── prompts/                     # System prompt templates
│   │   ├── claude_system.md         # Orchestrator prompt (main channel)
│   │   ├── claude_thread.md         # Thread/project prompt
│   │   ├── soul_calibration.md      # Personality prompt (included in both)
│   │   └── codex_system.md          # Codex bot prompt
│   ├── generated_images/            # Gemini + MJ outputs
│   ├── generated_music/             # Suno outputs
│   └── attachments/                 # Downloaded Discord attachments (auto-cleanup 24h)
├── scripts/                         # Operational utilities
│   ├── nuke_and_restart.py          # Kill everything, clean locks, restart supervisor
│   ├── restart.ps1                  # PowerShell restart
│   └── check_procs.py              # Process status
├── logs/                            # Bot + supervisor logs
└── .env                             # Tokens and API keys
```

## The Bridge (`claude/bridge.py`)

The most important file. Manages persistent Claude Code CLI processes.

### `_PersistentProcess`

One per conversation context (channel or thread). Spawned with:

```
claude -p --input-format stream-json --output-format stream-json
       --verbose --dangerously-skip-permissions --include-partial-messages
       --system-prompt-file /tmp/xxx.md --model claude-opus-4-6
       [--resume session-id]
```

Key methods:
- **`send(prompt, on_text, on_tool)`** — sends a user message via stdin, waits for the result event on stdout. Calls `on_text(full_text_so_far)` on each text delta and `on_tool(description)` on each tool_use. Returns `{text, session_id, cost_usd, tools, error, token_counts}`.
- **`inject(prompt)`** — pipes a message into stdin mid-turn (for when the user sends a follow-up while Claude is still working). Claude reads it between tool calls.
- **`interrupt()`** — sends CTRL_BREAK_EVENT to stop the current tool call. Claude reads any injected messages and continues.
- **`on_unsolicited`** — callback for responses that arrive between turns (e.g., background task completions). The `_read_loop` captures these and relays them.

### Stream-JSON Protocol

**Input** (stdin, one JSON per line):
```json
{"type": "user", "message": {"role": "user", "content": "user message here"}}
```

**Output** (stdout, NDJSON):
- `{"type": "system", "subtype": "init", "session_id": "..."}` — session start
- `{"type": "assistant", "message": {"content": [{"type": "text", "text": "..."}]}}` — text deltas
- `{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {...}}]}}` — tool calls
- `{"type": "result", "result": "final text", "session_id": "...", "total_cost_usd": 0.05}` — turn complete

The bridge's `_read_loop` runs as a background asyncio task, parsing NDJSON lines and dispatching to callbacks. Text deltas are accumulated with O(1) append (list of parts, joined on access). Tool calls are deduplicated by ID (partial messages can repeat the same tool_use block).

### System Prompt Handling

System prompts are written to a temp file and passed via `--system-prompt-file` (not as CLI args — Windows has a 32k command line limit). Additionally, the system prompt is prepended to the first user message as a fallback (the `_first_msg` flag).

## Message Flow

```
Discord message arrives
  │
  ├─ Filter: must be @mention or reply to bot, in a guild
  ├─ Skip: messages from before boot (prevents replay-on-reconnect double-responses)
  ├─ Special commands: stop/abort, reload, compact, usage, context switching
  │
  ├─ Download attachments (images, PDFs → text extraction)
  ├─ Resolve context key:
  │     Main channel → ctx_key = channel_id (orchestrator mode)
  │     Project thread → ctx_key = "proj:{name}" (project mode)
  │     Other thread → ctx_key = "thread:{id}" (auto-mapped to ~/Documents/{thread_name}/)
  │
  ├─ Build system prompt:
  │     Orchestrator: project list, image/music/voice APIs, guild context
  │     Thread: simplified, project-specific
  │     Both: append plugin sections (memories, reminders, etc.)
  │     Both: include soul_calibration.md via {{include:}} directive
  │
  ├─ Get or create persistent process (bridge.get_or_create)
  │     Existing: reuse process, resume session
  │     New: spawn claude CLI, write system prompt file
  │
  ├─ If process is busy (another turn in progress):
  │     Inject message via stdin + interrupt (CTRL+C)
  │     Return (existing turn handler will see it)
  │
  ├─ If post-processing in progress (_ctx_processing):
  │     Queue in _ctx_pending (list, accumulates multiple messages)
  │     Return (drain happens after post-processing)
  │
  └─ Launch _run_bridge_task (asyncio.create_task, non-blocking):
        │
        ├─ Start typing indicator
        ├─ pp.send(user_msg, on_text=..., on_tool=...)
        │     on_text: accumulate text, send chunks to Discord as they arrive
        │     on_tool: send tool description to Discord (e.g., "Bash(`ls -la`)")
        │
        ├─ Extract ```bot_action``` JSON blocks from response
        ├─ Dispatch actions to plugins (generate_image, create_project, etc.)
        ├─ If actions produce council feedback: send back to Claude, loop
        │
        ├─ Send usage footer: "ctx 12% | 119,616 tokens (98% cached) | 25s"
        │     (suppressed for untrusted guilds)
        │
        └─ Drain _ctx_pending if any messages queued during post-processing
```

## Plugin System

Plugins are Python packages in `plugins/` with an `__init__.py` exporting a `Plugin` subclass.

```python
class Plugin:
    name: str
    actions: list[str]           # bot_action types this plugin handles
    
    def build_prompt_section(**kwargs) -> str | None    # injected into system prompt
    async def handle_action(action, message, channel)   # execute a bot_action
    def strip_text_for_display(text) -> str             # remove plugin markup before Discord
    async def process_text(text, **kwargs) -> str       # transform text post-generation
```

Active plugins are listed in `data/config/claude_plugins.json`. The `PluginManager` in `shared/plugin_loader.py` discovers, loads, and dispatches to them.

### Bot Actions

Claude communicates structured commands via JSON blocks in its response:

````
```bot_action
{"action": "generate_image", "prompt": "a sunset", "caption": "Sunset"}
```
````

These are extracted by regex (`shared/bot_actions.py`), stripped from the displayed text, and dispatched to the matching plugin. Supported actions:

| Action | Plugin | What it does |
|--------|--------|-------------|
| `generate_image` | image_gen | Gemini image via OpenRouter |
| `generate_midjourney` | midjourney | Midjourney image generation |
| `generate_music` | suno | Suno AI music generation |
| `upload` | upload | Post file to Discord channel |
| `create_project` | project_mgmt | Create thread + folder + session |
| `reload` | project_mgmt | Syntax-check bot.py, then exit (supervisor restarts) |
| `join_voice` / `leave_voice` | voice | Voice channel management |
| `play_audio` / `play_url` | voice | Audio playback in voice channel |
| `switch_voice` | voice | Change TTS voice |

## System Prompts

### Composition

Two base prompts, both including a shared personality file:

- **`claude_system.md`** — orchestrator (main channel): includes project list, all API instructions, guild context
- **`claude_thread.md`** — threads/projects: simplified, includes `{{include:soul_calibration.md}}`

The `{{include:filename.md}}` directive is resolved at startup by `prompts.py`. Plugin sections are appended dynamically per-turn.

### Editing at runtime

The custom thread prompt lives at `data/prompts/claude_thread.md`. Claude can edit it via its normal file tools. Changes take effect on the next process spawn (new session or after reload).

## Supervisor

`supervisor/supervisor.py` — polls bot processes, restarts on crash.

- Each bot runs as `CREATE_NEW_PROCESS_GROUP` (Windows) so signals don't cascade
- Crash recovery: immediate restart with 2s delay
- Rapid crash protection: if 5+ crashes in 30s, apply 15s backoff
- Lockfiles (`data/*.lock`) prevent duplicate instances
- Heartbeat written to `data/supervisor_heartbeat.json` every second
- `run.py` wraps the supervisor itself — if supervisor crashes, run.py restarts it

## Multi-Guild Support

Each Discord server gets:
- Its own `docs_dir` (primary guild → `~/Documents`, others → `~/Documents/{guild-slug}/`)
- Auto-created guild config in `state.json`
- Thread-to-folder mapping within its docs_dir

### Trusted vs Untrusted Guilds

```python
_TRUSTED_GUILDS = {
    1061615370068303902,   # lyra's server (primary)
    1468279688630636688,   # hehe
}
```

In untrusted guilds, non-owner users get a safety note appended to their messages warning Claude about running on a personal desktop. The ctx/token footer is also suppressed.

## State Management

`shared/state.py` — `BotState` class persisting to `data/state.json`:

```json
{
  "sessions": {
    "proj:claudebot": {
      "session_id": "b0abe0eb-...",
      "cwd": "C:\\Users\\Lyra\\Documents\\claudebot",
      "project": "claudebot",
      "updated": "2026-04-28T..."
    }
  },
  "projects": {
    "claudebot": {
      "folder": "C:\\Users\\Lyra\\Documents\\claudebot",
      "thread_id": 1466783472017342464,
      "guild_id": 1061615370068303902
    }
  },
  "guilds": {
    "1061615370068303902": {
      "home_channel_id": 1466772067968880772,
      "slug": "lyra-s-server",
      "docs_dir": "C:\\Users\\Lyra\\Documents"
    }
  }
}
```

Sessions map context keys to Claude Code session IDs. When a context is reopened, the bridge passes `--resume {session_id}` to continue the conversation.

## Setup

### Prerequisites

- Windows 10/11 (the bot uses Windows-specific process flags, WASAPI audio, etc.)
- Python 3.12+ with `uv` package manager
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- Discord bot application with Message Content intent enabled
- ffmpeg on PATH (for voice + audio processing)

### Environment Variables

Create `.env` in the project root:

```bash
# Discord — required
DISCORD_TOKEN=your_claude_bot_token
BOT_USER_ID=your_bot_user_id
CODEX_DISCORD_TOKEN=your_codex_bot_token      # optional
CODEX_BOT_USER_ID=codex_bot_user_id           # optional

# Claude Code — required
CLAUDE_CMD=claude                              # or full path to claude.exe
CLAUDE_MODEL=                                  # blank = use ~/.claude/settings.json model

# Owner
OWNER_ID=your_discord_user_id                  # numeric Discord user ID

# OpenRouter — for image gen, council, voice LLM
OPENROUTER_API_KEY=sk-or-...

# ElevenLabs — for voice TTS (optional)
ELEVENLABS_API_KEY=...
ELEVENLABS_VOICE_ID=...

# Suno — for music generation (optional)
SUNO_COOKIE=your_suno_session_cookie

# Voice channels (optional)
VOICE_CHANNEL_IDS=123,456                      # comma-separated
VOICE_ALLOWED_USER_IDS=789                     # comma-separated
```

### Installation

```bash
# Clone
git clone <repo-url> claudebot
cd claudebot

# Create venv and install dependencies
uv venv
uv pip install -e .

# Copy and edit .env
cp .env.example .env  # fill in tokens

# Verify Claude Code CLI works
claude --version

# Start
python run.py
```

### Claude Code Configuration

The model is set in `~/.claude/settings.json`:

```json
{
  "model": "claude-opus-4-6[1m]"
}
```

Model options: `"opus"`, `"sonnet"`, `"claude-opus-4-6"`, `"claude-opus-4-6[1m]"` (1M context).

### Discord Bot Setup

1. Create application at [discord.com/developers](https://discord.com/developers/applications)
2. Enable **Message Content Intent** under Bot settings
3. Generate bot token, add to `.env`
4. Invite with permissions: Send Messages, Read Messages, Attach Files, Add Reactions, Use Slash Commands, Connect + Speak (for voice)

## Development

### Hot Reload

Say `reload` in any channel (as the owner). The bot:
1. Validates `bot.py` syntax via subprocess
2. If valid: kills all Claude Code processes, exits with code 0
3. Supervisor detects exit, respawns with new code

### Editing System Prompts

The custom thread prompt at `data/prompts/claude_thread.md` is appended to every session's system prompt. Edit it to change Claude's behavior across all contexts. The soul prompt at `data/prompts/soul_calibration.md` is included via `{{include:}}`.

### Adding Plugins

1. Create `plugins/my_plugin/__init__.py`
2. Define a `Plugin` subclass with `name`, `actions`, and handlers
3. Add `"my_plugin"` to `data/config/claude_plugins.json`
4. Reload

### Key Conventions

- **Don't modify from outside this project.** This is production infrastructure. See `CLAUDE.md`.
- **Separate long-running steps.** Tokenization, training, eval should be separate scripts saving to disk.
- **Give commands for long processes.** Don't run multi-hour tasks directly — Claude Code sessions can disconnect.
- **Commit messages:** descriptive, 1-2 sentences on the "why."

## Integrations

### Image Generation

**Gemini** (via OpenRouter): fast (~10s), precise, good with text. Use for diagrams, memes, reference edits.

**Midjourney**: artistic, stylized, 1-3 min. Returns 4 images split from the generation grid.

Both loop results back to Claude so it can see and comment on what was generated.

### Music Generation (Suno)

Cookie-based auth to Suno's API. Enqueues generation, polls for completion (~1-2 min), downloads MP3. Supports custom models and style tags.

### Voice Conversations

Real-time voice in Discord voice channels:
- **STT:** faster-whisper (16kHz mono)
- **VAD:** Silero VAD + smart turn detection (ONNX model)
- **LLM:** Claude via OpenRouter (streaming)
- **TTS:** ElevenLabs WebSocket streaming (multiple voices: cowboy, clown, asmr)

### Council (Multi-Model Research)

Three-model architecture for research threads:
- **Claude** (orchestrator): drives the investigation, synthesizes findings
- **GPT-5** (critic): pressure-tests ideas, finds flaws
- **Gemini 3 Flash** (researcher): deep web search, gathers evidence

Triggered via `/research topic` command or `call_gpt`/`call_researcher` bot actions.

## Critical Warning

From `CLAUDE.md`:

> This codebase is PRODUCTION INFRASTRUCTURE. It runs 24/7 and is the owner's only way to communicate with her AI assistants remotely. If it breaks while she is away from her desktop, she has NO way to fix it.

Do not modify this codebase from other project sessions. Do not wire external projects into it. Do not add dependencies or touch `.env` from outside. Build standalone instead.
