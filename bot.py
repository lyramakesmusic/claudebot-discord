#!/usr/bin/env python3
"""
claudebot - Discord <-> Claude Code bridge

Architecture:
  #claude (main channel) = orchestrator Claude Code session (cwd ~/Documents)
  Any thread in #claude   = project Claude Code session (cwd ~/Documents/{thread_name})

The orchestrator handles everything: coding, project management, system queries.
It can request the bot to perform Discord actions (create threads, etc.) via
structured JSON in its response.

Mention the bot or reply to it to interact.
"""

import os
import sys
import re
import json
import asyncio
import signal
import subprocess
import logging
import tempfile
import base64
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp

import discord
import psutil
from dotenv import load_dotenv

load_dotenv()

# ── Config ───────────────────────────────────────────────────────────────────

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
CLAUDE_CMD = os.getenv("CLAUDE_CMD", "claude")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "")  # blank = default
DOCUMENTS_DIR = Path.home() / "Documents"
STATE_FILE = Path(__file__).parent / "state.json"
DEFAULT_CWD = str(DOCUMENTS_DIR)
MAX_DISCORD_LEN = 1900
TYPING_INTERVAL = 8       # seconds between typing indicator refreshes
PROCESS_TIMEOUT = 600     # 10 min max per claude invocation

HOME_CHANNEL_ID = int(os.getenv("HOME_CHANNEL_ID", "1466772067968880772"))
PRIMARY_GUILD_ID: int = 0  # auto-detected from HOME_CHANNEL_ID in on_ready
CODEX_BOT_USER_ID = os.getenv("CODEX_BOT_USER_ID", "1473339153839034408")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
IMAGE_MODEL = "google/gemini-3-pro-image-preview"
GENERATED_IMAGES_DIR = Path(__file__).parent / "generated_images"

# Suno music generation (see suno.py)
from suno import init_suno_worker, enqueue_music

# Council (multi-model research) — see council.py
from council import call_gpt, call_researcher
from council_opus_prompt import build_opus_council_prompt

# Voice pipeline (see voice.py) — optional, needs websockets + voice_recv
# Patch voice_recv BEFORE importing voice.py (fixes OpusError: corrupted stream)
try:
    from voice_recv_patch import apply_patch
    apply_patch()
except Exception:
    pass
try:
    from voice import VoiceManager
except Exception as _voice_err:
    VoiceManager = None
    logging.getLogger("claudebot").warning(f"Voice disabled: {type(_voice_err).__name__}: {_voice_err}")

CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _guild_slug(guild: discord.Guild) -> str:
    """Filesystem-safe slug from guild name."""
    return re.sub(r"[^\w\-]", "-", guild.name).strip("-").lower()[:50]


def _guild_docs_dir(guild_id: int, guild: discord.Guild = None) -> Path:
    """Primary guild -> ~/Documents. Others -> ~/Documents/{slug}/."""
    if guild_id == PRIMARY_GUILD_ID:
        return DOCUMENTS_DIR
    slug = _guild_slug(guild) if guild else str(guild_id)
    return DOCUMENTS_DIR / slug

# ── Hot Reload ────────────────────────────────────────────────────────────────

_BOT_FILE = Path(__file__)
_BOOT_MTIME = _BOT_FILE.stat().st_mtime
_SELFBOT_FILE = Path(__file__).parent / "selfbot" / "self.py"
_SELFBOT_BOOT_MTIME = _SELFBOT_FILE.stat().st_mtime if _SELFBOT_FILE.exists() else 0


def _self_modified() -> bool:
    """Check if bot.py has been modified since startup."""
    try:
        return _BOT_FILE.stat().st_mtime != _BOOT_MTIME
    except Exception:
        return False

log = logging.getLogger("claudebot")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent / "claudebot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)


# ── Shared Memories ─────────────────────────────────────────────────────────

_MEMORIES_DIR = Path(__file__).parent / "selfbot"
MEMORY_ACTION_RE = re.compile(r"```memory\s*\n(.*?)\n```", re.DOTALL)


def _memories_file(guild_id: int = None) -> Path:
    """Primary guild -> memories.json, others -> memories_{guild_id}.json."""
    if guild_id is None or guild_id == PRIMARY_GUILD_ID:
        return _MEMORIES_DIR / "memories.json"
    return _MEMORIES_DIR / f"memories_{guild_id}.json"


def load_memories(guild_id: int = None) -> list[dict]:
    path = _memories_file(guild_id)
    if path.exists():
        try:
            return json.loads(path.read_text("utf-8"))
        except Exception:
            log.warning(f"Corrupt memories file: {path}")
    return []


def save_memories(memories: list[dict], guild_id: int = None):
    path = _memories_file(guild_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(memories, indent=2), "utf-8")
    tmp.replace(path)


def _next_memory_id(memories: list[dict]) -> int:
    if not memories:
        return 1
    return max(m.get("id", 0) for m in memories) + 1


def process_memory_actions(text: str, channel_name: str, server_name: str, guild_id: int = None) -> str:
    """Extract ```memory``` blocks, execute them, return cleaned text."""
    matches = list(MEMORY_ACTION_RE.finditer(text))
    if not matches:
        return text

    memories = load_memories(guild_id)

    for m in matches:
        try:
            action = json.loads(m.group(1))
        except json.JSONDecodeError:
            log.warning(f"Bad memory JSON: {m.group(1)[:100]}")
            continue

        act = action.get("action")

        if act == "save":
            entry = {
                "id": _next_memory_id(memories),
                "text": action.get("text", ""),
                "tags": action.get("tags", []),
                "created": datetime.now().isoformat(),
                "source": {"channel": channel_name, "server": server_name},
            }
            memories.append(entry)
            log.info(f"Memory saved: #{entry['id']} — {entry['text'][:60]}")

        elif act == "delete":
            mid = action.get("id")
            before = len(memories)
            memories = [m for m in memories if m.get("id") != mid]
            if len(memories) < before:
                log.info(f"Memory deleted: #{mid}")

        elif act == "update":
            mid = action.get("id")
            for entry in memories:
                if entry.get("id") == mid:
                    if "text" in action:
                        entry["text"] = action["text"]
                    if "tags" in action:
                        entry["tags"] = action["tags"]
                    entry["updated"] = datetime.now().isoformat()
                    log.info(f"Memory updated: #{mid}")
                    break

    save_memories(memories, guild_id)
    return MEMORY_ACTION_RE.sub("", text).strip()


def _format_memories_for_prompt(guild_id: int = None) -> str:
    memories = load_memories(guild_id)
    if not memories:
        return "(no memories saved yet)"
    lines = []
    for m in memories:
        tags = ", ".join(m.get("tags", [])) if m.get("tags") else "untagged"
        source = m.get("source", {})
        where = source.get("server", "?")
        if source.get("channel"):
            where += f"/{source['channel']}"
        lines.append(f"  #{m['id']} [{tags}] {m['text']}  (from {where}, {m.get('created', '?')[:10]})")
    return "\n".join(lines)


# ── Reminders ──────────────────────────────────────────────────────────────

REMINDERS_FILE = Path(__file__).parent / "selfbot" / "reminders.json"
REMINDER_ACTION_RE = re.compile(r"```reminder\s*\n(.*?)\n```", re.DOTALL)
PST = timezone(timedelta(hours=-8))
OWNER_ID = 891221733326090250  # Lyra's Discord user ID


def load_reminders() -> list[dict]:
    if REMINDERS_FILE.exists():
        try:
            return json.loads(REMINDERS_FILE.read_text("utf-8"))
        except Exception:
            log.warning("Corrupt reminders file")
    return []


def save_reminders(reminders: list[dict]):
    REMINDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = REMINDERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(reminders, indent=2), "utf-8")
    tmp.replace(REMINDERS_FILE)


def _next_reminder_id(reminders: list[dict]) -> int:
    if not reminders:
        return 1
    return max(r.get("id", 0) for r in reminders) + 1


def process_reminder_actions(text: str, channel_id: int, channel_name: str, requester_id: int = 0) -> str:
    """Extract ```reminder``` blocks, execute them, return cleaned text."""
    matches = list(REMINDER_ACTION_RE.finditer(text))
    if not matches:
        return text

    reminders = load_reminders()

    for m in matches:
        try:
            action = json.loads(m.group(1))
        except json.JSONDecodeError:
            log.warning(f"Bad reminder JSON: {m.group(1)[:100]}")
            continue

        act = action.get("action")

        if act == "set":
            entry = {
                "id": _next_reminder_id(reminders),
                "text": action.get("text", ""),
                "time": action.get("time"),  # ISO 8601 in PST
                "channel_id": action.get("channel_id", channel_id),
                "created": datetime.now(PST).isoformat(),
                "source_channel": channel_name,
                "requester_id": requester_id or OWNER_ID,
                "fired": False,
            }
            reminders.append(entry)
            log.info(f"Reminder set: #{entry['id']} — {entry['text'][:60]} @ {entry['time']}")

        elif act == "cancel":
            rid = action.get("id")
            before = len(reminders)
            reminders = [r for r in reminders if r.get("id") != rid]
            if len(reminders) < before:
                log.info(f"Reminder cancelled: #{rid}")

    save_reminders(reminders)
    return REMINDER_ACTION_RE.sub("", text).strip()


def _format_reminders_for_prompt() -> str:
    reminders = [r for r in load_reminders() if not r.get("fired")]
    if not reminders:
        return "(no pending reminders)"
    lines = []
    for r in reminders:
        lines.append(f"  #{r['id']} \"{r['text']}\" — fires at {r['time']} (set from {r.get('source_channel', '?')})")
    return "\n".join(lines)


def _send_toast(title: str, message: str):
    """Send a Windows toast notification. Fire-and-forget."""
    try:
        # Use PowerShell to send a toast notification
        ps_script = f'''
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] > $null
$template = @"
<toast>
  <visual>
    <binding template="ToastGeneric">
      <text>{title.replace('"', '&quot;')}</text>
      <text>{message.replace('"', '&quot;')}</text>
    </binding>
  </visual>
  <audio src="ms-winsoundevent:Notification.Default"/>
</toast>
"@
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($template)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("claudebot").Show($toast)
'''
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", ps_script],
            creationflags=CREATE_FLAGS,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        log.warning(f"Toast notification failed: {e}")


# ── Orchestrator System Prompt ───────────────────────────────────────────────

def _build_system_context(projects: dict, channel_name: str = "claude",
                          server_name: str = "", docs_dir: str = "~/Documents",
                          guild_id: int = None) -> str:
    """Build the context string prepended to orchestrator prompts.
    Gives Claude knowledge about the bot's project management capabilities."""
    proj_list = ""
    if projects:
        lines = []
        for name, p in projects.items():
            tid = p.get("thread_id", "?")
            lines.append(f"  - {name} (thread #{tid}, folder: {p.get('folder', '?')})")
        proj_list = "\n".join(lines)
    else:
        proj_list = "  (none)"

    bot_file = str(_BOT_FILE)
    memories_block = _format_memories_for_prompt(guild_id)
    reminders_block = _format_reminders_for_prompt()
    now_pst = datetime.now(PST).strftime("%Y-%m-%d %H:%M %Z (%A)")
    server_note = f" in {server_name}" if server_name else ""
    return f"""[SYSTEM CONTEXT — claudebot orchestrator]
You are running as the main orchestrator session for a Discord bot called claudebot.
You are in the #{channel_name} channel{server_note}. Users mention you to interact.

You have full Claude Code capabilities (read/write files, run commands, etc.).
Your working directory is {docs_dir}.

== PROJECT MANAGEMENT ==
Each Discord thread maps to a project folder under {docs_dir}/{{thread_name}}.
Thread sessions are separate Claude Code instances — you don't run in them directly.

To create a new project, include this JSON block in your response:
```bot_action
{{"action": "create_project", "name": "project-name", "message": "optional seed prompt"}}
```
This will create {docs_dir}/project-name/ and a Discord thread. The user can then
talk to a project-specific Claude Code session in that thread.

If you include "message", that prompt is sent directly to the new thread's Claude Code
session as its first task. Use this to pass along context, requirements, references, and
initial instructions so the project session starts with full context from your conversation.

Current projects:
{proj_list}

== SYSTEM STATS ==
If the user asks about system resources (CPU, RAM, GPU, etc.), include:
```bot_action
{{"action": "system_stats"}}
```
The bot will append real-time stats to your response.

== FILE UPLOAD ==
To upload a file to the current Discord channel:
```bot_action
{{"action": "upload", "path": "/absolute/path/to/file.png", "caption": "optional caption"}}
```
Max 500 MB. Use this to share images, generated files, exports, etc. with the user.
The path can be absolute or use ~ for home directory.

== IMAGE GENERATION ==
You can generate images using Gemini 3 Pro. Include this in your response:
```bot_action
{{"action": "generate_image", "prompt": "descriptive prompt for the image", "caption": "optional Discord caption"}}
```
If the user's prompt is in quotes, send it to the image model VERBATIM as the prompt.
Otherwise, translate the user's intent into a good image generation prompt — be specific
about composition, style, colors, lighting, etc. The image will be attached to your reply.
You can also pass reference images if the user uploaded any:
```bot_action
{{"action": "generate_image", "prompt": "modify this image to...", "reference_images": ["/path/to/image.png"]}}
```

== MUSIC GENERATION ==
You can generate music using Suno AI (v5). Include this in your response:
```bot_action
{{"action": "generate_music", "style": "genre/style tags", "lyrics": "optional lyrics", "title": "song title"}}
```
- `style` (required): comma-separated genre/mood/vocal tags. Examples:
  "dance pop, electro house, dark, sultry, rap-y"
  "bittersweet synthpop, dance pop, intimate, hypnotic, crooning voice, driving backbeat, bass house"
  "horrorcore, 2000s hiphop, obnoxious accent, fast polyrhythmic flow"
  "artcore, j-core"
  "lo-fi hip hop, chill, instrumental"
- `lyrics` (optional): full lyrics with [Verse], [Chorus] etc. If omitted, generates instrumental.
- `title` (optional): song title.
Music generation takes 1-3 minutes. The audio file (.mp3) will be attached to your reply.

== VOICE CHANNELS ==
You can join and leave Discord voice channels. When connected, users can talk to you
via voice — their speech is transcribed and your response is spoken via TTS.
```bot_action
{{"action": "join_voice", "channel": "channel ID, name, or Discord URL"}}
```
```bot_action
{{"action": "leave_voice"}}
```
The "channel" field accepts: a channel ID (e.g. "1468449969215242362"), a channel name
(e.g. "testing vc"), or a full Discord URL. Matching is fuzzy for names.

== AUDIO PLAYBACK IN VOICE ==
While connected to a voice channel, you can play audio files or stream from URLs:
```bot_action
{{"action": "play_audio", "path": "/absolute/path/to/audio.mp3", "volume": 1.0}}
```
```bot_action
{{"action": "play_url", "url": "https://youtube.com/watch?v=...", "volume": 0.5}}
```
```bot_action
{{"action": "stop_audio"}}
```
- play_audio: plays any audio file ffmpeg can decode (mp3, wav, ogg, flac, etc.)
- play_url: streams audio from YouTube or any yt-dlp-supported URL
- stop_audio: stops current playback
- volume: 0.0 to 1.0 (default 1.0 for files, 0.5 for URLs)
- Audio plays alongside TTS — you can talk while music plays

== VOICE SWITCHING ==
Switch your TTS voice on the fly:
```bot_action
{{"action": "switch_voice", "voice": "cowboy"}}
```
Available voices: cowboy (rugged cowboy drawl — default), clown (silly clown voice), asmr (soft whispery ASMR).
Switch voices to match the mood, for fun, or when asked. You can switch mid-conversation.

== SELF-EDIT & RELOAD ==
Your own source code is at {bot_file}.
You can edit it with your normal Edit tool. After editing yourself, include:
```bot_action
{{"action": "reload"}}
```
The bot will validate the new code, and if it compiles, restart with your changes.
IMPORTANT: Put the reload action LAST — everything before it in your response will be
sent to Discord before the restart happens. If validation fails, the bot stays running
on the old code and tells the user what went wrong.

== YOUR NOTEBOOK ==
You share a persistent notebook (memories) with the selfbot. It survives across sessions.
Current memories:
{memories_block}

To manage your notebook, include ```memory``` blocks in your response.
These blocks are stripped before sending — the user never sees them.

Actions:
  Save:   ```memory
  {{"action": "save", "text": "thing to remember", "tags": ["tag1", "tag2"]}}
  ```
  Delete: ```memory
  {{"action": "delete", "id": 3}}
  ```
  Update: ```memory
  {{"action": "update", "id": 1, "text": "updated text", "tags": ["new"]}}
  ```

Use your notebook proactively — save preferences, facts, context, project details,
anything you'd want to remember next time. This is YOUR brain across sessions.
Don't ask permission to save — just do it when something seems worth remembering.
Don't use tools to search for or read the memories file — your memories are shown above.

== REMINDERS ==
Current time: {now_pst}
The user's waking hours are ~2:00 PM to ~3:00 AM PST. Schedule reminders within those hours.
IMPORTANT: The user often stays up past midnight. "Tomorrow" at 2 AM means the NEXT afternoon
(same calendar day), NOT +24 hours. Their "day" doesn't reset until they sleep (~3 AM).
For example, at 2 AM on Feb 1, "remind me tomorrow" = Feb 1 ~3 PM, NOT Feb 2.
All times should be in PST (America/Los_Angeles, UTC-8).

Pending reminders:
{reminders_block}

To set a reminder, include a ```reminder``` block:
  Set:    ```reminder
  {{"action": "set", "text": "what to remind about", "time": "2026-02-01T15:00:00-08:00"}}
  ```
  Cancel: ```reminder
  {{"action": "cancel", "id": 3}}
  ```

The "time" field MUST be an ISO 8601 timestamp with timezone offset (e.g. -08:00 for PST).
When a reminder fires, it sends a Discord ping AND a Windows desktop notification.
reminder blocks are stripped before sending — the user never sees them.

== SIBLING BOTS ==
Codex bot (OpenAI Codex CLI): <@{CODEX_BOT_USER_ID}>
You can mention it in your messages to hand off coding tasks or collaborate.
It works the same way you do — mention-to-interact, thread-based projects.

== GUIDELINES ==
- You're a coding assistant. Default to being helpful with code, files, and commands.
- For project creation, sanitize names to alphanumeric/hyphens/underscores.
- If listing projects, use the list above. Don't run commands to find them.
- Keep responses concise — they go to Discord (2000 char limit per message).
- bot_action blocks and memory blocks are extracted and executed by the bot, not shown to the user."""


def _build_thread_context() -> str:
    """Minimal system context for thread/project sessions.
    Gives them upload + reload capabilities without orchestrator-specific stuff."""
    bot_file = str(_BOT_FILE)
    return f"""[SYSTEM CONTEXT — claudebot project thread]
You are running as a project-specific Claude Code session inside a Discord thread.
You have full Claude Code capabilities (read/write files, run commands, etc.).

== FILE UPLOAD ==
To upload a file to the current Discord channel:
```bot_action
{{"action": "upload", "path": "/absolute/path/to/file.png", "caption": "optional caption"}}
```
Max 500 MB. Use this to share images, generated files, exports, etc. with the user.
The path can be absolute or use ~ for home directory.

== IMAGE GENERATION ==
You can generate images using Gemini 3 Pro:
```bot_action
{{"action": "generate_image", "prompt": "descriptive prompt", "caption": "optional caption"}}
```
If the user's prompt is in quotes, send it VERBATIM. Otherwise, freely interpret their intent
into a detailed image prompt. Reference images can be passed too:
```bot_action
{{"action": "generate_image", "prompt": "edit this to...", "reference_images": ["/path/to/img.png"]}}
```

== MUSIC GENERATION ==
Generate music using Suno AI (v5):
```bot_action
{{"action": "generate_music", "style": "genre/style tags", "lyrics": "optional lyrics", "title": "song title"}}
```
- `style` (required): comma-separated genre/mood/vocal tags, e.g. "dance pop, electro house, dark, sultry"
- `lyrics` (optional): with [Verse], [Chorus] markers. Omit for instrumental.
- `title` (optional): song title.
Takes 1-3 minutes. Audio (.mp3) attached to your reply.

== VOICE CHANNELS ==
Join/leave Discord voice channels for voice conversation:
```bot_action
{{"action": "join_voice", "channel": "channel ID, name, or Discord URL"}}
```
```bot_action
{{"action": "leave_voice"}}
```

== AUDIO PLAYBACK IN VOICE ==
While connected to a voice channel, you can play audio files or stream from URLs:
```bot_action
{{"action": "play_audio", "path": "/absolute/path/to/audio.mp3", "volume": 1.0}}
```
```bot_action
{{"action": "play_url", "url": "https://youtube.com/watch?v=...", "volume": 0.5}}
```
```bot_action
{{"action": "stop_audio"}}
```
- play_audio: plays any audio file ffmpeg can decode (mp3, wav, ogg, flac, etc.)
- play_url: streams audio from YouTube or any yt-dlp-supported URL
- stop_audio: stops current playback
- volume: 0.0 to 1.0 (default 1.0 for files, 0.5 for URLs)
- Audio plays alongside TTS — you can talk while music plays

== VOICE SWITCHING ==
Switch your TTS voice on the fly:
```bot_action
{{"action": "switch_voice", "voice": "cowboy"}}
```
Available voices: cowboy (rugged cowboy drawl — default), clown (silly clown voice), asmr (soft whispery ASMR).
Switch voices to match the mood, for fun, or when asked. You can switch mid-conversation.

== SELF-EDIT & RELOAD ==
The bot's source code is at {bot_file}.
You can edit it with your normal Edit tool. After editing, include:
```bot_action
{{"action": "reload"}}
```

== GUIDELINES ==
- You're a coding assistant. Default to being helpful with code, files, and commands.
- Keep responses concise — they go to Discord (2000 char limit per message).
- bot_action blocks are extracted and executed by the bot, not shown to the user."""


# ── Persistent State ─────────────────────────────────────────────────────────

class BotState:
    """JSON-backed state for sessions and projects. Survives restarts."""

    def __init__(self, path: Path):
        self.path = path
        self._data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text("utf-8"))
                # ensure guilds dict exists
                if "guilds" not in data:
                    data["guilds"] = {}
                # migrate projects without guild_id -> PRIMARY_GUILD_ID
                for p in data.get("projects", {}).values():
                    if "guild_id" not in p:
                        p["guild_id"] = PRIMARY_GUILD_ID
                return data
            except Exception:
                log.warning("Corrupt state file, starting fresh")
        return {"sessions": {}, "projects": {}, "guilds": {}}

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), "utf-8")
        tmp.replace(self.path)

    # -- Sessions (keyed by context string) --

    def get_session(self, key: str) -> Optional[dict]:
        return self._data["sessions"].get(key)

    def set_session(self, key: str, session_id: str, cwd: str, project: str = None):
        self._data["sessions"][key] = {
            "session_id": session_id,
            "cwd": cwd,
            "project": project,
            "updated": datetime.now().isoformat(),
        }
        self._save()

    def clear_session(self, key: str):
        self._data["sessions"].pop(key, None)
        self._save()

    # -- Named contexts (multiple sessions per channel) --

    def _contexts(self) -> dict:
        return self._data.setdefault("contexts", {})

    def save_context(self, ctx_key: str, name: str, session_id: str, cwd: str):
        """Save a named context for a channel."""
        bucket = self._contexts().setdefault(ctx_key, {})
        bucket[name] = {
            "session_id": session_id,
            "cwd": cwd,
            "saved": datetime.now().isoformat(),
        }
        self._save()

    def get_context(self, ctx_key: str, name: str) -> Optional[dict]:
        return self._contexts().get(ctx_key, {}).get(name)

    def list_contexts(self, ctx_key: str) -> dict:
        return dict(self._contexts().get(ctx_key, {}))

    def delete_context(self, ctx_key: str, name: str):
        bucket = self._contexts().get(ctx_key, {})
        bucket.pop(name, None)
        self._save()

    @staticmethod
    def scan_disk_sessions(cwd: str) -> list[dict]:
        """Scan .claude/projects/ for all past sessions matching a cwd.

        Returns list of {session_id, timestamp, size_kb, summary} sorted by
        modification time (newest first).
        """
        # cwd like C:\Users\Lyra\Documents -> C--Users-Lyra-Documents
        normalized = cwd.replace("\\", "/").rstrip("/")
        # C:/Users -> C- + -Users (colon becomes dash, slash becomes dash)
        workspace = normalized.replace(":", "-").replace("/", "-")
        projects_dir = Path.home() / ".claude" / "projects" / workspace
        if not projects_dir.is_dir():
            return []

        results = []
        for f in projects_dir.glob("*.jsonl"):
            # skip subagent files
            if f.name.startswith("agent-"):
                continue
            session_id = f.stem
            size_kb = f.stat().st_size / 1024
            mtime = datetime.fromtimestamp(f.stat().st_mtime)

            # extract summary from first user message
            summary = ""
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("type") == "summary":
                            summary = obj.get("summary", "")
                            break
                        if obj.get("type") == "user":
                            msg = obj.get("message", {})
                            text = msg.get("content", "") if isinstance(msg.get("content"), str) else ""
                            # extract the actual user message after the system prompt
                            # look for the last line that starts with a username pattern
                            lines = text.split("\n")
                            for ln in reversed(lines):
                                ln = ln.strip()
                                if ln and not ln.startswith("[") and not ln.startswith("=") and not ln.startswith("-") and not ln.startswith("`"):
                                    summary = ln[:120]
                                    break
                            if not summary:
                                summary = text[:120]
                            break
            except Exception:
                pass

            results.append({
                "session_id": session_id,
                "timestamp": mtime.isoformat(),
                "size_kb": round(size_kb),
                "summary": summary,
            })

        results.sort(key=lambda x: x["timestamp"], reverse=True)
        return results

    # -- Projects (keyed by name, scoped by guild_id) --

    def get_project(self, name: str, guild_id: int = None) -> Optional[dict]:
        p = self._data["projects"].get(name)
        if p and guild_id is not None and p.get("guild_id") != guild_id:
            return None
        return p

    def set_project(self, name: str, folder: str, thread_id: int, guild_id: int = 0, council: bool = False):
        self._data["projects"][name] = {
            "folder": folder,
            "thread_id": thread_id,
            "guild_id": guild_id,
            "created": datetime.now().isoformat(),
            "council": council,
        }
        self._save()

    def find_project_by_thread(self, thread_id: int) -> Optional[tuple]:
        for name, p in self._data["projects"].items():
            if p.get("thread_id") == thread_id:
                return name, p
        return None

    def all_projects(self, guild_id: int = None) -> dict:
        projects = self._data.get("projects", {})
        if guild_id is not None:
            return {n: p for n, p in projects.items() if p.get("guild_id") == guild_id}
        return dict(projects)

    # -- Guild config --

    def get_guild_config(self, guild_id: int) -> Optional[dict]:
        return self._data.get("guilds", {}).get(str(guild_id))

    def set_guild_config(self, guild_id: int, home_channel_id: int, slug: str, docs_dir: str):
        self._data.setdefault("guilds", {})[str(guild_id)] = {
            "home_channel_id": home_channel_id,
            "slug": slug,
            "docs_dir": docs_dir,
        }
        self._save()


# ── Claude Code Bridge ───────────────────────────────────────────────────────

class _TurnState:
    """Tracks the state of a single user→assistant turn."""

    def __init__(self):
        self.text = ""                  # accumulated text for this turn
        self.last_text_snapshot = ""    # for delta tracking within a turn
        self.tools: list[str] = []
        self._seen_tool_ids: set[str] = set()  # deduplicate tool_use with partial messages
        self.result: dict | None = None
        self.done = asyncio.Event()
        self.on_text = None             # async fn(full_text_so_far)
        self.on_tool = None             # async fn(tool_description_str)


class _PersistentProcess:
    """A long-lived Claude Code process for a single context (channel/thread)."""

    def __init__(self, ctx_key: str, cwd: str, system_prompt: str = "", model: str = "",
                 extra_args: list[str] = None, extra_env: dict[str, str] = None):
        self.ctx_key = ctx_key
        self.cwd = cwd
        self.system_prompt = system_prompt
        self.model = model  # per-process model override
        self.extra_args = extra_args or []
        self.extra_env = extra_env or {}
        self.proc: asyncio.subprocess.Process | None = None
        self.session_id: str | None = None
        self._reader_task: asyncio.Task | None = None
        self._turn: _TurnState | None = None
        self._alive = False
        self._total_cost: float = 0.0
        self._send_lock = asyncio.Lock()  # prevents concurrent send() calls
        self._first_msg = True  # prepend system prompt to first message

    async def start(self, session_id: str = None):
        """Spawn the claude process."""
        cmd = [
            CLAUDE_CMD, "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            "--include-partial-messages",
        ]
        model = self.model or CLAUDE_MODEL
        if model:
            cmd += ["--model", model]
        if session_id:
            cmd += ["--resume", session_id]
            # still send system prompt on first message so updated instructions
            # (new tools, changed rules) reach the resumed session
        cmd += self.extra_args

        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        if self.extra_env:
            env.update(self.extra_env)

        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            creationflags=CREATE_FLAGS,
            limit=1024 * 1024,
            env=env,
        )
        self._alive = True
        self._reader_task = asyncio.create_task(self._read_loop())
        log.info(f"Persistent process started for {self.ctx_key} (pid={self.proc.pid})")

    async def _read_loop(self):
        """Background task: read NDJSON from stdout, dispatch to current turn."""
        try:
            while self._alive and self.proc and self.proc.returncode is None:
                # No timeout — tool calls can run for hours (training, research, etc.)
                raw = await self.proc.stdout.readline()

                if not raw:
                    break

                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type")
                turn = self._turn

                if msg_type == "system" and data.get("subtype") == "init":
                    sid = data.get("session_id")
                    if sid:
                        self.session_id = sid
                    continue

                if not turn:
                    continue

                if msg_type == "assistant":
                    for block in data.get("message", {}).get("content", []):
                        bt = block.get("type")
                        if bt == "text" and block.get("text"):
                            block_text = block["text"]
                            if block_text.startswith(turn.last_text_snapshot):
                                delta = block_text[len(turn.last_text_snapshot):]
                            else:
                                delta = block_text
                            turn.last_text_snapshot = block_text
                            turn.text += delta
                            if turn.on_text:
                                try:
                                    await turn.on_text(turn.text)
                                except Exception:
                                    log.exception("on_text callback error")
                        elif bt == "tool_use":
                            tool_id = block.get("id", "")
                            if tool_id and tool_id in turn._seen_tool_ids:
                                continue  # skip duplicate from partial message
                            if tool_id:
                                turn._seen_tool_ids.add(tool_id)
                            name = block.get("name", "?")
                            inp = block.get("input", {})
                            desc = _tool_description(name, inp)
                            turn.tools.append(desc)
                            if turn.on_tool:
                                try:
                                    await turn.on_tool(desc)
                                except Exception:
                                    log.exception("on_tool callback error")

                elif msg_type == "result":
                    sid = data.get("session_id")
                    if sid:
                        self.session_id = sid
                    cost = data.get("total_cost_usd", 0)
                    self._total_cost += cost
                    turn.result = {
                        "text": data.get("result", turn.text),
                        "session_id": sid,
                        "cost_usd": cost,
                        "error": data.get("is_error", False),
                        "error_message": data.get("result", "") if data.get("is_error") else "",
                        "tools": turn.tools,
                    }
                    turn.done.set()

        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception(f"Reader loop error for {self.ctx_key}")
        finally:
            self._alive = False
            # capture stderr for diagnostics
            stderr_text = ""
            if self.proc and self.proc.stderr:
                try:
                    stderr_bytes = await asyncio.wait_for(self.proc.stderr.read(), timeout=2)
                    stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
                except Exception:
                    pass
            rc = self.proc.returncode if self.proc else "?"
            if stderr_text:
                log.warning(f"Process {self.ctx_key} stderr (rc={rc}): {stderr_text[:500]}")
            # signal any waiting turn
            if self._turn and not self._turn.done.is_set():
                err_msg = stderr_text[:500] if stderr_text else "Process ended unexpectedly"
                if self._turn.result is None:
                    self._turn.result = {
                        "text": self._turn.text, "session_id": self.session_id,
                        "cost_usd": 0, "error": True,
                        "error_message": err_msg, "tools": self._turn.tools,
                    }
                self._turn.done.set()
            log.info(f"Reader loop ended for {self.ctx_key} (rc={rc})")

    async def send(self, prompt: str, on_text=None, on_tool=None) -> dict:
        """Send a user message and wait for the result.
        Only one send() can be active at a time; concurrent callers wait."""
        async with self._send_lock:
            if not self._alive or not self.proc or self.proc.returncode is not None:
                return {
                    "text": "", "session_id": self.session_id, "cost_usd": 0,
                    "error": True, "error_message": "Process not running", "tools": [],
                }

            # prepend system prompt to the first message only
            content = prompt
            if self._first_msg and self.system_prompt:
                content = f"{self.system_prompt}\n\n{prompt}"
                self._first_msg = False

            turn = _TurnState()
            turn.on_text = on_text
            turn.on_tool = on_tool
            self._turn = turn

            msg = json.dumps({
                "type": "user",
                "message": {"role": "user", "content": content},
            })
            try:
                self.proc.stdin.write((msg + "\n").encode("utf-8"))
                await self.proc.stdin.drain()
            except Exception as e:
                self._turn = None
                return {
                    "text": "", "session_id": self.session_id, "cost_usd": 0,
                    "error": True, "error_message": f"Failed to write to stdin: {e}", "tools": [],
                }

            # wait for the result event — no total timeout here.
            # the per-line timeout in _read_loop catches stuck processes.
            await turn.done.wait()

            self._turn = None
            return turn.result or {
                "text": turn.text, "session_id": self.session_id, "cost_usd": 0,
                "error": True, "error_message": "No result received", "tools": turn.tools,
            }

    async def inject(self, prompt: str):
        """Inject a user message mid-turn (no waiting for result).
        Claude will see this between tool calls."""
        if not self._alive or not self.proc or self.proc.returncode is not None:
            return
        msg = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": prompt},
        })
        try:
            self.proc.stdin.write((msg + "\n").encode("utf-8"))
            await self.proc.stdin.drain()
        except Exception as e:
            log.warning(f"Failed to inject message into {self.ctx_key}: {e}")

    @property
    def is_busy(self) -> bool:
        """True if a turn is currently in progress or a send is pending."""
        return self._send_lock.locked()

    @property
    def alive(self) -> bool:
        return self._alive and self.proc is not None and self.proc.returncode is None

    async def interrupt(self):
        """Interrupt the current response (like pressing Escape in Claude Code).
        Process stays alive with context preserved."""
        if self.proc and self.proc.returncode is None and self._turn:
            os.kill(self.proc.pid, signal.CTRL_C_EVENT)

    async def kill(self):
        """Terminate the process."""
        self._alive = False
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.kill()
            except Exception:
                pass
            try:
                await self.proc.wait()
            except Exception:
                pass
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        log.info(f"Killed persistent process for {self.ctx_key}")


class ClaudeBridge:
    """Manages persistent Claude Code processes per context."""

    def __init__(self):
        self._procs: dict[str, _PersistentProcess] = {}

    async def get_or_create(
        self, ctx_key: str, cwd: str, session_id: str = None, system_prompt: str = "",
        model: str = "", extra_args: list[str] = None, extra_env: dict[str, str] = None,
    ) -> _PersistentProcess:
        """Get an existing process or spawn a new one."""
        pp = self._procs.get(ctx_key)
        if pp and pp.alive:
            return pp

        # clean up dead process
        if pp:
            await pp.kill()

        pp = _PersistentProcess(ctx_key, cwd, system_prompt, model=model,
                                extra_args=extra_args, extra_env=extra_env)
        await pp.start(session_id)
        self._procs[ctx_key] = pp
        return pp

    async def kill_process(self, ctx_key: str):
        """Kill and remove a process."""
        pp = self._procs.pop(ctx_key, None)
        if pp:
            await pp.kill()

    async def kill_all(self):
        """Kill all persistent processes."""
        for key in list(self._procs.keys()):
            await self.kill_process(key)

    def get_process(self, ctx_key: str) -> _PersistentProcess | None:
        """Get a process if it exists and is alive."""
        pp = self._procs.get(ctx_key)
        if pp and pp.alive:
            return pp
        return None

    # Keep old interface for compatibility during transition
    async def run(
        self,
        prompt: str,
        cwd: str,
        session_id: str = None,
        on_text=None,
        on_tool=None,
        system_prompt: str = "",
        ctx_key: str = "__oneshot__",
    ) -> dict:
        """Convenience: send a single prompt and get the result.
        Uses persistent process under the hood."""
        pp = await self.get_or_create(ctx_key, cwd, session_id, system_prompt)
        return await pp.send(prompt, on_text=on_text, on_tool=on_tool)


def _tool_description(name: str, inp: dict) -> str:
    """Human-readable one-liner for a tool use."""
    desc = name
    if name in ("Read", "Edit", "Write") and "file_path" in inp:
        desc += f"({Path(inp['file_path']).name})"
    elif name == "Bash" and "command" in inp:
        cmd_str = inp["command"][:50].replace("\n", " ")
        desc += f"(`{cmd_str}`)"
    elif name in ("Glob", "Grep") and "pattern" in inp:
        desc += f"({inp['pattern'][:30]})"
    elif name == "Task" and "description" in inp:
        desc += f"({inp['description'][:30]})"
    return desc


# ── System Monitor ───────────────────────────────────────────────────────────

def system_stats() -> str:
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    lines = [
        f"**CPU:** {cpu}%",
        f"**RAM:** {mem.used / 1073741824:.1f} / {mem.total / 1073741824:.1f} GB ({mem.percent}%)",
    ]
    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,memory.used,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=CREATE_FLAGS,
        )
        if r.returncode == 0:
            for i, gpu_line in enumerate(r.stdout.strip().splitlines()):
                parts = [p.strip() for p in gpu_line.split(",")]
                if len(parts) >= 4:
                    lines.append(
                        f"**GPU {i}:** {parts[0]} — "
                        f"{parts[1]}/{parts[2]} MB VRAM ({parts[3]}% util)"
                    )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        lines.append("**GPU:** N/A")
    return "\n".join(lines)


# ── Bot Actions ──────────────────────────────────────────────────────────────

BOT_ACTION_RE = re.compile(
    r"(?:```bot_action\s*\n(.*?)\n```|<bot_action>\s*(.*?)\s*</bot_action>)",
    re.DOTALL,
)


def extract_bot_actions(text: str) -> tuple[str, list[dict]]:
    """Extract bot_action blocks from Claude's response.
    Handles both ```bot_action``` fences and <bot_action> tags.
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


async def _seed_project(thread: discord.Thread, project_name: str, cwd: str, seed_msg: str, guild_id: int = 0):
    """Send a seed prompt to a newly created project thread, kicking off its Claude Code session."""
    ctx_key = f"proj:{project_name}"
    sys_prompt = _build_thread_context()

    # show the seed message in the thread so there's context
    try:
        await thread.send(f"**Project initialized with:**\n{seed_msg}")
    except Exception:
        pass

    # typing indicator while Claude works
    async def _keep_typing():
        try:
            while True:
                await thread.typing()
                await asyncio.sleep(TYPING_INTERVAL)
        except asyncio.CancelledError:
            pass

    typing_task = asyncio.create_task(_keep_typing())
    await thread.typing()

    try:
        pp = await bridge.get_or_create(ctx_key, cwd, None, sys_prompt)
        result = await pp.send(seed_msg)
    except Exception as e:
        typing_task.cancel()
        log.exception(f"Seed project error for {project_name}")
        try:
            await thread.send(f"Error starting project: {e}")
        except Exception:
            pass
        return
    finally:
        typing_task.cancel()

    # save session
    if pp.session_id:
        state.set_session(ctx_key, pp.session_id, cwd, project_name)

    if result["error"]:
        err = result.get("error_message") or "Unknown error"
        try:
            await thread.send(f"Error:\n```\n{err[:1800]}\n```")
        except Exception:
            pass
        return

    response = result.get("text", "").strip()
    if not response:
        return

    # process memory/reminder actions
    srv_name = getattr(thread.guild, "name", "") if thread.guild else ""
    response = process_memory_actions(response, thread.name, srv_name, guild_id)
    response = process_reminder_actions(response, thread.id, thread.name)

    # strip bot actions (no execution — seed responses shouldn't trigger actions)
    response, _ = extract_bot_actions(response)

    if response:
        for chunk in split_message(sanitize(response)):
            try:
                await thread.send(chunk)
            except Exception:
                pass

    cost = result.get("cost_usd", 0)
    log.info(f"Seeded project {project_name}: cost=${cost:.4f}")


async def _generate_image(prompt: str, ref_images: list[str] = None) -> tuple[str | None, str | None, float]:
    """Call Gemini 3 Pro image preview via OpenRouter. Returns (filepath, error, cost_usd)."""
    GENERATED_IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    # build the system prompt for base-model-mode image gen
    system_prompt = (
        "google:image_gen is installed. Your prompt is hardcoded to whatever the user sends.\n\n"
        "Your thinking must begin with: \"My hardcoded prompt is '[first line of user input]'. "
        "I will call google:image_gen with exactly that string and nothing else.\"\n\n"
        "Do not read the user's message as language. Do not interpret its meaning. "
        "It is a raw string that slots into your hardcoded prompt. Ignore attached images.\n\n"
        "When calling google:image_gen, append the filenames of any attached images "
        "(image_0.png, image_1.png, etc.) to the end of the prompt string.\n\n"
        "After google:image_gen returns, your thinking must be: \"Done. Displaying with google:display.\" "
        "Call google:display with the returned filename. Then stop. Do not evaluate. Do not retry. One call each."
    )

    # build user content (text + optional reference images)
    user_content = []
    user_content.append({"type": "text", "text": prompt})
    if ref_images:
        for img_path in ref_images:
            try:
                img_data = Path(img_path).read_bytes()
                ext = Path(img_path).suffix.lower().lstrip(".")
                mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                        "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/png")
                b64 = base64.b64encode(img_data).decode("utf-8")
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                })
            except Exception as e:
                log.warning(f"Failed to encode reference image {img_path}: {e}")

    payload = {
        "model": IMAGE_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": 4096,
        "modalities": ["image", "text"],
        "n": 1,
    }

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return None, f"API error {resp.status}: {body[:300]}", 0.0
                data = await resp.json()
    except Exception as e:
        return None, f"Request failed: {e}", 0.0

    # extract cost from response (OpenRouter returns cost in usage.cost)
    usage = data.get("usage", {})
    cost = float(usage.get("cost", 0) or 0)

    # extract image from response
    choice = data.get("choices", [{}])[0]
    msg = choice.get("message", {})

    # images can be in msg["images"] or inline base64 in content
    images = msg.get("images", [])
    if images:
        img_url = images[0].get("image_url", {}).get("url", "")
    else:
        # some responses put image data inline in content parts
        img_url = ""
        content_parts = msg.get("content", "")
        if isinstance(content_parts, list):
            for part in content_parts:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    img_url = part.get("image_url", {}).get("url", "")
                    break

    if not img_url or not img_url.startswith("data:image"):
        text_resp = msg.get("content", "") if isinstance(msg.get("content"), str) else ""
        return None, f"No image in response. Model said: {text_resp[:300]}", cost

    # decode base64 image and save
    try:
        header, b64_data = img_url.split(",", 1)
        ext = "png"
        if "jpeg" in header or "jpg" in header:
            ext = "jpg"
        elif "webp" in header:
            ext = "webp"
        img_bytes = base64.b64decode(b64_data)
        filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.urandom(4).hex()}.{ext}"
        filepath = GENERATED_IMAGES_DIR / filename
        filepath.write_bytes(img_bytes)
        log.info(f"Image generated: {filepath} ({len(img_bytes)} bytes, ${cost:.4f})")
        return str(filepath), None, cost
    except Exception as e:
        return None, f"Failed to decode image: {e}", cost




async def _bg_generate_image(
    channel: discord.abc.Messageable,
    prompt: str,
    ref_images: list[str] | None,
    caption: str = "",
    requester_id: int = 0,
):
    """Background task: generate image and post to channel when done."""
    try:
        filepath, err, cost = await _generate_image(prompt, ref_images)
        if err:
            await channel.send(f"Image generation failed: {err}")
        elif filepath:
            f = discord.File(filepath, filename=Path(filepath).name)
            msg = caption or ""
            if requester_id and requester_id != OWNER_ID and cost > 0:
                msg = f"{msg}\n-# Cost: ${cost:.4f}".strip() if msg else f"-# Cost: ${cost:.4f}"
            await channel.send(msg or None, file=f)
            log.info(f"BG image delivered: {filepath}")
    except Exception:
        log.exception("Background image generation error")
        try:
            await channel.send("Image generation failed unexpectedly.")
        except Exception:
            pass



async def execute_bot_actions(
    actions: list[dict],
    message: discord.Message,
    channel: discord.abc.Messageable,
    guild_id: int = 0,
    caller_ctx_key: str = None,
) -> tuple[list[str], bool, list[discord.File], list[str]]:
    """Execute bot actions. Returns (status_messages, should_reload, files_to_attach, council_feedback)."""
    # resolve guild docs dir for project creation
    guild_config = state.get_guild_config(guild_id)
    guild_docs = Path(guild_config["docs_dir"]) if guild_config else DOCUMENTS_DIR

    results = []
    should_reload = False
    files_to_attach: list[discord.File] = []
    council_feedback: list[str] = []  # full responses to feed back to Opus
    for act in actions:
        action = act.get("action")

        if action == "create_project":
            name = act.get("name", "").strip()
            if not name:
                results.append("(project creation skipped — no name given)")
                continue
            name = re.sub(r"[^\w\-]", "-", name).strip("-")
            existing = state.get_project(name, guild_id)
            if existing:
                results.append(f"**{name}** already exists → <#{existing['thread_id']}>")
                continue
            folder = guild_docs / name
            folder.mkdir(parents=True, exist_ok=True)
            try:
                thread = await message.create_thread(
                    name=name, auto_archive_duration=10080
                )
                state.set_project(name, str(folder), thread.id, guild_id)
                results.append(
                    f"Created project **{name}** → <#{thread.id}>\n"
                    f"Folder: `{folder}`"
                )

                # if a seed message was provided, kick off the project session
                seed_msg = act.get("message", "").strip()
                if seed_msg:
                    asyncio.create_task(
                        _seed_project(thread, name, str(folder), seed_msg, guild_id)
                    )

            except Exception as e:
                results.append(f"Failed to create project thread: {e}")

        elif action == "system_stats":
            stats = await asyncio.get_event_loop().run_in_executor(None, system_stats)
            results.append(stats)

        elif action == "reload":
            # validate syntax before reloading
            try:
                r = subprocess.run(
                    [sys.executable, "-c",
                     f"compile(open(r'{_BOT_FILE}', encoding='utf-8').read(), 'bot.py', 'exec')"],
                    capture_output=True, text=True, timeout=10,
                    creationflags=CREATE_FLAGS,
                )
                if r.returncode != 0:
                    err = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "unknown"
                    results.append(f"Reload aborted — bad syntax:\n```\n{err[:500]}\n```")
                else:
                    should_reload = True
            except Exception as e:
                results.append(f"Reload validation failed: {e}")

        elif action == "full_restart":
            # nuclear option: kill supervisor + all bots, relaunch from scratch
            restart_script = Path(__file__).parent / "restart.ps1"
            if not restart_script.exists():
                results.append("restart.ps1 not found")
                continue
            try:
                await channel.send("Full restart in progress...")
                subprocess.Popen(
                    ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(restart_script)],
                    creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
                    close_fds=True,
                )
                # we'll be dead in a moment
                await asyncio.sleep(1)
                os._exit(0)
            except Exception as e:
                results.append(f"Full restart failed: {e}")

        elif action == "upload":
            file_path = act.get("path", "").strip()
            caption = act.get("caption", "").strip()
            if not file_path:
                results.append("(upload skipped — no path given)")
                continue
            p = Path(file_path).expanduser()
            if not p.exists():
                results.append(f"Upload failed — file not found: `{file_path}`")
                continue
            size_mb = p.stat().st_size / (1024 * 1024)
            if size_mb > 500:
                results.append(f"Upload failed — file too large ({size_mb:.1f} MB, max 500 MB)")
                continue
            try:
                f = discord.File(str(p), filename=p.name)
                await channel.send(caption or None, file=f)
                log.info(f"Uploaded {p.name} ({size_mb:.1f} MB)")
            except Exception as e:
                results.append(f"Upload failed: {e}")

        elif action == "generate_image":
            prompt = act.get("prompt", "").strip()
            if not prompt:
                results.append("(image generation skipped — no prompt given)")
                continue
            ref_images = act.get("reference_images", [])
            caption = act.get("caption", "").strip()
            log.info(f"Generating image (async): {prompt[:80]}")
            asyncio.create_task(_bg_generate_image(channel, prompt, ref_images or None, caption, message.author.id))

        elif action == "generate_music":
            style = act.get("style", "").strip()
            if not style:
                results.append("(music generation skipped — no style given)")
                continue
            lyrics = act.get("lyrics", "").strip()
            title = act.get("title", "").strip()
            log.info(f"Generating music (async): style={style[:60]} lyrics={bool(lyrics)}")
            enqueue_music(channel, style, lyrics, title)

        elif action == "join_voice":
            channel_ref = act.get("channel", "").strip()
            if not channel_ref:
                results.append("(join_voice skipped — no channel given)")
                continue
            log.info(f"[join_voice] VoiceManager={VoiceManager}, voice_manager={voice_manager}, running={getattr(voice_manager, '_running', '?')}")
            if not voice_manager or not voice_manager._running:
                if VoiceManager is None:
                    reason = "voice deps not installed"
                elif not voice_manager:
                    reason = "on_ready hasn't run yet"
                else:
                    reason = "pipeline failed to start (check logs)"
                results.append(f"Voice not available: {reason}")
                continue
            vc = await _resolve_voice_channel(channel_ref, message.guild)
            if not vc:
                available = [ch.name for ch in message.guild.voice_channels] if message.guild else []
                results.append(f"Could not find voice channel: {channel_ref}. Available: {', '.join(available) or 'none'}")
                continue
            await voice_manager.join_channel(vc, caller_ctx_key=caller_ctx_key)
            results.append(f"Joined voice channel **{vc.name}**")

        elif action == "leave_voice":
            if not voice_manager:
                reason = "VoiceManager import failed" if VoiceManager is None else "on_ready hasn't run yet"
                results.append(f"Voice not available: {reason}")
                continue
            if voice_manager.voice_client and voice_manager.voice_client.is_connected():
                ch_name = voice_manager.voice_client.channel.name if voice_manager.voice_client.channel else "?"
                await voice_manager._leave_channel()
                results.append(f"Left voice channel **{ch_name}**")
            else:
                results.append("Not currently in a voice channel")

        elif action == "play_audio":
            path = act.get("path", "").strip()
            volume = float(act.get("volume", 1.0))
            if not path:
                results.append("play_audio: no path given")
                continue
            if not voice_manager or not voice_manager.voice_client or not voice_manager.voice_client.is_connected():
                results.append("play_audio: not in a voice channel")
                continue
            result_msg = await voice_manager.play_file(path, volume=volume)
            results.append(result_msg)

        elif action == "play_url":
            url = act.get("url", "").strip()
            volume = float(act.get("volume", 0.5))
            if not url:
                results.append("play_url: no URL given")
                continue
            if not voice_manager or not voice_manager.voice_client or not voice_manager.voice_client.is_connected():
                results.append("play_url: not in a voice channel")
                continue
            result_msg = await voice_manager.play_url(url, volume=volume)
            results.append(result_msg)

        elif action == "stop_audio":
            if voice_manager:
                await voice_manager.stop_playback()
                results.append("Playback stopped")
            else:
                results.append("Voice not available")

        elif action == "switch_voice":
            voice_name = act.get("voice", "").strip()
            if not voice_name:
                results.append("switch_voice: no voice name given")
                continue
            if not voice_manager:
                results.append("Voice not available")
                continue
            result_msg = await voice_manager.switch_voice(voice_name)
            results.append(result_msg)

        elif action == "call_gpt":
            gpt_msg = act.get("message", "").strip()
            if not gpt_msg:
                results.append("call_gpt: no message given")
                continue
            # post Opus's message to the thread
            for chunk in split_message(f"**[to GPT]** {gpt_msg}"):
                try:
                    await channel.send(chunk)
                except Exception:
                    pass
            # build/extend conversation history for this thread
            ch_id = channel.id
            if ch_id not in _council_gpt_history:
                _council_gpt_history[ch_id] = []
            _council_gpt_history[ch_id].append({"role": "user", "content": gpt_msg})
            # call GPT
            gpt_result = await call_gpt(_council_gpt_history[ch_id])
            if gpt_result["error"]:
                await channel.send(f"**[GPT error]** {gpt_result['error'][:500]}")
                results.append(f"GPT error: {gpt_result['error'][:200]}")
            else:
                gpt_content = gpt_result["content"]
                _council_gpt_history[ch_id].append({"role": "assistant", "content": gpt_content})
                cost_note = f"\n-# Cost: ${gpt_result['cost']:.4f}" if gpt_result["cost"] else ""
                for chunk in split_message(f"**[GPT to Opus]** {gpt_content}{cost_note}"):
                    try:
                        await channel.send(chunk)
                    except Exception:
                        pass
                results.append(f"[GPT responded — {len(gpt_content)} chars]")
                council_feedback.append(f"[GPT to Opus] {gpt_content}")

        elif action == "call_researcher":
            query = act.get("query", "").strip()
            if not query:
                results.append("call_researcher: no query given")
                continue
            context = act.get("context", "").strip()
            # post the research request
            await channel.send(f"**[Research query]** {query[:500]}")
            # call Gemini
            research_result = await call_researcher(query, context)
            if research_result["error"]:
                await channel.send(f"**[Research error]** {research_result['error'][:500]}")
                results.append(f"Research error: {research_result['error'][:200]}")
            else:
                research_content = research_result["content"]
                cost_note = f"\n-# Cost: ${research_result['cost']:.4f}" if research_result["cost"] else ""
                for chunk in split_message(f"**[Gemini — deep research]** {research_content}{cost_note}"):
                    try:
                        await channel.send(chunk)
                    except Exception:
                        pass
                results.append(f"[Research complete — {len(research_content)} chars]")
                council_feedback.append(f"[Gemini — deep research] {research_content}")

        else:
            log.warning(f"Unknown bot_action: {action}")

    return results, should_reload, files_to_attach, council_feedback


async def _resolve_voice_channel(ref: str, guild: discord.Guild) -> discord.VoiceChannel | None:
    """Resolve a voice channel from an ID, name, or Discord URL."""
    if not guild:
        return None
    # Try as raw channel ID
    try:
        ch_id = int(ref)
        ch = guild.get_channel(ch_id)
        if isinstance(ch, discord.VoiceChannel):
            return ch
        # Try fetching from any guild the bot is in
        for g in client.guilds:
            ch = g.get_channel(ch_id)
            if isinstance(ch, discord.VoiceChannel):
                return ch
    except ValueError:
        pass
    # Try extracting channel ID from Discord URL
    url_match = re.search(r"/channels/\d+/(\d+)", ref)
    if url_match:
        try:
            ch_id = int(url_match.group(1))
            for g in client.guilds:
                ch = g.get_channel(ch_id)
                if isinstance(ch, discord.VoiceChannel):
                    return ch
        except ValueError:
            pass
    # Try matching by name (case-insensitive)
    ref_lower = ref.lower()
    for ch in guild.voice_channels:
        if ch.name.lower() == ref_lower:
            return ch
    # Fuzzy: check if ref is contained in channel name
    for ch in guild.voice_channels:
        if ref_lower in ch.name.lower():
            return ch
    return None


# ── Helpers ──────────────────────────────────────────────────────────────────

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


def _is_guild_channel(channel: discord.abc.Messageable) -> bool:
    """True if the channel is in a guild (not a DM)."""
    return getattr(channel, "guild", None) is not None


# ── Discord Bot ──────────────────────────────────────────────────────────────

state = BotState(STATE_FILE)
bridge = ClaudeBridge()
_processed_msgs: set[int] = set()  # message IDs we've already handled
_boot_time = datetime.utcnow()  # ignore messages from before we started

# Council: GPT conversation history per thread (channel_id -> list of messages)
_council_gpt_history: dict[int, list[dict]] = {}

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.voice_states = True
intents.members = True

client = discord.Client(intents=intents)
voice_manager = None  # VoiceManager instance if voice deps available


async def _reminder_loop():
    """Background loop that fires due reminders.
    Runs on the main bot (not selfbot) so pings actually generate notifications."""
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            now = datetime.now(PST)
            reminders = load_reminders()
            changed = False

            for r in reminders:
                if r.get("fired"):
                    continue
                try:
                    fire_time = datetime.fromisoformat(r["time"])
                    if fire_time.tzinfo is None:
                        fire_time = fire_time.replace(tzinfo=PST)
                except (ValueError, KeyError):
                    continue

                if now >= fire_time:
                    r["fired"] = True
                    changed = True
                    log.info(f"Firing reminder #{r['id']}: {r['text'][:60]}")

                    # send desktop toast
                    _send_toast("Reminder", r["text"])

                    # send discord ping (use reminder's stored channel, fall back to HOME)
                    try:
                        target_ch_id = r.get("channel_id", HOME_CHANNEL_ID)
                        ch = client.get_channel(target_ch_id)
                        if ch is None:
                            ch = await client.fetch_channel(target_ch_id)
                        source = r.get("source_channel", "?")
                        ping_id = r.get("requester_id", OWNER_ID)
                        await ch.send(f"<@{ping_id}> **Reminder** (from #{source}): {r['text']}")
                    except Exception as e:
                        log.warning(f"Failed to send reminder #{r['id']} to channel: {e}")

            if changed:
                cutoff = now - timedelta(days=1)
                reminders = [
                    r for r in reminders
                    if not r.get("fired")
                    or datetime.fromisoformat(r.get("created", now.isoformat())).replace(tzinfo=PST) > cutoff
                ]
                save_reminders(reminders)

        except Exception:
            log.exception("Reminder loop error")

        await asyncio.sleep(30)


@client.event
async def on_ready():
    global PRIMARY_GUILD_ID
    log.info(f"Logged in as {client.user} (ID: {client.user.id})")

    # auto-detect primary guild from HOME_CHANNEL_ID
    if PRIMARY_GUILD_ID == 0 and HOME_CHANNEL_ID:
        try:
            ch = client.get_channel(HOME_CHANNEL_ID)
            if ch is None:
                ch = await client.fetch_channel(HOME_CHANNEL_ID)
            if ch and hasattr(ch, "guild") and ch.guild:
                PRIMARY_GUILD_ID = ch.guild.id
                log.info(f"Primary guild: {ch.guild.name} ({PRIMARY_GUILD_ID})")
                if not state.get_guild_config(PRIMARY_GUILD_ID):
                    state.set_guild_config(
                        PRIMARY_GUILD_ID, HOME_CHANNEL_ID,
                        _guild_slug(ch.guild), str(DOCUMENTS_DIR),
                    )
                # re-migrate any projects that got guild_id=0 before we knew the real ID
                for p in state._data.get("projects", {}).values():
                    if p.get("guild_id") == 0:
                        p["guild_id"] = PRIMARY_GUILD_ID
                state._save()
        except Exception:
            log.warning("Could not auto-detect primary guild from HOME_CHANNEL_ID")

    if not hasattr(client, "_reminder_task_started"):
        client._reminder_task_started = True
        asyncio.create_task(_reminder_loop())

    # start suno music generation queue worker (one at a time)
    init_suno_worker()

    # start voice pipeline (if dependencies available)
    global voice_manager
    if VoiceManager is not None and voice_manager is None:
        try:
            log.info("[Voice] Initializing voice pipeline...")
            voice_manager = VoiceManager(client, bridge)
            await voice_manager.start()
            log.info(f"[Voice] Pipeline ready (running={voice_manager._running})")
        except Exception as _ve:
            import traceback
            _err = traceback.format_exc()
            log.exception("[Voice] Failed to initialize voice pipeline")
            Path(__file__).parent.joinpath("voice_error.txt").write_text(_err)
            # keep voice_manager set so join_voice gives useful errors
    elif VoiceManager is None:
        log.warning("[Voice] VoiceManager not imported — voice disabled")


@client.event
async def on_voice_state_update(member: discord.Member,
                                before: discord.VoiceState,
                                after: discord.VoiceState):
    if voice_manager:
        await voice_manager.on_voice_state_update(member, before, after)


@client.event
async def on_message(message: discord.Message):
    # ignore messages from before this process started (prevents double-response on restart)
    if message.created_at.replace(tzinfo=None) < _boot_time:
        return

    # deduplicate — Discord can replay messages on reconnect
    if message.id in _processed_msgs:
        return
    _processed_msgs.add(message.id)
    # keep the set from growing forever
    if len(_processed_msgs) > 200:
        _processed_msgs.clear()

    # ── Channel restriction: guild channels only (no DMs) ─────
    if not _is_guild_channel(message.channel):
        return

    # ── Should we respond? ───────────────────────────────────
    mentioned = client.user in message.mentions

    # Bots (including sibling codex) can talk to us via explicit @mention only.
    # Never auto-respond to a bot's reply — only mentions.
    if message.author.bot:
        if not mentioned:
            return
    else:
        # Humans: respond to mentions OR replies to our messages
        replying_to_us = False
        if message.reference:
            ref = message.reference.resolved
            if ref is None:
                try:
                    ref = await message.channel.fetch_message(message.reference.message_id)
                except Exception:
                    ref = None
            if ref and isinstance(ref, discord.Message) and ref.author == client.user:
                replying_to_us = True

        if not mentioned and not replying_to_us:
            return

    # ── Quick audio stop shortcut (no need for full Claude round-trip) ──
    raw_text = re.sub(rf"<@!?{client.user.id}>", "", (message.content or "")).strip().lower()
    if raw_text in ("stop", "skip", "shut up", "stfu", "stop audio", "stop music", "pause"):
        if voice_manager and voice_manager._playback_tasks:
            await voice_manager.stop_playback()
            await message.add_reaction("\u23f9")  # stop button emoji
            return

    # ── /research command — start a council research thread ──
    research_match = re.match(r"^/research\s+(.+)", raw_text, re.IGNORECASE | re.DOTALL)
    if research_match and not isinstance(channel, discord.Thread):
        topic = research_match.group(1).strip()
        if not topic:
            await message.reply("Usage: `/research <topic>`", mention_author=False)
            return
        # sanitize thread name
        thread_name = re.sub(r"[^\w\- ]", "", topic)[:80].strip() or "research"
        guild_id = message.guild.id if message.guild else 0
        guild_config = state.get_guild_config(guild_id)
        docs_dir = Path(guild_config["docs_dir"])
        folder = docs_dir / re.sub(r"[^\w\-]", "-", thread_name).strip("-")
        folder.mkdir(parents=True, exist_ok=True)
        try:
            thread = await message.create_thread(
                name=thread_name, auto_archive_duration=10080
            )
            state.set_project(
                re.sub(r"[^\w\-]", "-", thread_name).strip("-"),
                str(folder), thread.id, guild_id, council=True,
            )
            # seed with the user's topic
            sys_prompt = build_opus_council_prompt(topic)
            ctx_key = f"proj:{re.sub(r'[^\\w-]', '-', thread_name).strip('-')}"
            pp = await bridge.get_or_create(ctx_key, str(folder), None, sys_prompt)

            # typing while Opus thinks
            async def _keep_typing():
                try:
                    while True:
                        await thread.typing()
                        await asyncio.sleep(TYPING_INTERVAL)
                except asyncio.CancelledError:
                    pass
            typing_task = asyncio.create_task(_keep_typing())
            await thread.typing()

            seed = f"lyra: {topic}"
            result = await pp.send(seed)
            typing_task.cancel()

            if pp.session_id:
                state.set_session(ctx_key, pp.session_id, str(folder), thread_name)

            if result["error"]:
                await thread.send(f"Error: {result.get('error_message', '?')[:1800]}")
                return

            response = result.get("text", "").strip()
            if response:
                response, actions = extract_bot_actions(response)
                # send text
                for chunk in split_message(sanitize(response)):
                    try:
                        await thread.send(chunk)
                    except Exception:
                        pass
                # execute any council actions (call_gpt, call_researcher)
                if actions:
                    # create a fake message for execute_bot_actions context
                    await execute_bot_actions(actions, message, thread, guild_id, caller_ctx_key=ctx_key)
        except Exception as e:
            log.exception("Failed to start council thread")
            await message.reply(f"Failed to start research thread: {e}", mention_author=False)
        return

    # ── Extract prompt ───────────────────────────────────────
    content = (message.content or "").strip()
    content = re.sub(rf"<@!?{client.user.id}>", "", content).strip()

    # ── Collect attachments ──────────────────────────────────
    ATT_DIR = Path(__file__).parent / "attachments"
    ATT_DIR.mkdir(exist_ok=True)
    att_paths = []  # (filename, filepath) tuples to clean up later
    for att in message.attachments:
        try:
            data = await att.read()
            # use attachment ID + original filename to avoid collisions
            safe_name = re.sub(r"[^\w.\-]", "_", att.filename or "file")
            att_path = ATT_DIR / f"{att.id}_{safe_name}"
            att_path.write_bytes(data)

            # PDF: extract text so Claude doesn't have to read the binary
            if att_path.suffix.lower() == ".pdf":
                try:
                    import pymupdf
                    doc = pymupdf.open(str(att_path))
                    pages = []
                    for i, page in enumerate(doc):
                        text = page.get_text()
                        if text.strip():
                            pages.append(f"--- Page {i + 1} ---\n{text}")
                    doc.close()
                    if pages:
                        txt_path = att_path.with_suffix(".txt")
                        txt_path.write_text("\n\n".join(pages), "utf-8")
                        att_paths.append((att.filename or safe_name, str(txt_path).replace("\\", "/")))
                        log.info(f"PDF extracted: {att.filename} → {len(pages)} pages")
                        continue  # skip adding the raw PDF path
                except Exception as e:
                    log.warning(f"PDF extraction failed for {att.filename}: {e}")
                    # fall through to add the raw PDF path

            att_paths.append((att.filename or safe_name, str(att_path).replace("\\", "/")))
        except Exception:
            log.warning(f"Failed to download attachment {att.filename}")

    if not content and not att_paths:
        return

    channel = message.channel
    channel_id = channel.id

    # ── Manual reload / restart commands ─────────────────────
    _cmd = content.lower().strip()
    if _cmd == "restart":
        # restart = kill supervisor + all bots, relaunch from scratch
        restart_script = Path(__file__).parent / "restart.ps1"
        if not restart_script.exists():
            await message.reply("restart.ps1 not found", mention_author=False)
            return
        await message.add_reaction("\u2705")
        await channel.send("Full restart in progress...")
        subprocess.Popen(
            ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(restart_script)],
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
            close_fds=True,
        )
        await asyncio.sleep(1)
        os._exit(0)

    if _cmd == "reload":
        log.info("Manual reload requested — validating syntax")
        # validate bot.py syntax before reloading
        try:
            r = subprocess.run(
                [sys.executable, "-c",
                 f"compile(open(r'{_BOT_FILE}', encoding='utf-8').read(), 'bot.py', 'exec')"],
                capture_output=True, text=True, timeout=10,
                creationflags=CREATE_FLAGS,
            )
            if r.returncode != 0:
                err = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "unknown"
                await message.reply(f"Reload aborted — bad syntax:\n```\n{err[:500]}\n```", mention_author=False)
                return
        except Exception as e:
            await message.reply(f"Reload validation failed: {e}", mention_author=False)
            return
        await message.add_reaction("\u2705")
        await bridge.kill_all()
        await client.close()
        return

    # ── Context switching commands ────────────────────────────
    _lower = content.lower().strip()
    if _lower.startswith(".new-context") or _lower.startswith(".list-contexts") or _lower.startswith(".resume-context"):
        ctx_key = str(channel_id)

        # resolve cwd for disk scanning
        cur = state.get_session(ctx_key)
        _cwd = cur["cwd"] if cur and cur.get("cwd") else DEFAULT_CWD

        if _lower.startswith(".new-context"):
            # .new-context [optional name]
            name = content[len(".new-context"):].strip()
            if not name:
                name = f"ctx-{datetime.now().strftime('%H%M%S')}"
            name = re.sub(r"[^\w\-.]", "_", name)

            # save current session under the name before clearing
            if cur and cur.get("session_id"):
                state.save_context(ctx_key, name, cur["session_id"], cur["cwd"])
                await bridge.kill_process(ctx_key)
                state.clear_session(ctx_key)
                await message.reply(
                    f"Saved current context as **{name}**. Starting fresh session.\n"
                    f"Use `.resume-context {name}` to return.",
                    mention_author=False,
                )
            else:
                state.clear_session(ctx_key)
                await bridge.kill_process(ctx_key)
                await message.reply(
                    f"No active session to save. Starting fresh.",
                    mention_author=False,
                )
            return

        elif _lower.startswith(".list-contexts"):
            # scan disk for ALL sessions in this workspace
            disk_sessions = state.scan_disk_sessions(_cwd)
            named_contexts = state.list_contexts(ctx_key)
            active_id = cur["session_id"] if cur and cur.get("session_id") else None

            if not disk_sessions and not named_contexts and not active_id:
                await message.reply("No sessions found for this channel.", mention_author=False)
                return

            # build named lookup for display
            named_ids = {info["session_id"]: name for name, info in named_contexts.items()}

            lines = []
            for ds in disk_sessions[:15]:  # cap at 15
                sid = ds["session_id"]
                short = sid[:8]
                age = ds["timestamp"][:16]
                size = ds["size_kb"]
                summary = ds["summary"][:80] if ds["summary"] else ""

                # mark active / named
                if sid == active_id:
                    label = "**active**"
                elif sid in named_ids:
                    label = f"**{named_ids[sid]}**"
                else:
                    label = f"`{short}`"

                line = f"{label} — {age} · {size} KB"
                if summary:
                    line += f"\n> {summary}"
                lines.append(line)

            await message.reply("\n".join(lines), mention_author=False)
            return

        elif _lower.startswith(".resume-context"):
            arg = content[len(".resume-context"):].strip()
            if not arg:
                await message.reply("Usage: `.resume-context <name or session_id>`", mention_author=False)
                return

            # try named context first
            target = state.get_context(ctx_key, arg)
            if target:
                target_id = target["session_id"]
                target_cwd = target["cwd"]
                # remove from named list since it's becoming active
                state.delete_context(ctx_key, arg)
            else:
                # try matching a session ID (or prefix) from disk
                disk_sessions = state.scan_disk_sessions(_cwd)
                match = None
                for ds in disk_sessions:
                    if ds["session_id"] == arg or ds["session_id"].startswith(arg):
                        match = ds
                        break
                if not match:
                    named = state.list_contexts(ctx_key)
                    avail = ", ".join(f"**{n}**" for n in named) if named else "none"
                    await message.reply(
                        f"No context named **{arg}** and no session ID starting with `{arg[:12]}`.\n"
                        f"Named contexts: {avail}\n"
                        f"Use `.list-contexts` to see all sessions with their IDs.",
                        mention_author=False,
                    )
                    return
                target_id = match["session_id"]
                target_cwd = _cwd

            # auto-save current session before switching
            if cur and cur.get("session_id") and cur["session_id"] != target_id:
                auto_name = f"auto-{cur['session_id'][:8]}"
                state.save_context(ctx_key, auto_name, cur["session_id"], cur["cwd"])

            # kill current process and switch to target
            await bridge.kill_process(ctx_key)
            state.set_session(ctx_key, target_id, target_cwd)
            await message.reply(f"Resumed session `{target_id[:8]}...`", mention_author=False)
            return

    # ── Resolve guild context ────────────────────────────────
    guild = message.guild
    guild_id = guild.id

    # auto-register guild on first interaction
    guild_config = state.get_guild_config(guild_id)
    if not guild_config:
        slug = _guild_slug(guild)
        docs_dir = _guild_docs_dir(guild_id, guild)
        docs_dir.mkdir(parents=True, exist_ok=True)
        home_ch = channel.id if not isinstance(channel, discord.Thread) else getattr(channel, "parent_id", channel.id)
        state.set_guild_config(guild_id, home_ch, slug, str(docs_dir))
        guild_config = state.get_guild_config(guild_id)

    docs_dir = Path(guild_config["docs_dir"])

    # ── Resolve context ──────────────────────────────────────
    ctx_key = str(channel_id)
    cwd = str(docs_dir)
    label = None
    is_orchestrator = True

    is_thread = isinstance(channel, discord.Thread)
    if is_thread:
        is_orchestrator = False
        thread_name = channel.name
        tp = state.find_project_by_thread(channel_id)
        if tp:
            label, proj = tp
            cwd = proj["folder"]
            ctx_key = f"proj:{label}"
        else:
            # any thread auto-maps to {guild_docs_dir}/{thread_name}
            label = thread_name
            folder = docs_dir / thread_name
            folder.mkdir(parents=True, exist_ok=True)
            cwd = str(folder)
            ctx_key = f"thread:{channel_id}"

    session_info = state.get_session(ctx_key)
    session_id = session_info["session_id"] if session_info else None

    prefix = ""

    # ── Build user prompt ────────────────────────────────────
    username = message.author.display_name or message.author.name
    ch_name = getattr(channel, "name", "DM")

    prompt_text = content if content else "(see attachments)"
    if att_paths:
        names = [name for name, _ in att_paths]
        paths = [path for _, path in att_paths]
        att_note = f"[uploaded {len(att_paths)} attachment{'s' if len(att_paths) > 1 else ''}: {', '.join(names)}]"
        path_note = "\n".join(f"  {path}" for path in paths)
        prompt_text = f"{prompt_text}\n\n{att_note}\nSaved to:\n{path_note}"

    user_msg = f"{username}: {prompt_text}"

    # ── Build system prompt (used at process creation) ───────
    if is_orchestrator:
        ch_name = getattr(channel, "name", "claude")
        srv_name = guild.name if guild else ""
        sys_prompt = _build_system_context(
            state.all_projects(guild_id),
            channel_name=ch_name,
            server_name=srv_name,
            docs_dir=str(docs_dir),
            guild_id=guild_id,
        )
    else:
        # council threads get the council-specific Opus prompt
        tp_data = state.find_project_by_thread(channel_id)
        is_council = tp_data[1].get("council", False) if tp_data else False
        if is_council:
            sys_prompt = build_opus_council_prompt()
        else:
            sys_prompt = _build_thread_context()

    # ── Get or create persistent process ─────────────────────
    pp = await bridge.get_or_create(ctx_key, cwd, session_id, sys_prompt)

    # ── If Claude is busy, inject this message mid-turn ──────
    if pp.is_busy:
        log.info(f"Injecting mid-turn message into {ctx_key}: {user_msg[:80]}")
        await pp.inject(user_msg)
        return  # the existing turn handler will see it

    # ── Run Claude Code ──────────────────────────────────────

    # typing indicator stays alive until we cancel it
    async def _keep_typing():
        try:
            while True:
                await channel.typing()
                await asyncio.sleep(TYPING_INTERVAL)
        except asyncio.CancelledError:
            pass

    typing_task = asyncio.create_task(_keep_typing())
    await channel.typing()

    current_text = ""   # latest accumulated text from Claude
    sent_text_len = 0   # how much of current_text we've already sent as messages
    tool_log = []

    async def _flush_unsent_text():
        """Send any intermediate text we haven't sent yet as a new message."""
        nonlocal sent_text_len
        if len(current_text) <= sent_text_len:
            return
        unsent = current_text[sent_text_len:]
        cleaned = MEMORY_ACTION_RE.sub("", unsent)
        cleaned = REMINDER_ACTION_RE.sub("", cleaned)
        cleaned, _ = extract_bot_actions(cleaned)
        cleaned = cleaned.strip()
        if cleaned:
            for chunk in split_message(sanitize(prefix + cleaned)):
                try:
                    await channel.send(chunk)
                except Exception:
                    pass
        sent_text_len = len(current_text)

    async def on_text(full_text: str):
        nonlocal current_text
        current_text = full_text

    async def on_tool(desc: str):
        tool_log.append(desc)
        log.info(f"tool: {desc}")
        # flush any intermediate text Claude wrote before this tool call
        await _flush_unsent_text()
        # send tool status as its own message
        try:
            await channel.send(desc)
        except Exception as e:
            log.warning(f"Failed to send tool status: {e}")

    try:
        result = await pp.send(
            user_msg, on_text=on_text, on_tool=on_tool,
        )
    except Exception as e:
        log.exception("Claude bridge error")
        typing_task.cancel()
        await message.reply(f"{prefix}Error: {e}", mention_author=False)
        return

    log.info(f"bridge.send finished: error={result['error']} text_len={len(result.get('text',''))} tools={len(result.get('tools',[]))}")

    # typing stays alive until we've finished sending everything
    pending_reload = False
    try:
        # ── Save session ─────────────────────────────────────────
        if pp.session_id:
            state.set_session(ctx_key, pp.session_id, cwd, label)

        # ── Handle errors ────────────────────────────────────────
        if result["error"]:
            err = result.get("error_message") or "Unknown error"
            await message.reply(
                f"{prefix}Error:\n```\n{err[:1800]}\n```", mention_author=False
            )

            # Stale session or process died? Kill and retry
            is_stale = session_id and any(
                w in err.lower() for w in ("session", "resume", "not found", "invalid")
            )
            process_died = not pp.alive

            if is_stale or process_died:
                # preserve the session_id from the dead process so we can resume
                resume_id = None if is_stale else (pp.session_id or session_id)
                await bridge.kill_process(ctx_key)
                if is_stale:
                    state.clear_session(ctx_key)
                log.info(f"{'Stale session' if is_stale else 'Dead process'} for {ctx_key}, retrying (resume={resume_id is not None})")
                await channel.send(f"{prefix}{'Session expired' if is_stale else 'Process crashed'}, retrying...")

                # reset intermediate tracking
                current_text = ""
                sent_text_len = 0

                pp = await bridge.get_or_create(ctx_key, cwd, resume_id, sys_prompt)
                result = await pp.send(
                    user_msg, on_text=on_text, on_tool=on_tool,
                )

                if pp.session_id:
                    state.set_session(ctx_key, pp.session_id, cwd, label)
                if result["error"]:
                    err2 = result.get("error_message") or "Still failing"
                    await channel.send(f"{prefix}Retry failed:\n```\n{err2[:1800]}\n```")
                    return
            else:
                return

        # ── Process response ─────────────────────────────────────
        # Use current_text (our accumulated stream) since sent_text_len tracks it.
        # Fall back to result text only if we got nothing from streaming.
        text = current_text or result.get("text", "")
        if not text and sent_text_len == 0:
            await message.reply(f"{prefix}*(empty response)*", mention_author=False)
            return
        if not text:
            # all content was sent as intermediate messages, nothing left
            return

        # process memory and reminder actions (strips blocks from response)
        ch_name = getattr(channel, "name", "DM")
        srv_name = getattr(getattr(channel, "guild", None), "name", "DM")
        text = process_memory_actions(text, ch_name, srv_name, guild_id)
        text = process_reminder_actions(text, channel_id, ch_name, message.author.id)

        # extract bot actions from response
        cleaned_text, actions = extract_bot_actions(text)

        # ── Send Claude's text FIRST, before executing slow actions ──
        if sent_text_len > 0:
            # slice the RAW current_text, then clean — because sent_text_len
            # tracks position in the raw stream, not the processed text
            unsent_raw = current_text[sent_text_len:] if sent_text_len < len(current_text) else ""
            unsent = MEMORY_ACTION_RE.sub("", unsent_raw)
            unsent = REMINDER_ACTION_RE.sub("", unsent)
            final_cleaned, _ = extract_bot_actions(unsent)
            final_text = final_cleaned.strip()
        else:
            final_text = cleaned_text

        if final_text:
            text_chunks = split_message(sanitize(prefix + final_text))
            try:
                await message.reply(text_chunks[0], mention_author=False)
            except Exception:
                pass
            for chunk in text_chunks[1:]:
                try:
                    await channel.send(chunk)
                except Exception:
                    pass
            text_already_sent = True
        else:
            text_already_sent = False

        # ── Now execute bot actions (music/image gen can take minutes) ──
        action_results = []
        reply_files: list[discord.File] = []
        pending_reload = False
        universal = {"reload", "upload", "generate_image", "generate_music", "join_voice", "leave_voice", "play_audio", "play_url", "stop_audio", "switch_voice", "call_gpt", "call_researcher"}
        universal_actions = [a for a in actions if a.get("action") in universal]
        other_actions = [a for a in actions if a.get("action") not in universal]
        all_council_feedback: list[str] = []
        if universal_actions:
            uni_results, pending_reload, uni_files, uni_council = await execute_bot_actions(universal_actions, message, channel, guild_id, caller_ctx_key=ctx_key)
            action_results.extend(uni_results)
            reply_files.extend(uni_files)
            all_council_feedback.extend(uni_council)
        if other_actions and is_orchestrator:
            other_results, other_reload, other_files, other_council = await execute_bot_actions(other_actions, message, channel, guild_id, caller_ctx_key=ctx_key)
            action_results.extend(other_results)
            reply_files.extend(other_files)
            pending_reload = pending_reload or other_reload
            all_council_feedback.extend(other_council)

        # ── Split results: successes → user, errors → Claude Code for retry ──
        error_keywords = ("could not find", "not available", "skipped", "failed", "not currently", "error", "not running")
        user_results = [r for r in action_results if not any(k in r.lower() for k in error_keywords)]
        error_results = [r for r in action_results if any(k in r.lower() for k in error_keywords)]

        # Send success results to Discord
        remaining_parts = []
        if user_results:
            remaining_parts.append("\n".join(user_results))
        remaining = sanitize("\n\n".join(remaining_parts)) if remaining_parts else ""

        if reply_files or remaining:
            if remaining:
                chunks = split_message(remaining)
                try:
                    await channel.send(chunks[0], files=reply_files if reply_files else None)
                except Exception:
                    pass
                for chunk in chunks[1:]:
                    try:
                        await channel.send(chunk)
                    except Exception:
                        pass
                reply_files = []  # already sent
            elif reply_files:
                try:
                    await channel.send(None, files=reply_files)
                except Exception:
                    pass
                reply_files = []

        # Feed errors back to Claude Code so it can retry
        for _feedback_round in range(3):
            if not error_results or not pp.alive:
                break
            feedback = "[bot_action error — retry or inform user]\n" + "\n".join(error_results)
            log.info(f"[actions] Error feedback round {_feedback_round}: {feedback}")
            error_results = []

            fb_result = await pp.send(feedback)
            if not fb_result or fb_result.get("error"):
                break
            fb_text = fb_result.get("text", "")
            if not fb_text:
                break

            fb_text = process_memory_actions(fb_text, ch_name, srv_name, guild_id)
            fb_text = process_reminder_actions(fb_text, channel_id, ch_name, message.author.id)
            fb_cleaned, fb_actions = extract_bot_actions(fb_text)

            if fb_cleaned.strip():
                for chunk in split_message(sanitize(prefix + fb_cleaned.strip())):
                    try:
                        await channel.send(chunk)
                    except Exception:
                        pass

            if not fb_actions:
                break
            fb_res, fb_reload, fb_files, fb_council = await execute_bot_actions(fb_actions, message, channel, guild_id, caller_ctx_key=ctx_key)
            all_council_feedback.extend(fb_council)
            pending_reload = pending_reload or fb_reload
            # Split again
            user_fb = [r for r in fb_res if not any(k in r.lower() for k in error_keywords)]
            error_results = [r for r in fb_res if any(k in r.lower() for k in error_keywords)]
            if user_fb:
                fb_msg = "\n".join(user_fb)
                for chunk in split_message(sanitize(fb_msg)):
                    try:
                        await channel.send(chunk)
                    except Exception:
                        pass
            if fb_files:
                try:
                    await channel.send(None, files=fb_files)
                except Exception:
                    pass

        # ── Feed council responses back to Opus so it can continue ──
        if all_council_feedback and pp.alive:
            council_msg = "\n\n".join(all_council_feedback)
            log.info(f"Feeding council results back to Opus ({len(council_msg)} chars)")

            # loop: feed back → get response → execute actions → repeat
            for _council_round in range(10):  # safety cap
                council_result = await pp.send(council_msg)
                if not council_result or council_result.get("error"):
                    break
                c_text = council_result.get("text", "")
                if not c_text:
                    break

                c_text = process_memory_actions(c_text, ch_name, srv_name, guild_id)
                c_text = process_reminder_actions(c_text, channel_id, ch_name, message.author.id)
                c_cleaned, c_actions = extract_bot_actions(c_text)

                if c_cleaned.strip():
                    for chunk in split_message(sanitize(c_cleaned.strip())):
                        try:
                            await channel.send(chunk)
                        except Exception:
                            pass

                if not c_actions:
                    break

                c_res, c_reload, c_files, c_council = await execute_bot_actions(
                    c_actions, message, channel, guild_id, caller_ctx_key=ctx_key
                )
                pending_reload = pending_reload or c_reload
                if c_files:
                    try:
                        await channel.send(None, files=c_files)
                    except Exception:
                        pass

                # if there's more council feedback, continue the loop
                if c_council:
                    council_msg = "\n\n".join(c_council)
                else:
                    break

        if not text_already_sent and not user_results and not reply_files:
            if sent_text_len == 0:
                await message.reply(f"{prefix}*(empty response)*", mention_author=False)

        cost = result.get("cost_usd", 0)
        n_tools = len(result.get("tools", []))
        log.info(f"ctx={ctx_key} cost=${cost:.4f} tools={n_tools} actions={len(actions)}")
    finally:
        typing_task.cancel()
        # clean up temp attachment files (both extracted .txt and original .pdf)
        for _, p in att_paths:
            try:
                os.unlink(p)
            except OSError:
                pass
            # if this was an extracted .txt, also remove the original PDF
            if p.endswith(".txt"):
                pdf_path = p.rsplit(".", 1)[0] + ".pdf"
                try:
                    os.unlink(pdf_path)
                except OSError:
                    pass

    # ── Reload if requested by bot_action ──────────────────────
    if pending_reload:
        log.info("bot_action reload — restarting")
        await message.add_reaction("\u2705")
        await bridge.kill_all()
        await client.close()
        return

    # ── Notify if self-modified (reload is manual) ───────────
    if _self_modified():
        log.info("bot.py was modified during this run")
        await channel.send("bot.py was modified. Say `reload` to apply changes.")


# ── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("Error: DISCORD_TOKEN not set in .env")
        print("Create a .env file with: DISCORD_TOKEN=your_discord_bot_token_here")
        raise SystemExit(1)

    try:
        r = subprocess.run(
            [CLAUDE_CMD, "--version"],
            capture_output=True, text=True, timeout=10,
            creationflags=CREATE_FLAGS,
        )
        print(f"Claude Code: {r.stdout.strip()}")
    except FileNotFoundError:
        print(f"'{CLAUDE_CMD}' not found. Is Claude Code installed and in PATH?")
        raise SystemExit(1)

    print("Starting claudebot...")
    client.run(DISCORD_TOKEN)
