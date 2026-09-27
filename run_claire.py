#!/usr/bin/env python3
"""Launch Claire — a Claude Fable 5 bot instance, separate from claudebot."""

import os
import sys
from pathlib import Path

# ── Claire-specific overrides (set BEFORE importing bot) ─────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent

from shared.lockfile import acquire_or_exit

acquire_or_exit("claire")

os.environ["CLAUDE_MODEL"] = "claude-fable-5-1"
os.environ["STATE_FILE"] = str(PROJECT_ROOT / "data" / "claire" / "state.json")

# Load claire-specific .env (override=True so it wins over the base .env)
claire_env = PROJECT_ROOT / ".env.claire"
if claire_env.exists():
    from dotenv import load_dotenv
    load_dotenv(claire_env, override=True)

# Verify we have a token
if not os.environ.get("DISCORD_TOKEN"):
    print("ERROR: No DISCORD_TOKEN set.")
    print("Create .env.claire with DISCORD_TOKEN=...")
    sys.exit(1)

print(f"Starting Claire (Claude Fable 5.1)")
print(f"  Model: {os.environ['CLAUDE_MODEL']}")
print(f"  State: {os.environ['STATE_FILE']}")

# Run the main bot — env vars are set, so bot.py picks them up on import
from claude.bot import main
main()
