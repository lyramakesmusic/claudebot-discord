#!/usr/bin/env python3
"""Run Opal with Clod's harness and a separate Discord identity/session store."""

import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

from shared.lockfile import acquire_or_exit

PROJECT_ROOT = Path(__file__).resolve().parent


def main():
    config = dotenv_values(PROJECT_ROOT / ".env.opal")
    token = (config.get("DISCORD_TOKEN") or "").strip()
    if not token:
        raise SystemExit("Opal requires DISCORD_TOKEN in .env.opal")

    acquire_or_exit("opal")
    load_dotenv(PROJECT_ROOT / ".env")
    load_dotenv(PROJECT_ROOT / ".env.opal", override=True)
    os.environ["DISCORD_TOKEN"] = token
    os.environ["CLAUDE_MODEL"] = config.get("CLAUDE_MODEL") or "claude-opus-5-5"
    os.environ["STATE_FILE"] = str(PROJECT_ROOT / "data" / "opal" / "state.json")
    os.environ.pop("BARE_MODE", None)
    (PROJECT_ROOT / "data" / "opal").mkdir(parents=True, exist_ok=True)

    from claude.bot import main as run_bot
    run_bot()


if __name__ == "__main__":
    main()
