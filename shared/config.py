"""Shared constants used by both Claude and Codex bots."""

import os
import subprocess
from pathlib import Path

DOCUMENTS_DIR = Path.home() / "Documents"
MAX_DISCORD_LEN = 1900
TYPING_INTERVAL = 8
OWNER_ID = 891221733326090250
CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROCESS_TIMEOUT = 600
