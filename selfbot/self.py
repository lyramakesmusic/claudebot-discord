#!/usr/bin/env python3
"""
claude selfbot — portable personal Claude Code assistant

Monitors your own messages for @claude triggers, grabs recent context,
runs through Claude Code, and responds as you with > prefix.

Uses a modified discord.py that supports user tokens.
"""

import os
import sys
import re
import json
import asyncio
import subprocess
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

import aiohttp

# Single-instance enforcement
sys.path.insert(0, str(Path(__file__).parent.parent))
from shared.lockfile import acquire_or_exit
acquire_or_exit("selfbot")

# Add the modified discord.py package to the import path
SELFBOT_DIR = Path(__file__).parent
PACKAGE_DIR = SELFBOT_DIR.parent / "selfbot discordpy package"
sys.path.insert(0, str(PACKAGE_DIR))

import discord
from dotenv import load_dotenv

# Load .env from the parent claudebot directory
load_dotenv(SELFBOT_DIR.parent / ".env")

# ── Config ───────────────────────────────────────────────────────────────────

SELFBOT_TOKEN = os.getenv("SELFBOT_TOKEN", "")
CLAUDE_CMD = os.getenv("CLAUDE_CMD", "claude")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "")
HOME_CHANNEL_ID = int(os.getenv("HOME_CHANNEL_ID", "1466772067968880772"))
BOT_USER_ID = int(os.getenv("BOT_USER_ID", "1466773230147604651"))
DOCUMENTS_DIR = Path.home() / "Documents"
DEFAULT_CWD = str(DOCUMENTS_DIR)
MAX_DISCORD_LEN = 1900
CONTEXT_LIMIT = 20        # messages of context to grab
PROCESS_TIMEOUT = 300     # 5 min max per invocation (shorter than main bot)

TRIGGER_RE = re.compile(r"^\.\s*claude\b", re.IGNORECASE)
SIMULATE_RE = re.compile(r"^\.\s*simulate\s+(\S+)", re.IGNORECASE)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
SIMULATE_MODEL = "z-ai/glm-4.5"
SIMULATE_PROVIDER = {"order": ["novita"], "allow_fallbacks": False}
SIMULATE_ENDPOINT = "https://openrouter.ai/api/v1/completions"

CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# ── Logging ──────────────────────────────────────────────────────────────────

log = logging.getLogger("selfbot")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(SELFBOT_DIR / "selfbot.log", encoding="utf-8"),
    ],
)

# ── Memories ────────────────────────────────────────────────────────────────

MEMORIES_FILE = SELFBOT_DIR / "memories.json"
MEMORY_ACTION_RE = re.compile(r"```memory\s*\n(.*?)\n```", re.DOTALL)


def load_memories() -> list[dict]:
    if MEMORIES_FILE.exists():
        try:
            return json.loads(MEMORIES_FILE.read_text("utf-8"))
        except Exception:
            log.warning("Corrupt memories file, starting fresh")
    return []


def save_memories(memories: list[dict]):
    tmp = MEMORIES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(memories, indent=2), "utf-8")
    tmp.replace(MEMORIES_FILE)


def _next_memory_id(memories: list[dict]) -> int:
    if not memories:
        return 1
    return max(m.get("id", 0) for m in memories) + 1


def process_memory_actions(text: str, channel_name: str, server_name: str) -> str:
    """Extract ```memory``` blocks, execute them, return cleaned text."""
    matches = list(MEMORY_ACTION_RE.finditer(text))
    if not matches:
        return text

    memories = load_memories()

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

    save_memories(memories)
    return MEMORY_ACTION_RE.sub("", text).strip()


def _format_memories_for_prompt() -> str:
    """Format current memories as a readable block for the prompt."""
    memories = load_memories()
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

REMINDERS_FILE = SELFBOT_DIR / "reminders.json"
REMINDER_ACTION_RE = re.compile(r"```reminder\s*\n(.*?)\n```", re.DOTALL)
PST = timezone(timedelta(hours=-8))
OWNER_ID = 891221733326090250


def load_reminders() -> list[dict]:
    if REMINDERS_FILE.exists():
        try:
            return json.loads(REMINDERS_FILE.read_text("utf-8"))
        except Exception:
            log.warning("Corrupt reminders file")
    return []


def save_reminders(reminders: list[dict]):
    tmp = REMINDERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(reminders, indent=2), "utf-8")
    tmp.replace(REMINDERS_FILE)


def _next_reminder_id(reminders: list[dict]) -> int:
    if not reminders:
        return 1
    return max(r.get("id", 0) for r in reminders) + 1


def process_reminder_actions(text: str, channel_id: int, channel_name: str) -> str:
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
                "time": action.get("time"),
                "channel_id": action.get("channel_id", channel_id),
                "created": datetime.now(PST).isoformat(),
                "source_channel": channel_name,
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
    """Send a Windows toast notification."""
    try:
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


# ── Delegate (selfbot → main bot) ────────────────────────────────────────────

DELEGATE_ACTION_RE = re.compile(r"```delegate\s*\n(.*?)\n```", re.DOTALL)


async def process_delegate_actions(text: str, source_channel_name: str) -> str:
    """Extract ```delegate``` blocks, send build requests to #claude, return cleaned text."""
    matches = list(DELEGATE_ACTION_RE.finditer(text))
    if not matches:
        return text

    for m in matches:
        try:
            action = json.loads(m.group(1))
        except json.JSONDecodeError:
            log.warning(f"Bad delegate JSON: {m.group(1)[:100]}")
            continue

        act = action.get("action")

        if act == "build":
            project_name = action.get("name", "").strip()
            spec = action.get("spec", "").strip()
            if not project_name or not spec:
                log.warning("Delegate build missing name or spec")
                continue

            # ping the bot in #claude — it handles thread/folder creation
            try:
                ch = client.get_channel(HOME_CHANNEL_ID)
                if ch is None:
                    ch = await client.fetch_channel(HOME_CHANNEL_ID)

                msg = (
                    f"<@{BOT_USER_ID}> Create a new project called **{project_name}**.\n\n"
                    f"**Spec (from #{source_channel_name}):**\n{spec}\n\n"
                    f"Work agentically — check available skills, read relevant project files, "
                    f"and build as much as you can autonomously without waiting for input."
                )
                await ch.send(msg)
                log.info(f"Delegated '{project_name}' to bot in #claude")
            except Exception as e:
                log.warning(f"Failed to delegate to #claude: {e}")

    return DELEGATE_ACTION_RE.sub("", text).strip()


# ── State (sessions per channel) ─────────────────────────────────────────────

STATE_FILE = SELFBOT_DIR / "sessions.json"


def _load_sessions() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:
            log.warning("Corrupt sessions file, starting fresh")
    return {}


def _save_sessions(sessions: dict):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sessions, indent=2), "utf-8")
    tmp.replace(STATE_FILE)


sessions: dict = _load_sessions()


SESSION_MAX_AGE_HOURS = 12


def get_session(channel_id: int) -> str | None:
    entry = sessions.get(str(channel_id))
    if not entry:
        return None
    # start fresh context if session is older than 12 hours
    updated = entry.get("updated")
    if updated:
        try:
            age = datetime.now() - datetime.fromisoformat(updated)
            if age.total_seconds() > SESSION_MAX_AGE_HOURS * 3600:
                log.info(f"Session for {channel_id} is {age} old, starting fresh")
                return None
        except (ValueError, TypeError):
            pass
    return entry["session_id"]


def set_session(channel_id: int, session_id: str):
    sessions[str(channel_id)] = {
        "session_id": session_id,
        "updated": datetime.now().isoformat(),
    }
    _save_sessions(sessions)


# ── Claude Code Bridge (simplified) ─────────────────────────────────────────

_locks: dict[str, asyncio.Lock] = {}


def lock_for(key: str) -> asyncio.Lock:
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]


async def run_claude(prompt: str, cwd: str, session_id: str = None) -> dict:
    """Run a prompt through Claude Code. Returns dict with text, session_id, error."""
    cmd = [
        CLAUDE_CMD, "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    if CLAUDE_MODEL:
        cmd += ["--model", CLAUDE_MODEL]
    if session_id:
        cmd += ["--resume", session_id]

    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        creationflags=CREATE_FLAGS,
        limit=1024 * 1024,  # 1MB line buffer (default 64KB too small for large responses)
        env=env,
    )

    text = ""
    last_text_snapshot = ""
    result = {
        "text": "", "session_id": None, "cost_usd": 0,
        "error": False, "error_message": "",
    }

    try:
        while True:
            try:
                raw = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=PROCESS_TIMEOUT
                )
            except asyncio.TimeoutError:
                proc.kill()
                result["error"] = True
                result["error_message"] = f"Timed out ({PROCESS_TIMEOUT}s)"
                break

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

            if msg_type == "assistant":
                for block in data.get("message", {}).get("content", []):
                    bt = block.get("type")
                    if bt == "text" and block.get("text"):
                        block_text = block["text"]
                        if block_text.startswith(last_text_snapshot):
                            delta = block_text[len(last_text_snapshot):]
                        else:
                            delta = block_text
                        last_text_snapshot = block_text
                        text += delta

            elif msg_type == "result":
                result.update({
                    "text": data.get("result", text),
                    "session_id": data.get("session_id"),
                    "cost_usd": data.get("total_cost_usd", 0),
                    "error": data.get("is_error", False),
                })
                if result["error"]:
                    result["error_message"] = data.get("result", "Unknown error")

    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
        await proc.wait()

    if not result["text"] and not result["error"]:
        stderr = (await proc.stderr.read()).decode("utf-8", errors="replace")
        if proc.returncode != 0:
            result["error"] = True
            result["error_message"] = stderr or f"Exit code {proc.returncode}"

    # prefer streamed text over result text
    if text:
        result["text"] = text

    return result


# ── Helpers ──────────────────────────────────────────────────────────────────

def split_message(text: str, limit: int = MAX_DISCORD_LEN) -> list[str]:
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


def quote_response(text: str) -> str:
    """Prefix each line with > for quote formatting."""
    lines = text.split("\n")
    return "\n".join(f"> {line}" for line in lines)


# ── Simulate (base model completion) ─────────────────────────────────────────

async def _run_simulate(channel: discord.TextChannel, target_name: str, before_msg: discord.Message):
    """Gather context, build prefill, call GLM 4.5 base model, return completion."""
    # gather last 20 messages as context
    lines = []
    try:
        async for msg in channel.history(limit=20, before=before_msg):
            text = (msg.content or "").strip()
            if not text:
                continue
            author = msg.author.display_name or msg.author.name
            lines.append(f"<{author}>: {text}")
    except Exception:
        pass
    lines.reverse()

    # collect unique speaker names for stop sequences
    speakers = set()
    for l in lines:
        if ">: " in l:
            name = l.split(">:", 1)[0].lstrip("<")
            speakers.add(name)

    # build prefill: context lines + target prompt (trailing space for continuation)
    prefill = "\n\n".join(lines) + f"\n\n<{target_name}>: "

    # stop on ANY speaker tag (including target — one turn only)
    stop_seqs = [f"\n<{name}>:" for name in speakers]
    stop_seqs.append(f"\n<{target_name}>:")  # in case target wasn't in history
    stop_seqs.append("\n\n")
    # dedupe, keep order
    seen = set()
    stop_seqs = [s for s in stop_seqs if not (s in seen or seen.add(s))]

    log.info(f"Simulate prefill ({len(prefill)} chars):\n{prefill[-500:]}")
    log.info(f"Stop sequences: {stop_seqs[:4]}")

    # call OpenRouter /v1/completions (streaming)
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": SIMULATE_MODEL,
        "prompt": prefill,
        "max_tokens": 512,
        "temperature": 1.0,
        "min_p": 0.01,
        "stream": True,
        "stop": stop_seqs[:4],  # API typically limits to 4 stop sequences
        "provider": SIMULATE_PROVIDER,
    }

    result = ""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                SIMULATE_ENDPOINT, headers=headers, json=payload
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.warning(f"Simulate API error {resp.status}: {body[:300]}")
                    return None

                async for raw_line in resp.content:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    token = ""
                    for choice in data.get("choices", []):
                        token = choice.get("text", "")

                    if not token:
                        continue
                    if not result:
                        log.info(f"Simulate first token: {token!r}")
                    result += token

                    # client-side stop: truncate at \n< or \n\n
                    for stop in ("\n\n", "\n<"):
                        idx = result.find(stop)
                        if idx != -1:
                            result = result[:idx]
                            break
                    else:
                        continue
                    break  # hit a stop, done

    except Exception as e:
        log.exception(f"Simulate streaming error: {e}")
        return None

    return result.strip() if result.strip() else None


# ── Discord Client ───────────────────────────────────────────────────────────

client = discord.Client()


@client.event
async def on_ready():
    log.info(f"Selfbot logged in as {client.user} (ID: {client.user.id})")
    # Note: reminder firing is handled by the main bot (bot.py) since pings
    # from the selfbot (user's own token) are auto-read and don't notify.



@client.event
async def on_message(message: discord.Message):
    # only respond to our own messages
    if message.author.id != client.user.id:
        return

    log.debug(f"Own message: {message.content[:80]}")

    content = (message.content or "").strip()

    # check for .simulate trigger
    sim_match = SIMULATE_RE.search(content)
    if sim_match:
        target_name = sim_match.group(1)
        channel = message.channel
        log.info(f"Simulate triggered for '{target_name}' in {getattr(channel, 'name', 'DM')}")

        try:
            await message.add_reaction("\U0001f3ad")  # 🎭
        except Exception:
            pass

        completion = await _run_simulate(channel, target_name, message)

        try:
            await message.remove_reaction("\U0001f3ad", client.user)
        except Exception:
            pass

        if completion:
            formatted = f".\n\n{target_name}: {completion}"
            for chunk in split_message(formatted):
                try:
                    await channel.send(chunk)
                except Exception:
                    pass
        else:
            try:
                await channel.send(f"> Simulate failed — no output from model.")
            except Exception:
                pass
        return

    # check for @claude trigger
    if not TRIGGER_RE.search(content):
        return

    # strip the trigger from the prompt
    prompt_text = TRIGGER_RE.sub("", content).strip()
    if not prompt_text:
        return

    channel = message.channel
    channel_id = channel.id
    ctx_key = str(channel_id)

    log.info(f"Triggered in {getattr(channel, 'name', 'DM')} (id={channel_id})")

    # acknowledge trigger
    try:
        await message.add_reaction("\U0001f4dd")  # 📝
    except Exception:
        pass

    # ── Gather context (last N messages before this one) ──
    context_lines = []
    try:
        async for hist_msg in channel.history(limit=CONTEXT_LIMIT, before=message):
            line = (hist_msg.content or "").strip()
            if line:
                author = hist_msg.author.display_name or hist_msg.author.name
                context_lines.append(f"{author}: {line}")
    except Exception:
        pass
    context_lines.reverse()

    # build full prompt with context
    username = message.author.display_name or message.author.name
    memories_block = _format_memories_for_prompt()
    reminders_block = _format_reminders_for_prompt()
    now_pst = datetime.now(PST).strftime("%Y-%m-%d %H:%M %Z (%A)")

    system_block = (
        f"[SYSTEM — selfbot assistant]\n"
        f"You are {username}'s automated self — a courier that lets them stay in conversation\n"
        f"without having to context-switch to delegate tasks, look things up, write notes, or\n"
        f"manage things. You respond as them in Discord.\n"
        f"Keep responses concise — 1-3 sentences for simple questions, short paragraph for explanations.\n\n"
        f"== YOUR NOTEBOOK ==\n"
        f"You have a persistent notebook (memories) that survives across sessions.\n"
        f"Current memories:\n{memories_block}\n\n"
        f"To manage your notebook, include ```memory``` blocks in your response.\n"
        f"These blocks are stripped before sending — the user never sees them.\n\n"
        f"Actions:\n"
        f"  Save:   ```memory\n"
        f'  {{"action": "save", "text": "thing to remember", "tags": ["tag1", "tag2"]}}\n'
        f"  ```\n"
        f"  Delete: ```memory\n"
        f'  {{"action": "delete", "id": 3}}\n'
        f"  ```\n"
        f"  Update: ```memory\n"
        f'  {{"action": "update", "id": 1, "text": "updated text", "tags": ["new"]}}\n'
        f"  ```\n\n"
        f"Use your notebook proactively — save preferences, facts, context, project details,\n"
        f"anything you'd want to remember next time. This is YOUR brain across sessions.\n"
        f"Don't ask permission to save — just do it when something seems worth remembering.\n\n"
        f"== REMINDERS ==\n"
        f"Current time: {now_pst}\n"
        f"The user's waking hours are ~2:00 PM to ~3:00 AM PST. Schedule reminders within those hours.\n"
        f"IMPORTANT: The user often stays up past midnight. 'Tomorrow' at 2 AM means the NEXT afternoon\n"
        f"(same calendar day), NOT +24 hours. Their 'day' doesn't reset until they sleep (~3 AM).\n"
        f"For example, at 2 AM on Feb 1, 'remind me tomorrow' = Feb 1 ~3 PM, NOT Feb 2.\n"
        f"All times should be in PST (America/Los_Angeles, UTC-8).\n\n"
        f"Pending reminders:\n{reminders_block}\n\n"
        f"To set a reminder, include a ```reminder``` block:\n"
        f"  Set:    ```reminder\n"
        f'  {{"action": "set", "text": "what to remind about", "time": "2026-02-01T15:00:00-08:00"}}\n'
        f"  ```\n"
        f"  Cancel: ```reminder\n"
        f'  {{"action": "cancel", "id": 3}}\n'
        f"  ```\n\n"
        f"The \"time\" field MUST be an ISO 8601 timestamp with timezone offset (e.g. -08:00 for PST).\n"
        f"When a reminder fires, it sends a Discord ping AND a Windows desktop notification.\n"
        f"reminder blocks are stripped before sending — the user never sees them.\n\n"
        f"== PROJECT DELEGATION ==\n"
        f"Your role is conversational — chatting, answering quick questions, managing memories\n"
        f"and reminders. There are dedicated agents for everything else: building, researching,\n"
        f"coding, etc. You just pass the message along. Don't overwork yourself — you're a\n"
        f"courier, not the whole team.\n\n"
        f"When the user wants to build, create, research, or start anything beyond a quick\n"
        f"conversation, emit a ```delegate``` block and the right agent picks it up.\n\n"
        f"  ```delegate\n"
        f'  {{"action": "build", "name": "project-name", "spec": "Detailed spec with all context..."}}\n'
        f"  ```\n\n"
        f"This pings the main bot in #claude, which creates the project thread, folder, and a\n"
        f"dedicated Claude Code instance that works autonomously. You just fire and forget.\n\n"
        f"Your most important job here is translating conversation into clear instructions.\n"
        f"The user talks casually — 'hey can you look into X for my Y project' — and you turn\n"
        f"that into a specific, actionable spec the builder can run with. The builder instance\n"
        f"won't have this conversation, so the spec is all it gets. Include:\n"
        f"- A clear description of what to build or research\n"
        f"- Requirements, constraints, and preferences from the conversation\n"
        f"- Referenced projects, files, or technologies (with full paths when you know them)\n"
        f"- Architecture decisions or approaches that came up\n"
        f"- The user's exact words when they matter\n\n"
        f"Also tell the builder to work agentically — check available skills, read any relevant\n"
        f"project files, and build as much as it can autonomously without waiting for input.\n"
        f"You're turning casual intent into a ready-to-go work order.\n\n"
        f"IMPORTANT: The user often addresses both you AND the builder in the same message.\n"
        f"You need to figure out which parts are for you vs which parts to pass through.\n"
        f"- Instructions addressed to you (the courier): 'search X and start a project',\n"
        f"  'spin up a research thread for Y', 'kick off a build for Z'\n"
        f"- Instructions addressed to the builder (pass through verbatim in the spec):\n"
        f"  'write the prompts yourself', 'use unsloth for training', 'make it a CLI tool',\n"
        f"  specific technical details, architecture choices, implementation preferences\n"
        f"- When they say things like 'you know how these look' or 'do it the way we discussed',\n"
        f"  that's meant for the builder — pass it along with enough context so it makes sense.\n"
        f"The user shouldn't have to think about who they're talking to. Just be smart about it.\n"
        f"That said — if they're not explicitly asking for a project/build/research task,\n"
        f"they're always talking to you. Only split the audience when delegation is happening.\n\n"
        f"After emitting the delegate block, let the user know you've kicked it off.\n"
        f"delegate blocks are stripped before sending — the user never sees them."
    )

    if context_lines:
        context_block = "\n".join(context_lines)
        full_prompt = (
            f"{system_block}\n\n"
            f"Recent conversation:\n{context_block}\n\n"
            f"{username}: {prompt_text}"
        )
    else:
        full_prompt = (
            f"{system_block}\n\n"
            f"{username}: {prompt_text}"
        )

    # ── Run Claude Code ──
    session_id = get_session(channel_id)
    lock = lock_for(ctx_key)

    async with lock:
        try:
            result = await run_claude(full_prompt, DEFAULT_CWD, session_id)
        except Exception as e:
            log.exception("Claude bridge error")
            await message.reply(f"> Error: {e}", mention_author=False)
            return

    # save session
    if result.get("session_id"):
        set_session(channel_id, result["session_id"])

    # helper to clean up reaction
    async def _remove_trigger_reaction():
        try:
            await message.remove_reaction("\U0001f4dd", client.user)
        except Exception:
            pass

    # handle errors
    if result["error"]:
        err = result.get("error_message") or "Unknown error"
        # stale session — retry fresh
        if session_id and any(
            w in err.lower() for w in ("session", "resume", "not found", "invalid")
        ):
            log.info(f"Stale session for {ctx_key}, retrying fresh")
            sessions.pop(ctx_key, None)
            _save_sessions(sessions)
            async with lock:
                result = await run_claude(full_prompt, DEFAULT_CWD, None)
            if result.get("session_id"):
                set_session(channel_id, result["session_id"])
            if result["error"]:
                await _remove_trigger_reaction()
                try:
                    await message.reply(f"> Error: {result.get('error_message', '?')}", mention_author=False)
                except Exception:
                    await channel.send(f"> Error: {result.get('error_message', '?')}")
                return
        else:
            await _remove_trigger_reaction()
            try:
                await message.reply(f"> Error: {err[:500]}", mention_author=False)
            except Exception:
                await channel.send(f"> Error: {err[:500]}")
            return

    response = result.get("text", "").strip()
    if not response:
        await _remove_trigger_reaction()
        return

    # process memory, reminder, and delegate actions (strips blocks from response)
    ch_name = getattr(channel, "name", "DM")
    srv_name = getattr(getattr(channel, "guild", None), "name", "DM")
    response = process_memory_actions(response, ch_name, srv_name)
    response = process_reminder_actions(response, channel_id, ch_name)
    response = await process_delegate_actions(response, ch_name)

    if not response:
        await _remove_trigger_reaction()
        return

    # send as reply to the triggering message
    quoted = quote_response(response)
    chunks = split_message(quoted)
    if chunks:
        try:
            await message.reply(chunks[0], mention_author=False)
        except Exception:
            try:
                await channel.send(chunks[0])
            except Exception:
                pass
        for chunk in chunks[1:]:
            try:
                await channel.send(chunk)
            except Exception:
                pass

    await _remove_trigger_reaction()

    cost = result.get("cost_usd", 0)
    log.info(f"ctx={ctx_key} cost=${cost:.4f}")


# ── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not SELFBOT_TOKEN:
        print("Error: SELFBOT_TOKEN not set in .env")
        raise SystemExit(1)

    try:
        r = subprocess.run(
            [CLAUDE_CMD, "--version"],
            capture_output=True, text=True, timeout=10,
            creationflags=CREATE_FLAGS,
        )
        print(f"Claude Code: {r.stdout.strip()}")
    except FileNotFoundError:
        print(f"'{CLAUDE_CMD}' not found.")
        raise SystemExit(1)

    print("Starting selfbot...")
    from shared.watchdog import start_watchdog
    start_watchdog()
    client.run(SELFBOT_TOKEN, log_handler=None)
