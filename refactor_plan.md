# Claudebot Refactor Plan

## Goal

Split the monolithic bot.py (2,652 lines) and codex_bot.py (1,133 lines) into focused modules. Deduplicate ~200 lines of shared code. Organize runtime files (logs, state, generated content) into proper directories. Harden the supervisor.

Each phase is a **separate git commit** so any phase can be reverted independently. After each phase, both bots must still boot and respond to messages.

---

## Target Directory Structure

```
claudebot/
    run.py                          # Hardened supervisor (entry point)
    CLAUDE.md
    pyproject.toml
    .env / .gitignore / .python-version

    shared/                         # Code used by both bots
        __init__.py
        config.py                   # Shared constants
        state.py                    # Unified BotState class
        discord_utils.py            # Message splitting, sanitize, guild helpers
        bot_actions.py              # Bot action regex + extraction
        attachments.py              # Attachment download/cleanup with PDF support
        hotreload.py                # Hot reload detection + syntax validation
        logging_setup.py            # Logging factory

    claude/                         # Claude Code bot
        __init__.py
        bot.py                      # Slim entry point (~350 lines)
        bridge.py                   # _TurnState, _PersistentProcess, ClaudeBridge
        prompts.py                  # System prompt builders
        actions.py                  # Bot action dispatcher (15+ actions)
        memories.py                 # Memory system
        reminders.py                # Reminder system + toast notifications
        image_gen.py                # Gemini image generation
        system_stats.py             # CPU/RAM/GPU stats
        project_seeding.py          # New project seeding
        research.py                 # /research command handler
        context_switching.py        # .new-context, .list-contexts, .resume-context

    codex/                          # Codex CLI bot
        __init__.py
        bot.py                      # Slim entry point (~250 lines)
        bridge.py                   # _TurnState, CodexAppServer
        prompts.py                  # System prompt builders
        actions.py                  # Bot action dispatcher (upload + reload)

    integrations/                   # External service integrations
        __init__.py
        council.py                  # GPT/Gemini research calls
        council_prompt.py           # Council Opus system prompt
        suno.py                     # Suno music generation
        voice.py                    # Voice pipeline
        voice_recv_patch.py         # Jitter buffer fix

    selfbot/                        # Unchanged — do not touch
        self.py

    data/                           # Runtime data (gitignored)
        state.json
        codex_state.json
        attachments/
        codex_attachments/
        generated_images/
        generated_music/

    logs/                           # All logs (gitignored)
        claudebot.log
        codexbot.log
        selfbot.log
        supervisor.log
        crashes.jsonl

    scripts/                        # Utility scripts
        restart.ps1
        kill_bots.ps1
        kill_all.py
        install_task.py

    models/                         # ML models (gitignored)
        smart-turn-v3.2-cpu.onnx
```

---

## Phase 0: Create Directory Structure

**No behavior changes.** Just create directories and empty `__init__.py` files.

### Steps

1. Create directories:
   ```
   mkdir -p shared claude codex integrations data logs scripts tests
   ```

2. Create empty `__init__.py` in each package directory:
   - `shared/__init__.py`
   - `claude/__init__.py`
   - `codex/__init__.py`
   - `integrations/__init__.py`

3. Create `shared/config.py` with constants extracted from both bots:

```python
"""Shared constants used by both Claude and Codex bots."""

import os
import subprocess
from pathlib import Path

DOCUMENTS_DIR = Path.home() / "Documents"
MAX_DISCORD_LEN = 1900
TYPING_INTERVAL = 8        # seconds between typing indicator refreshes
OWNER_ID = 891221733326090250  # Lyra's Discord user ID
CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
PROJECT_ROOT = Path(__file__).resolve().parent.parent
```

4. Create `shared/logging_setup.py`:

```python
"""Shared logging configuration factory."""

import logging
from pathlib import Path


def setup_logging(name: str, log_file: Path) -> logging.Logger:
    """Configure and return a logger with file + console handlers."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    if not logger.handlers:  # avoid duplicate handlers on reload
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            handlers=[
                logging.FileHandler(log_file, encoding="utf-8"),
                logging.StreamHandler(),
            ],
        )
    return logger
```

5. **Do NOT change bot.py or codex_bot.py yet.** Verify both bots still start normally.

### Commit message
`refactor(phase-0): create directory structure and shared config`

---

## Phase 1: Extract Shared Utilities

Extract duplicated pure functions into `shared/` modules. Update both bots to import from shared.

### Step 1.1: `shared/discord_utils.py`

Extract these functions (identical in both bots):

```python
"""Discord utility functions shared by both bots."""

import re
from pathlib import Path

import discord

from shared.config import MAX_DISCORD_LEN, DOCUMENTS_DIR


def split_message(text: str, limit: int = MAX_DISCORD_LEN) -> list[str]:
    """Split text into Discord-safe chunks, breaking at newlines/spaces."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        idx = text.rfind("\n", 0, limit)
        if idx < limit // 3:
            idx = text.rfind(" ", 0, limit)
        if idx < limit // 3:
            idx = limit
        chunks.append(text[:idx])
        text = text[idx:].lstrip("\n")
    return chunks


def sanitize(text: str) -> str:
    """Prevent accidental @everyone/@here pings."""
    return text.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")


def is_guild_channel(channel: discord.abc.Messageable) -> bool:
    """True if the channel is in a guild (not a DM)."""
    return getattr(channel, "guild", None) is not None


def guild_slug(guild: discord.Guild) -> str:
    """Filesystem-safe slug from guild name."""
    return re.sub(r"[^\w\-]", "-", guild.name).strip("-").lower()[:50]


def guild_docs_dir(guild_id: int, guild: discord.Guild = None,
                   primary_guild_id: int = 0) -> Path:
    """Primary guild -> ~/Documents. Others -> ~/Documents/{slug}/."""
    if guild_id == primary_guild_id:
        return DOCUMENTS_DIR
    slug = guild_slug(guild) if guild else str(guild_id)
    return DOCUMENTS_DIR / slug
```

**Source locations:**
- `bot.py` lines 80-82: `_guild_slug()` → `guild_slug()`
- `bot.py` lines 85-90: `_guild_docs_dir()` → `guild_docs_dir()` (add `primary_guild_id` param instead of reading module global)
- `bot.py` lines 1724-1740: `split_message()`
- `bot.py` lines 1743-1745: `sanitize()`
- `bot.py` lines 1748-1750: `_is_guild_channel()` → `is_guild_channel()`
- `codex_bot.py` lines 73-74: `_guild_slug()` (identical)
- `codex_bot.py` lines 77-81: `_guild_docs_dir()` (identical)
- `codex_bot.py` lines 728-743: `split_message()` (identical)
- `codex_bot.py` lines 746-747: `sanitize()` (identical)
- `codex_bot.py` lines 750-751: `_is_guild_channel()` (identical)

**Update bot.py:**
- Remove the local definitions of all 5 functions
- Add at top: `from shared.discord_utils import split_message, sanitize, is_guild_channel, guild_slug, guild_docs_dir`
- Replace all calls to `_guild_slug(g)` with `guild_slug(g)`
- Replace all calls to `_guild_docs_dir(gid, g)` with `guild_docs_dir(gid, g, PRIMARY_GUILD_ID)`
- Replace all calls to `_is_guild_channel(ch)` with `is_guild_channel(ch)`

**Update codex_bot.py:** Same changes.

### Step 1.2: `shared/bot_actions.py`

```python
"""Bot action extraction — shared regex and parser."""

import re
import json
import logging

log = logging.getLogger(__name__)

BOT_ACTION_RE = re.compile(
    r"(?:```bot_action\s*\n(.*?)\n```|<bot_action>\s*(.*?)\s*</bot_action>)",
    re.DOTALL,
)


def extract_bot_actions(text: str) -> tuple[str, list[dict]]:
    """Extract bot_action blocks from response text.
    Returns (cleaned_text, list_of_action_dicts)."""
    actions = []
    for m in BOT_ACTION_RE.finditer(text):
        raw = m.group(1) or m.group(2)
        try:
            actions.append(json.loads(raw))
        except json.JSONDecodeError:
            log.warning(f"Bad bot_action JSON: {raw[:100]}")
    cleaned = BOT_ACTION_RE.sub("", text).strip()
    return cleaned, actions
```

**Source:** `bot.py` lines 1179-1197, `codex_bot.py` lines 650-665 (identical logic)

**Update both bots:** Remove local `BOT_ACTION_RE` and `extract_bot_actions`. Import from `shared.bot_actions`.

### Step 1.3: `shared/hotreload.py`

```python
"""Hot reload detection and syntax validation."""

import subprocess
import sys
from pathlib import Path

from shared.config import CREATE_FLAGS


def check_self_modified(bot_file: Path, boot_mtime: float) -> bool:
    """Check if a bot file has been modified since startup."""
    try:
        return bot_file.stat().st_mtime != boot_mtime
    except Exception:
        return False


def validate_syntax(bot_file: Path) -> tuple[bool, str | None]:
    """Validate Python syntax of a file. Returns (ok, error_message)."""
    try:
        result = subprocess.run(
            [sys.executable, "-c",
             f"import py_compile; py_compile.compile({str(bot_file)!r}, doraise=True)"],
            capture_output=True, text=True, timeout=10,
            creationflags=CREATE_FLAGS,
        )
        if result.returncode != 0:
            return False, result.stderr.strip() or result.stdout.strip()
        return True, None
    except subprocess.TimeoutExpired:
        return False, "Syntax check timed out"
    except Exception as e:
        return False, str(e)
```

**Source:**
- `bot.py` lines 100-105: `_self_modified()`
- `bot.py` lines ~1467-1480: syntax validation inside reload action handler
- `codex_bot.py` lines 54-58: `_self_modified()` (identical)
- `codex_bot.py` lines 706-718: syntax validation (identical pattern)

**Update both bots:**
- Remove `_self_modified()` function and `_BOT_FILE`/`_BOOT_MTIME` globals
- Add at top: `from shared.hotreload import check_self_modified, validate_syntax`
- Set module-level: `_BOT_FILE = Path(__file__)` and `_BOOT_MTIME = _BOT_FILE.stat().st_mtime`
- Replace `_self_modified()` calls with `check_self_modified(_BOT_FILE, _BOOT_MTIME)`
- Replace inline syntax validation with `validate_syntax(_BOT_FILE)`

### Step 1.4: `shared/attachments.py`

```python
"""Attachment download and cleanup utilities."""

import logging
from pathlib import Path

log = logging.getLogger(__name__)


async def download_attachments(
    attachments: list,  # list of discord.Attachment
    att_dir: Path,
    extract_pdf_text: bool = True,
) -> list[dict]:
    """Download Discord attachments to a local directory.

    Returns list of dicts: {path, filename, description} where description
    includes PDF text extraction if enabled and pymupdf is available.
    """
    att_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []

    for att in attachments:
        dest = att_dir / att.filename
        try:
            await att.save(dest)
        except Exception as e:
            log.warning(f"Failed to download attachment {att.filename}: {e}")
            continue

        info = {"path": str(dest), "filename": att.filename}

        # PDF text extraction
        if extract_pdf_text and att.filename.lower().endswith(".pdf"):
            try:
                import fitz  # pymupdf
                doc = fitz.open(str(dest))
                text_pages = []
                for page in doc:
                    text_pages.append(page.get_text())
                doc.close()
                info["description"] = (
                    f"[PDF: {att.filename}, {len(text_pages)} pages]\n"
                    + "\n---\n".join(text_pages[:20])  # cap at 20 pages
                )
            except ImportError:
                info["description"] = f"[PDF: {att.filename}]"
            except Exception as e:
                info["description"] = f"[PDF: {att.filename}, extraction failed: {e}]"
        else:
            info["description"] = f"[Attached file: {att.filename}]"

        downloaded.append(info)

    return downloaded


def cleanup_attachments(att_paths: list[str]):
    """Delete downloaded attachment files. Best-effort, ignores errors."""
    for p in att_paths:
        try:
            Path(p).unlink(missing_ok=True)
        except Exception:
            pass
```

**Source:**
- `bot.py` lines 2010-2044: attachment download with PDF extraction
- `bot.py` lines 2604-2616: cleanup
- `codex_bot.py` lines 841-852: attachment download (simpler, no PDF)
- `codex_bot.py` lines 1095-1099: cleanup

**Update both bots:** Import from `shared.attachments`. Remove local attachment handling code. Codex bot can call with `extract_pdf_text=False` if pymupdf isn't available for it.

### Commit message
`refactor(phase-1): extract shared utilities into shared/ package`

---

## Phase 2: Extract BotState

### Step 2.1: Create `shared/state.py`

Copy the full `BotState` class from `bot.py` lines 620-797 into `shared/state.py`. This is the superset — codex_bot's version is a strict subset (no `save_context`, `get_context`, `list_contexts`, `delete_context`, `scan_disk_sessions`).

```python
"""Unified BotState — JSON-backed persistent state for sessions, projects, and guilds."""

import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)


class BotState:
    """JSON-backed state for sessions and projects. Survives restarts."""

    def __init__(self, path: Path, primary_guild_id: int = 0):
        self.path = path
        self.primary_guild_id = primary_guild_id
        self._data = self._load()

    # ... (copy entire class from bot.py lines 620-797)
    # Change: replace references to the global PRIMARY_GUILD_ID with self.primary_guild_id
    # In _load(), line 637: p["guild_id"] = PRIMARY_GUILD_ID -> p["guild_id"] = self.primary_guild_id
```

**Key changes from bot.py's version:**
- Constructor takes `primary_guild_id` as parameter instead of reading the module global
- `_load()` uses `self.primary_guild_id` for migration

**Update bot.py:**
- Remove entire `BotState` class (lines 620-797)
- Add: `from shared.state import BotState`
- Change instantiation: `state = BotState(STATE_FILE)` → `state = BotState(STATE_FILE, PRIMARY_GUILD_ID)`
- Note: `PRIMARY_GUILD_ID` is set in `on_ready`, so `state.primary_guild_id` should be updated there too: `state.primary_guild_id = PRIMARY_GUILD_ID`

**Update codex_bot.py:**
- Remove entire `BotState` class (lines 107-185)
- Add: `from shared.state import BotState`
- Change instantiation similarly
- At call sites: `set_session(ctx, conversation_id=conv_id, cwd=cwd)` → `set_session(ctx, session_id=conv_id, cwd=cwd)` (rename the keyword argument)

### Commit message
`refactor(phase-2): unify BotState into shared/state.py`

---

## Phase 3: Decompose bot.py

This is the big one. Extract modules one at a time from bot.py. **After each step, bot.py must still work** — the extracted module is imported back in.

### Step 3.1: `claude/memories.py`

**Extract from bot.py lines 118-219.** Move these functions:
- `_memories_file(guild_id)` → `memories_file(guild_id, primary_guild_id, memories_dir)`
- `load_memories(guild_id)`
- `save_memories(memories, guild_id)`
- `_next_memory_id(memories)`
- `process_memory_actions(text, channel_name, server_name, guild_id)`
- `_format_memories_for_prompt(guild_id)` → `format_memories_for_prompt(guild_id)`
- `MEMORY_ACTION_RE` constant

**Dependencies:** `json`, `re`, `logging`, `datetime`, `pathlib.Path`

**Configuration needed:** The memories directory path (`selfbot/`) and `PRIMARY_GUILD_ID`. Pass these as parameters or set as module-level config:

```python
"""Memory system — persistent notebook shared with selfbot."""

import json
import re
import logging
from pathlib import Path
from datetime import datetime

log = logging.getLogger(__name__)

MEMORY_ACTION_RE = re.compile(r"```memory\s*\n(.*?)\n```", re.DOTALL)

# Set by claude/bot.py at startup
_memories_dir: Path = Path("selfbot")
_primary_guild_id: int = 0


def configure(memories_dir: Path, primary_guild_id: int):
    global _memories_dir, _primary_guild_id
    _memories_dir = memories_dir
    _primary_guild_id = primary_guild_id


def memories_file(guild_id: int = None) -> Path:
    # ... (from bot.py lines 124-128, using _memories_dir and _primary_guild_id)

# ... rest of functions copied from bot.py lines 131-218
```

**Update bot.py:**
- Remove lines 118-219
- Add: `from claude.memories import configure as configure_memories, process_memory_actions, format_memories_for_prompt, MEMORY_ACTION_RE`
- In `on_ready` or at module level: `configure_memories(Path(__file__).parent / "selfbot", PRIMARY_GUILD_ID)`

### Step 3.2: `claude/reminders.py`

**Extract from bot.py lines 221-334 AND lines 1773-1825** (the `_reminder_loop` coroutine and `_send_toast` are in the on_ready section).

Move these functions:
- `load_reminders()`
- `save_reminders(reminders)`
- `_next_reminder_id(reminders)`
- `process_reminder_actions(text, channel_id, channel_name, requester_id)`
- `_format_reminders_for_prompt()` → `format_reminders_for_prompt()`
- `_send_toast(title, message)`
- `reminder_loop(client)` — the background coroutine that checks and fires reminders
- `REMINDER_ACTION_RE`, `PST` constants

**Dependencies:** `json`, `re`, `logging`, `datetime`, `subprocess`, `pathlib.Path`, `asyncio`, `discord`

**Configuration needed:** `REMINDERS_FILE` path, `OWNER_ID`, `CREATE_FLAGS`

```python
"""Reminder system — scheduled notifications with Discord ping + Windows toast."""

import json
import re
import logging
import asyncio
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

REMINDER_ACTION_RE = re.compile(r"```reminder\s*\n(.*?)\n```", re.DOTALL)
PST = timezone(timedelta(hours=-8))

# Set by claude/bot.py at startup
_reminders_file: Path = Path("selfbot/reminders.json")
_owner_id: int = 0
_create_flags: int = 0


def configure(reminders_file: Path, owner_id: int, create_flags: int):
    global _reminders_file, _owner_id, _create_flags
    _reminders_file = reminders_file
    _owner_id = owner_id
    _create_flags = create_flags


# ... rest of functions from bot.py lines 229-334
# ... _send_toast from bot.py lines 303-333
# ... reminder_loop from bot.py lines 1773-1825
```

**Update bot.py:**
- Remove lines 221-334 and the `_reminder_loop` function (lines 1773-1825)
- Import from `claude.reminders`
- Call `configure(...)` in on_ready or at module level

### Step 3.3: `claude/bridge.py`

**Extract from bot.py lines 801-1144.** This is the core Claude Code subprocess management.

Move these classes/functions:
- `_TurnState` class (lines 801-812)
- `_PersistentProcess` class (lines 815-1070) — the big one
- `ClaudeBridge` class (lines 1072-1130)
- `_tool_description(name, inp)` helper (lines 1132-1144)

**Dependencies:** `asyncio`, `json`, `subprocess`, `logging`, `pathlib.Path`, `os`, `signal`

**Configuration needed:** `CLAUDE_CMD`, `CLAUDE_MODEL`, `CREATE_FLAGS`, `PROCESS_TIMEOUT`

```python
"""Claude Code bridge — manages persistent Claude CLI subprocesses."""

import os
import json
import asyncio
import signal
import subprocess
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Set by claude/bot.py at startup
CLAUDE_CMD = "claude"
CLAUDE_MODEL = ""
CREATE_FLAGS = 0
PROCESS_TIMEOUT = 600


def configure(claude_cmd: str, claude_model: str, create_flags: int, process_timeout: int):
    global CLAUDE_CMD, CLAUDE_MODEL, CREATE_FLAGS, PROCESS_TIMEOUT
    CLAUDE_CMD = claude_cmd
    CLAUDE_MODEL = claude_model
    CREATE_FLAGS = create_flags
    PROCESS_TIMEOUT = process_timeout


class _TurnState:
    # ... from bot.py lines 801-812

class _PersistentProcess:
    # ... from bot.py lines 815-1070
    # NOTE: This class references CLAUDE_CMD, CLAUDE_MODEL, CREATE_FLAGS, PROCESS_TIMEOUT
    # as module-level variables. Those are set by configure().

class ClaudeBridge:
    # ... from bot.py lines 1072-1130

def tool_description(name: str, inp: dict) -> str:
    # ... from bot.py lines 1132-1144
```

**Update bot.py:**
- Remove lines 801-1144
- Add: `from claude.bridge import ClaudeBridge, tool_description, configure as configure_bridge`
- Call `configure_bridge(CLAUDE_CMD, CLAUDE_MODEL, CREATE_FLAGS, PROCESS_TIMEOUT)` at module level

### Step 3.4: `claude/prompts.py`

**Extract from bot.py lines 336-616.**

Move these functions:
- `_build_system_context(projects, channel_name, server_name, docs_dir, guild_id)` → `build_system_context(...)`
- `_build_thread_context()` → `build_thread_context()`

**Dependencies:** `datetime`, `pathlib.Path`

**These functions reference:**
- `_BOT_FILE` — pass as parameter or set via configure()
- `_format_memories_for_prompt()` — import from `claude.memories`
- `_format_reminders_for_prompt()` — import from `claude.reminders`
- `CODEX_BOT_USER_ID` — pass as parameter or set via configure()
- `PST` — import from `claude.reminders`

```python
"""System prompt builders for Claude bot."""

from datetime import datetime
from pathlib import Path

from claude.memories import format_memories_for_prompt
from claude.reminders import format_reminders_for_prompt, PST

# Set by claude/bot.py
_bot_file: str = ""
_codex_bot_user_id: str = ""


def configure(bot_file: str, codex_bot_user_id: str):
    global _bot_file, _codex_bot_user_id
    _bot_file = bot_file
    _codex_bot_user_id = codex_bot_user_id


def build_system_context(projects: dict, channel_name: str = "claude",
                         server_name: str = "", docs_dir: str = "~/Documents",
                         guild_id: int = None) -> str:
    # ... from bot.py lines 338-531

def build_thread_context() -> str:
    # ... from bot.py lines 534-616
```

**Update bot.py:**
- Remove lines 336-616
- Import from `claude.prompts`

### Step 3.5: `claude/system_stats.py`

**Extract from bot.py lines 1147-1175.** Small and self-contained.

```python
"""System resource monitoring."""

import subprocess
import logging

import psutil

from shared.config import CREATE_FLAGS

log = logging.getLogger(__name__)


def system_stats() -> str:
    # ... from bot.py lines 1149-1174
```

**Update bot.py:** Remove lines 1147-1175, import `from claude.system_stats import system_stats`.

### Step 3.6: `claude/image_gen.py`

**Extract from bot.py lines 1272-1407.**

Move:
- `_generate_image(prompt, ref_images)` → `generate_image(...)`
- `_bg_generate_image(channel, prompt, ref_images, caption, requester_id)` → `bg_generate_image(...)`

**Dependencies:** `aiohttp`, `base64`, `json`, `logging`, `pathlib.Path`, `tempfile`, `discord`

**Configuration needed:** `OPENROUTER_API_KEY`, `IMAGE_MODEL`, `GENERATED_IMAGES_DIR`

```python
"""Image generation via Gemini 3 Pro (OpenRouter)."""

import json
import base64
import logging
import tempfile
from pathlib import Path
from datetime import datetime

import aiohttp
import discord

log = logging.getLogger(__name__)

# Set by claude/bot.py
OPENROUTER_API_KEY = ""
IMAGE_MODEL = "google/gemini-3-pro-image-preview"
GENERATED_IMAGES_DIR = Path("generated_images")


def configure(api_key: str, model: str, images_dir: Path):
    global OPENROUTER_API_KEY, IMAGE_MODEL, GENERATED_IMAGES_DIR
    OPENROUTER_API_KEY = api_key
    IMAGE_MODEL = model
    GENERATED_IMAGES_DIR = images_dir


async def generate_image(prompt: str, ref_images: list[str] = None) -> ...:
    # ... from bot.py lines 1272-1370ish

async def bg_generate_image(channel, prompt, ref_images, caption, requester_id) -> None:
    # ... from bot.py lines 1372-1407
```

### Step 3.7: `claude/actions.py`

**Extract from bot.py lines 1410-1720.** This is the big bot action dispatcher.

Move:
- `execute_bot_actions(actions, message, channel, guild_id, caller_ctx_key)`
- `_resolve_voice_channel(ref, guild)` (lines 1682-1719)

**Dependencies:** This imports from many other modules:
- `claude.system_stats.system_stats`
- `claude.image_gen.bg_generate_image`
- `claude.project_seeding.seed_project`
- `integrations.suno.enqueue_music` (at this point, suno.py hasn't moved yet — import from root)
- `integrations.voice.VoiceManager` (at this point, voice.py hasn't moved yet — import from root)
- `integrations.council.call_gpt, call_researcher` (same — import from root for now)
- `shared.hotreload.validate_syntax`
- `shared.state.BotState`

**Important:** During Phase 3, the integration files (suno.py, voice.py, council.py) are still at the root level. Use conditional imports or import from root. They'll be moved in Phase 5, and imports updated then.

```python
"""Bot action dispatcher for Claude bot."""

import re
import logging
from pathlib import Path

import discord

log = logging.getLogger(__name__)

# These will be set by claude/bot.py to avoid circular imports
_state = None
_bridge = None
_voice_manager = None
_client = None


def configure(state, bridge, voice_manager, client):
    global _state, _bridge, _voice_manager, _client
    _state = state
    _bridge = bridge
    _voice_manager = voice_manager
    _client = client


async def execute_bot_actions(
    actions: list[dict],
    message: discord.Message,
    channel: discord.abc.Messageable,
    guild_id: int = 0,
    caller_ctx_key: str = None,
) -> tuple[list[str], bool, list[discord.File], list[str]]:
    """Execute bot actions. Returns (status_messages, should_reload, files_to_attach, council_feedback)."""
    # ... from bot.py lines 1410-1720
```

### Step 3.8: `claude/project_seeding.py`

**Extract from bot.py lines 1200-1270.**

```python
"""Project seeding — sends initial prompt to new project threads."""

import asyncio
import logging

import discord

from shared.config import TYPING_INTERVAL

log = logging.getLogger(__name__)

# Set by claude/bot.py
_bridge = None
_state = None


def configure(bridge, state):
    global _bridge, _state
    _bridge = bridge
    _state = state


async def seed_project(thread, project_name, cwd, seed_msg, guild_id=0):
    # ... from bot.py lines 1200-1270
    # Uses _bridge and _state instead of module-level globals
```

### Step 3.9: `claude/research.py`

**Extract from bot.py on_message handler, lines 1938-2003.** This is the `/research` command handler.

```python
"""Research command handler — creates council research threads."""

import logging

import discord

log = logging.getLogger(__name__)

# Set by claude/bot.py
_state = None
_bridge = None
_council_gpt_history = None


def configure(state, bridge, council_gpt_history):
    global _state, _bridge, _council_gpt_history
    _state = state
    _bridge = bridge
    _council_gpt_history = council_gpt_history


async def handle_research_command(message, raw_text, guild_id):
    """Handle /research command. Returns True if handled, False otherwise."""
    # ... extract the research handling logic from bot.py on_message
    # Returns True if this was a /research command and was handled
```

### Step 3.10: `claude/context_switching.py`

**Extract from bot.py on_message handler, lines 2092-2207.** These are the `.new-context`, `.list-contexts`, `.resume-context` commands.

```python
"""Context switching commands for Claude bot."""

import logging

import discord

log = logging.getLogger(__name__)

_state = None
_bridge = None


def configure(state, bridge):
    global _state, _bridge
    _state = state
    _bridge = bridge


async def handle_context_command(message, content, guild_id, ctx_key, cwd):
    """Handle .new-context, .list-contexts, .resume-context commands.
    Returns True if handled, False otherwise."""
    # ... from bot.py lines 2092-2207
```

### Step 3.11: Rename remaining bot.py → `claude/bot.py`

After all extractions, what remains in bot.py should be approximately:
- Imports (~30 lines)
- Config loading from .env (~20 lines)
- Module-level instances: state, bridge, client (~15 lines)
- Integration imports: suno, council, voice (~15 lines)
- `on_ready()` (~50 lines)
- `on_voice_state_update()` (~10 lines)
- `on_message()` (~250 lines — the dispatcher that delegates to extracted modules)
- Entry point (~15 lines)
- **Total: ~350-400 lines**

**Steps:**
1. Move the remaining `bot.py` content to `claude/bot.py`
2. Update all internal imports to use the new module paths
3. Keep root `bot.py` as a thin wrapper that just imports and runs `claude/bot.py`:

```python
#!/usr/bin/env python3
"""Entry point — delegates to claude/bot.py."""
from claude.bot import main
if __name__ == "__main__":
    main()
```

This thin wrapper is needed because `run.py` launches `bot.py` from the root.

### Commit message
`refactor(phase-3): decompose bot.py into claude/ package (11 modules)`

---

## Phase 4: Decompose codex_bot.py

Same treatment, simpler because codex_bot.py is smaller and has fewer features.

### Step 4.1: `codex/bridge.py`

**Extract from codex_bot.py lines 190-645.**

Move:
- `_TurnState` class (lines 190-200)
- `CodexAppServer` class (lines 203-645)

**Configuration needed:** `CODEX_CMD`, `CODEX_MODEL`, `CREATE_FLAGS`

### Step 4.2: `codex/prompts.py`

**Extract from codex_bot.py lines 84-102.**

Move:
- `_build_system_context()` → `build_system_context()`
- `_build_thread_context()` → `build_thread_context()`

### Step 4.3: `codex/actions.py`

**Extract from codex_bot.py lines 648-723.**

Move:
- Bot action execution (upload + reload only)

### Step 4.4: Slim down codex_bot.py → `codex/bot.py`

Same pattern as claude — keep root `codex_bot.py` as thin wrapper:

```python
#!/usr/bin/env python3
"""Entry point — delegates to codex/bot.py."""
from codex.bot import main
if __name__ == "__main__":
    main()
```

### Commit message
`refactor(phase-4): decompose codex_bot.py into codex/ package`

---

## Phase 5: Move Integrations

Move already-modular root files into `integrations/`. These files need minimal changes — just update import paths.

### Step 5.1: `council.py` → `integrations/council.py`

Copy file, update internal imports if any. Then update importers:
- `claude/actions.py`: `from council import call_gpt, call_researcher` → `from integrations.council import call_gpt, call_researcher`
- `claude/bot.py`: same

### Step 5.2: `council_opus_prompt.py` → `integrations/council_prompt.py`

Copy file, rename. Update importers:
- `claude/prompts.py` or `claude/research.py`: `from council_opus_prompt import build_opus_council_prompt` → `from integrations.council_prompt import build_opus_council_prompt`

### Step 5.3: `suno.py` → `integrations/suno.py`

Copy file. Update importers:
- `claude/bot.py`: `from suno import init_suno_worker, enqueue_music` → `from integrations.suno import init_suno_worker, enqueue_music`
- `claude/actions.py`: same

### Step 5.4: `voice.py` → `integrations/voice.py` and `voice_recv_patch.py` → `integrations/voice_recv_patch.py`

Copy files. Update importers:
- `claude/bot.py`: `from voice import VoiceManager` → `from integrations.voice import VoiceManager`
- `from voice_recv_patch import apply_patch` → `from integrations.voice_recv_patch import apply_patch`
- Inside `integrations/voice.py`: if it imports `voice_recv_patch`, update that too

### Step 5.5: Delete original root files

After verifying imports work from the new locations, delete the root-level copies:
- `council.py`
- `council_opus_prompt.py`
- `suno.py`
- `voice.py`
- `voice_recv_patch.py`

### Commit message
`refactor(phase-5): move integrations to integrations/ package`

---

## Phase 6: Move Data, Logs, and Scripts

### Step 6.1: Move state files

Update path references:
- `bot.py` (now `claude/bot.py`): `STATE_FILE = Path(__file__).parent / "state.json"` → `STATE_FILE = PROJECT_ROOT / "data" / "state.json"`
- `codex_bot.py` (now `codex/bot.py`): same for `codex_state.json`

Create `data/` directory. Move existing files:
```bash
mv state.json data/state.json 2>/dev/null
mv codex_state.json data/codex_state.json 2>/dev/null
```

### Step 6.2: Move log files

Update logging setup calls to point to `logs/`:
- Claude bot: `logs/claudebot.log`
- Codex bot: `logs/codexbot.log`

```bash
mkdir -p logs
mv claudebot.log logs/claudebot.log 2>/dev/null
mv codexbot.log logs/codexbot.log 2>/dev/null
```

Update selfbot log path if it references the root.

### Step 6.3: Move attachment directories

Update path constants:
- `ATTACHMENTS_DIR` → `PROJECT_ROOT / "data" / "attachments"`
- `CODEX_ATTACHMENTS_DIR` → `PROJECT_ROOT / "data" / "codex_attachments"`

```bash
mv attachments data/attachments 2>/dev/null
mv codex_attachments data/codex_attachments 2>/dev/null
```

### Step 6.4: Move generated content directories

- `GENERATED_IMAGES_DIR` → `PROJECT_ROOT / "data" / "generated_images"`
- `GENERATED_MUSIC_DIR` → `PROJECT_ROOT / "data" / "generated_music"`

```bash
mv generated_images data/generated_images 2>/dev/null
mv generated_music data/generated_music 2>/dev/null
```

### Step 6.5: Move scripts

```bash
mv restart.ps1 scripts/
mv kill_bots.ps1 scripts/
mv kill_all.py scripts/
mv install_task.py scripts/
```

Update any references to these scripts (e.g., if bot actions reference `restart.ps1`).

### Step 6.6: Update `.gitignore`

Replace root-level entries with new paths:

```gitignore
# Secrets
.env
.env.bak

# Runtime data
data/

# Logs
logs/

# Python
__pycache__/
*.pyc

# Models (large binaries)
models/

# Selfbot state
selfbot/sessions.json
selfbot/sessions.tmp
selfbot/memories.json
selfbot/memories*.json
selfbot/memories.tmp
selfbot/reminders.json
selfbot/reminders.tmp
selfbot discordpy package/
selfbot/nul

# Reference repos
reference/

# Misc
nul
.reload
tmp_log_check.py
```

### Commit message
`refactor(phase-6): organize runtime files into data/, logs/, scripts/`

---

## Phase 7: Harden Supervisor

### Step 7.1: Per-bot independent restart

Replace the current all-or-nothing restart with a `BotProcess` class:

```python
class BotProcess:
    """Manages a single bot subprocess with independent crash tracking."""

    def __init__(self, name: str, start_fn):
        self.name = name
        self.start_fn = start_fn
        self.proc: subprocess.Popen | None = None
        self.recent_crashes: list[float] = []
        self.start_time: float = 0

    def start(self):
        self.proc = self.start_fn()
        self.start_time = time.time()
        log.info(f"{self.name} started (pid={self.proc.pid})")

    def poll(self) -> int | None:
        if self.proc is None:
            return None
        return self.proc.poll()

    def restart_if_crashed(self) -> bool:
        """Check if process crashed and restart if needed. Returns True if restarted."""
        ret = self.poll()
        if ret is None:
            return False

        log.warning(f"{self.name} exited with code {ret}")
        _capture_crash_info(self.name, self.proc, ret)

        now = time.time()
        self.recent_crashes = [t for t in self.recent_crashes if now - t < RAPID_WINDOW]
        self.recent_crashes.append(now)

        if len(self.recent_crashes) >= MAX_RAPID_RESTARTS:
            log.error(f"{self.name}: {MAX_RAPID_RESTARTS} crashes in {RAPID_WINDOW}s — backing off {BACKOFF_DELAY}s")
            time.sleep(BACKOFF_DELAY)
            self.recent_crashes.clear()
        else:
            time.sleep(RESTART_DELAY)

        self.start()
        return True

    def terminate(self, timeout: int = 10):
        """Graceful shutdown with force-kill fallback."""
        if self.proc is None:
            return
        # ... graceful termination logic
```

Key change: When claude bot crashes, **only claude bot restarts**. Codex and selfbot keep running. All bots only restart together on `.env` change.

### Step 7.2: Heartbeat health checks

Add a heartbeat file check to the supervisor loop:

```python
HEARTBEAT_TIMEOUT = 120  # seconds before considering a bot stuck

def _check_heartbeat(name: str) -> bool:
    """Returns True if heartbeat is fresh, False if stale/missing."""
    hb_file = WATCH_DIR / "data" / f"heartbeat_{name}.json"
    if not hb_file.exists():
        return True  # no heartbeat file yet — bot might be starting up
    try:
        data = json.loads(hb_file.read_text("utf-8"))
        age = time.time() - data.get("timestamp", 0)
        return age < HEARTBEAT_TIMEOUT
    except Exception:
        return True  # can't read — give benefit of doubt
```

Add to each bot a simple heartbeat coroutine:

```python
async def _heartbeat_loop():
    """Write heartbeat file every 30 seconds."""
    hb_file = PROJECT_ROOT / "data" / f"heartbeat_{BOT_NAME}.json"
    while True:
        try:
            hb_file.write_text(json.dumps({
                "timestamp": time.time(),
                "pid": os.getpid(),
            }), "utf-8")
        except Exception:
            pass
        await asyncio.sleep(30)
```

Start this in each bot's `on_ready`.

### Step 7.3: Crash forensics

```python
def _capture_crash_info(name: str, proc: subprocess.Popen, exit_code: int):
    """Log crash context for diagnosis."""
    crash_time = datetime.now().isoformat()
    crash_log = WATCH_DIR / "logs" / "crashes.jsonl"
    crash_log.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        "bot": name,
        "exit_code": exit_code,
        "timestamp": crash_time,
        "pid": proc.pid,
    }
    with open(crash_log, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    # Snapshot last 50 lines of bot log
    log_file = WATCH_DIR / "logs" / f"{name}.log"
    if log_file.exists():
        try:
            lines = log_file.read_text("utf-8").splitlines()[-50:]
            snapshot = WATCH_DIR / "logs" / f"crash_{name}_{crash_time.replace(':', '-')}.txt"
            snapshot.write_text("\n".join(lines), "utf-8")
        except Exception:
            pass
```

### Step 7.4: Graceful shutdown

```python
def _graceful_terminate(proc, name, timeout=10):
    """Try graceful shutdown, then force kill."""
    if proc is None:
        return
    try:
        if os.name == "nt":
            # Send CTRL_BREAK to allow graceful shutdown
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
        proc.wait(timeout=timeout)
        log.info(f"{name} shut down gracefully")
    except subprocess.TimeoutExpired:
        log.warning(f"{name} did not stop gracefully, force killing")
        _terminate(proc)  # existing tree-kill logic
```

### Step 7.5: Supervisor watchdog

Write `data/supervisor_heartbeat.json` every loop iteration:

```python
def _write_supervisor_heartbeat(bots: dict[str, BotProcess]):
    data = {
        "timestamp": time.time(),
        "bots": {
            name: {
                "pid": bp.proc.pid if bp.proc else None,
                "alive": bp.proc is not None and bp.poll() is None,
                "uptime": time.time() - bp.start_time if bp.proc else 0,
            }
            for name, bp in bots.items()
        }
    }
    hb_file = WATCH_DIR / "data" / "supervisor_heartbeat.json"
    hb_file.parent.mkdir(parents=True, exist_ok=True)
    hb_file.write_text(json.dumps(data, indent=2), "utf-8")
```

### Step 7.6: Structured logging

Replace all `print()` in run.py with proper logging:

```python
import logging

log = logging.getLogger("supervisor")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(WATCH_DIR / "logs" / "supervisor.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
```

### Step 7.7: Update bot launch paths

Update `run.py` to launch from new paths:
- `run_bot()`: launch `claude/bot.py` (or root `bot.py` wrapper)
- `run_codex_bot()`: launch `codex/bot.py` (or root `codex_bot.py` wrapper)

### Commit message
`refactor(phase-7): harden supervisor with health checks, crash forensics, graceful shutdown`

---

## Phase 8: Cleanup

### Step 8.1: Remove old root files

After verifying everything works from new locations, delete:
- Root-level `council.py`, `council_opus_prompt.py`, `suno.py`, `voice.py`, `voice_recv_patch.py`
- Root-level `restart.ps1`, `kill_bots.ps1`, `kill_all.py`, `install_task.py`
- Any other files that were moved

**Do NOT delete:**
- Root `bot.py` and `codex_bot.py` (they're now thin wrappers)
- `run.py` (still the entry point)
- `.env`, `.gitignore`, `pyproject.toml`, `CLAUDE.md`

### Step 8.2: Update CLAUDE.md

Update with new directory structure and module descriptions.

### Step 8.3: Move test files to `tests/`

```bash
mv test_cc_latency.py tests/
mv test_interrupt_latency.py tests/
mv test_latency_comparison.py tests/
```

### Step 8.4: Clean up misc root files

- `main.py` — delete (was a stub, no longer needed)
- `eval_runner.py` — move to `scripts/` or `tests/`
- `user_memories.md` — move to `data/` or delete if unused

### Commit message
`refactor(phase-8): cleanup old files, update docs`

---

## Verification Checklist

Run after each phase:

1. **Syntax check all modified Python files:**
   ```bash
   python -c "import py_compile; py_compile.compile('shared/config.py', doraise=True)"
   ```

2. **Import check:**
   ```bash
   python -c "from shared.state import BotState; print('OK')"
   python -c "from shared.discord_utils import split_message; print('OK')"
   ```

3. **Boot test:** Start both bots, verify they reach `on_ready` in logs

4. **Message test:** Mention each bot in Discord, verify response

5. **Feature tests (after all phases):**
   - [ ] Basic message → response
   - [ ] Streaming tool status during response
   - [ ] Memory save/load (ask bot to remember something, restart, verify)
   - [ ] Reminder set/fire
   - [ ] Image generation
   - [ ] Music generation
   - [ ] Voice join/leave
   - [ ] Project creation (creates thread + folder)
   - [ ] Context switching (.new-context, .list-contexts, .resume-context)
   - [ ] /research command
   - [ ] File upload
   - [ ] Reload (edit bot, say "reload")
   - [ ] State persistence across restart
   - [ ] Codex bot basic response
   - [ ] Codex bot file upload
   - [ ] Supervisor crash recovery (kill a bot process, verify restart)
   - [ ] Supervisor .env change detection (touch .env, verify all restart)

---

## Notes for Implementer

1. **Module-level `configure()` pattern:** Many extracted modules use a `configure()` function to receive references to shared objects (state, bridge, client). This avoids circular imports. Call `configure()` in the bot's startup before any other module functions are used.

2. **Don't break selfbot:** The selfbot reads `selfbot/memories.json` and `selfbot/reminders.json` directly. These paths must NOT change. The memory/reminder modules in `claude/` read/write the same files.

3. **Root wrappers:** Keep thin `bot.py` and `codex_bot.py` at the root so `run.py` doesn't need path changes until Phase 7. The wrappers just do `from claude.bot import main; main()`.

4. **Import ordering:** When extracting, make sure `shared/` modules don't import from `claude/` or `codex/` — the dependency direction is: `claude/` → `shared/`, `codex/` → `shared/`, `claude/` → `integrations/`.

5. **The `configure()` calls order matters:** In `claude/bot.py`, call configures in dependency order:
   - `shared.config` (no configure needed — pure constants)
   - `claude.memories.configure(...)`
   - `claude.reminders.configure(...)`
   - `claude.bridge.configure(...)`
   - `claude.prompts.configure(...)`
   - `claude.actions.configure(...)` (depends on bridge, state, voice)
   - `claude.project_seeding.configure(...)`
   - etc.

6. **run.py uses VENV_PYTHON:** The supervisor already uses the venv python directly (not `uv run`). Keep this pattern when updating launch paths.
