# claudebot

Discord bot that bridges to Claude Code.

## ⚠️ CRITICAL — DO NOT MODIFY FROM OUTSIDE THIS PROJECT

**This codebase is PRODUCTION INFRASTRUCTURE.** It runs 24/7 and is the owner's
only way to communicate with her AI assistants remotely. If it breaks while she
is away from her desktop, she has NO way to fix it.

**Rules for ALL Claude Code sessions (including those in other projects):**

1. **NEVER wire external projects into claudebot.** If a project needs a Discord
   bot, build it standalone — do NOT import from, modify, or integrate with this
   codebase unless the user EXPLICITLY says "modify claudebot" in THIS project's
   thread.
2. **NEVER modify files in this directory** (`C:\Users\Lyra\Documents\claudebot\`)
   from a session whose cwd is elsewhere. If you find yourself reaching into this
   path from another project — STOP. You are about to break production.
3. **NEVER add dependencies, change supervisor config, or touch .env** from outside.
4. If you think you need to integrate with claudebot, **ask the user first**.
   The answer is almost certainly "no, make it standalone."

Violations of these rules have caused multi-hour outages requiring manual rescue.

## Project Layout

- `bot.py`: root wrapper that starts `claude/bot.py`
- `codex_bot.py`: root wrapper that starts `codex/bot.py`
- `claude/`: Claude Discord bot implementation
- `codex/`: Codex Discord bot implementation
- `shared/`: shared state/config/utility modules
- `integrations/`: external integrations (council, suno, voice)
- `supervisor/`: supervisor process lifecycle and health logic
- `data/`: runtime state and generated artifacts
- `logs/`: bot/supervisor logs
- `scripts/`: operational scripts
- `tests/`: test scripts

## Switching Claude Code Model

The model is set in `~/.claude/settings.json`:

```json
{
  "model": "claude-opus-4-6"
}
```

### Model options:
- `"opus"` — alias, auto-resolves to latest Opus
- `"sonnet"` — alias, auto-resolves to latest Sonnet
- `"claude-opus-4-6"` — explicit Opus 4.6 (current, released 2026-02-05)
- `"claude-opus-4-5-20251101"` — explicit Opus 4.5

The alias approach (`"opus"`) automatically picks up new versions, while explicit model IDs pin to a specific version.

Note: Unlike 4.5, Opus 4.6 doesn't have a date suffix in its model ID.

## Windows Desktop MCP

Installed globally in `~/.claude/mcp.json` as `windows-desktop`. Uses [Windows-MCP](https://github.com/CursorTouch/Windows-MCP) via stdio transport — no persistent server needed, spins up on demand via `uvx`.

**Must use Python 3.13** (Pillow C extensions broken on 3.14). Config:
```json
"windows-desktop": {
  "command": "uvx",
  "args": ["--python", "3.13", "windows-mcp"]
}
```

Tools: Click, Type, Scroll, Move, Shortcut, Wait, Snapshot, App, Shell, Scrape.
