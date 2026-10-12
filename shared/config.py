"""Shared constants used by both Claude and Codex bots."""

import os
import subprocess
from pathlib import Path

DOCUMENTS_DIR = Path.home() / "Documents"
MAX_DISCORD_LEN = 1900
TYPING_INTERVAL = 8
PROJECT_ROOT_ = Path(__file__).resolve().parent.parent
try:  # make .env values visible no matter which module imports config first (never overrides existing env)
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT_ / ".env")
except ImportError:
    pass
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)   # set OWNER_ID=<your discord user id> in .env
CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROCESS_TIMEOUT = 600
