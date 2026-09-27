#!/usr/bin/env python3
"""Launch Opus 3 — a bare Claude 3 Opus instance. No tools, no memories, no system prompt."""

import os
import sys
from pathlib import Path

# ── Opus 3 overrides (set BEFORE importing bot) ──────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent

os.environ["CLAUDE_MODEL"] = "claude-3-opus-20240229"
os.environ["STATE_FILE"] = str(PROJECT_ROOT / "data" / "opus3" / "state.json")
os.environ["BARE_MODE"] = "1"  # disables system prompt, memories, tools in prompts.py

# Load opus3-specific .env (API key, workspace, discord token)
opus3_env = PROJECT_ROOT / ".env.opus3"
if opus3_env.exists():
    from dotenv import load_dotenv
    load_dotenv(opus3_env, override=True)

# Verify we have a token
if not os.environ.get("DISCORD_TOKEN"):
    print("ERROR: No DISCORD_TOKEN set.")
    print("Create .env.opus3 with DISCORD_TOKEN=...")
    sys.exit(1)

# Verify we have API credentials
if not os.environ.get("ANTHROPIC_API_KEY"):
    print("ERROR: No ANTHROPIC_API_KEY set.")
    print("Add ANTHROPIC_API_KEY to .env.opus3")
    sys.exit(1)

# Create state directory
(PROJECT_ROOT / "data" / "opus3").mkdir(parents=True, exist_ok=True)

print(f"Starting Opus 3 (bare mode)")
print(f"  Model: {os.environ['CLAUDE_MODEL']}")
print(f"  State: {os.environ['STATE_FILE']}")
print(f"  Bare:  {os.environ['BARE_MODE']}")
print(f"  Workspace: {os.environ.get('ANTHROPIC_CUSTOM_HEADERS', 'none')[:60]}...")

# Run the main bot
from claude.bot import main
main()
