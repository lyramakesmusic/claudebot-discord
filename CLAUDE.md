# claudebot

Discord bot that bridges to Claude Code.

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
