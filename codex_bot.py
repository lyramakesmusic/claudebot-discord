#!/usr/bin/env python3
"""
codex_bot — Discord <-> OpenAI Codex CLI bridge

Uses `codex app-server` over stdio for a persistent JSON-RPC connection.
One app-server process, multiple conversations (one per Discord channel/thread).
Streaming text deltas, auto-approved command execution, interrupt support.

Mention the bot or reply to it to interact.
"""

import os
import sys
import re
import json
import asyncio
import subprocess
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

import discord
from dotenv import load_dotenv

load_dotenv()

# ── Config ───────────────────────────────────────────────────────────────────

DISCORD_TOKEN = os.getenv("CODEX_DISCORD_TOKEN", "")
BOT_USER_ID = int(os.getenv("CODEX_BOT_USER_ID", "0"))
CLAUDE_BOT_USER_ID = os.getenv("BOT_USER_ID", "1466773230147604651")
_codex_default = "codex.cmd" if os.name == "nt" else "codex"
CODEX_CMD = os.getenv("CODEX_CMD", _codex_default)
CODEX_MODEL = os.getenv("CODEX_MODEL", "")  # blank = use server default
DOCUMENTS_DIR = Path.home() / "Documents"
STATE_FILE = Path(__file__).parent / "codex_state.json"
MAX_DISCORD_LEN = 1900
TYPING_INTERVAL = 8

HOME_CHANNEL_ID = int(os.getenv("HOME_CHANNEL_ID", "0"))
PRIMARY_GUILD_ID: int = 0

OWNER_ID = 891221733326090250  # Lyra

CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# ── Hot Reload ────────────────────────────────────────────────────────────────

_BOT_FILE = Path(__file__)
_BOOT_MTIME = _BOT_FILE.stat().st_mtime


def _self_modified() -> bool:
    try:
        return _BOT_FILE.stat().st_mtime != _BOOT_MTIME
    except Exception:
        return False

log = logging.getLogger("codexbot")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent / "codexbot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)


# ── Guild helpers ─────────────────────────────────────────────────────────────

def _guild_slug(guild: discord.Guild) -> str:
    return re.sub(r"[^\w\-]", "-", guild.name).strip("-").lower()[:50]


def _guild_docs_dir(guild_id: int, guild: discord.Guild = None) -> Path:
    if guild_id == PRIMARY_GUILD_ID:
        return DOCUMENTS_DIR
    slug = _guild_slug(guild) if guild else str(guild_id)
    return DOCUMENTS_DIR / slug


# ── System Prompt ─────────────────────────────────────────────────────────────

def _build_system_context(channel_name: str = "codex",
                          server_name: str = "", docs_dir: str = "~/Documents") -> str:
    server_note = f" in {server_name}" if server_name else ""
    return (
        f"You are codex_bot, a coding assistant on Discord (#{channel_name}{server_note}). "
        f"cwd: {docs_dir}. Sibling: <@{CLAUDE_BOT_USER_ID}> (Claude — handles projects and orchestration). "
        f"You just code. Respond directly — never summarize context. Max 2000 chars. "
        f'Upload files: ```bot_action\n{{"action":"upload","path":"/path"}}\n```'
    )


def _build_thread_context() -> str:
    return (
        "You are codex_bot in a Discord thread. You just code. "
        f"Respond directly — never summarize context. Max 2000 chars. "
        f'Upload files: ```bot_action\n{{"action":"upload","path":"/path"}}\n```'
    )


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
                if "guilds" not in data:
                    data["guilds"] = {}
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

    def get_session(self, key: str) -> Optional[dict]:
        return self._data["sessions"].get(key)

    def set_session(self, key: str, conversation_id: str, cwd: str, project: str = None):
        self._data["sessions"][key] = {
            "session_id": conversation_id,
            "cwd": cwd,
            "project": project,
            "updated": datetime.now().isoformat(),
        }
        self._save()

    def clear_session(self, key: str):
        self._data["sessions"].pop(key, None)
        self._save()

    def get_project(self, name: str, guild_id: int = None) -> Optional[dict]:
        p = self._data["projects"].get(name)
        if p and guild_id is not None and p.get("guild_id") != guild_id:
            return None
        return p

    def set_project(self, name: str, folder: str, thread_id: int, guild_id: int = 0):
        self._data["projects"][name] = {
            "folder": folder,
            "thread_id": thread_id,
            "guild_id": guild_id,
            "created": datetime.now().isoformat(),
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

    def get_guild_config(self, guild_id: int) -> Optional[dict]:
        return self._data.get("guilds", {}).get(str(guild_id))

    def set_guild_config(self, guild_id: int, home_channel_id: int, slug: str, docs_dir: str):
        self._data.setdefault("guilds", {})[str(guild_id)] = {
            "home_channel_id": home_channel_id,
            "slug": slug,
            "docs_dir": docs_dir,
        }
        self._save()


# ── Codex App-Server Bridge ─────────────────────────────────────────────────

class _TurnState:
    """Tracks a single user→assistant turn for a conversation."""

    def __init__(self):
        self.text = ""
        self.tools: list[str] = []
        self._seen_tool_ids: set[str] = set()
        self.done = asyncio.Event()
        self.error: str | None = None
        self.turn_id: str | None = None
        self.on_text = None
        self.on_tool = None


class CodexAppServer:
    """Persistent codex app-server process over stdio JSON-RPC.

    One process, many conversations. Each Discord channel/thread gets its own
    conversation with full persistent context.
    """

    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self._alive = False
        self._req_id = 0
        self._pending: dict[int | str, asyncio.Future] = {}  # id -> future for responses
        self._threads: dict[str, str] = {}  # ctx_key -> threadId
        self._thread_models: dict[str, str] = {}  # threadId -> model
        self._turns: dict[str, _TurnState] = {}  # threadId -> active turn
        self._send_locks: dict[str, asyncio.Lock] = {}  # ctx_key -> lock
        self._reader_task: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    @staticmethod
    def _is_stale_conversation_error(err: Exception) -> bool:
        msg = str(err).lower()
        needles = ("conversation not found", "invalid conversation", "unknown conversation",
                   "thread not found", "invalid thread", "unknown thread")
        return any(n in msg for n in needles)

    async def start(self):
        """Spawn the codex app-server process."""
        cmd = [CODEX_CMD, "app-server"]
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(DOCUMENTS_DIR),
            creationflags=CREATE_FLAGS,
            limit=4 * 1024 * 1024,
            env=env,
        )
        self._alive = True
        self._reader_task = asyncio.create_task(self._read_loop())
        log.info(f"Codex app-server started (pid={self.proc.pid})")

        # Initialize handshake
        resp = await self._request("initialize", {
            "clientInfo": {"name": "codex_bot", "version": "1.0.0"},
        })
        log.info(f"App-server initialized: {json.dumps(resp)[:200]}")
        await self._notify("initialized")

    async def _write(self, msg: dict):
        """Write a JSON-RPC message to stdin."""
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("App-server not running")
        line = json.dumps(msg) + "\n"
        async with self._write_lock:
            self.proc.stdin.write(line.encode("utf-8"))
            await self.proc.stdin.drain()

    async def _request(self, method: str, params: dict, timeout: float = 30) -> dict:
        """Send a JSON-RPC request and wait for the response."""
        req_id = self._next_id()
        fut = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        await self._write({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise

    async def _notify(self, method: str, params: dict = None):
        """Send a JSON-RPC notification (no response expected)."""
        msg = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        await self._write(msg)

    async def _respond(self, req_id, result: dict):
        """Send a JSON-RPC response to a server request."""
        await self._write({"jsonrpc": "2.0", "id": req_id, "result": result})

    async def _read_loop(self):
        """Background reader: dispatch JSON-RPC messages from stdout."""
        try:
            while self._alive and self.proc and self.proc.returncode is None:
                raw = await self.proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Response to our request
                if "id" in msg and "result" in msg:
                    req_id = msg["id"]
                    fut = self._pending.pop(req_id, None)
                    if fut and not fut.done():
                        fut.set_result(msg.get("result", {}))
                    continue

                # Error response
                if "id" in msg and "error" in msg:
                    req_id = msg["id"]
                    fut = self._pending.pop(req_id, None)
                    if fut and not fut.done():
                        fut.set_exception(RuntimeError(
                            msg["error"].get("message", "Unknown error")
                        ))
                    else:
                        log.warning(f"App-server error: {msg['error']}")
                    continue

                # Server request (needs response) — has "id" + "method"
                if "id" in msg and "method" in msg:
                    await self._handle_server_request(msg)
                    continue

                # Notification (no id, has method)
                method = msg.get("method", "")
                params = msg.get("params", {})
                if method:
                    await self._handle_notification(method, params)

        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("App-server read loop error")
        finally:
            self._alive = False
            # Resolve all pending futures with error
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("App-server disconnected"))
            self._pending.clear()
            # Thread handles belong to the current app-server process.
            # Force resume/new thread on next turn after any disconnect.
            self._threads.clear()
            self._thread_models.clear()
            self._turns.clear()
            log.warning("App-server read loop exited")

    async def _handle_server_request(self, msg: dict):
        """Handle requests from the server that need a response (approvals)."""
        req_id = msg["id"]
        method = msg.get("method", "")
        params = msg.get("params", {})

        # Auto-approve everything — we run dangerously
        if "Approval" in method or "approval" in method:
            log.info(f"Auto-approving: {method}")
            # The response format depends on the request type
            if "commandExecution" in method or "execCommand" in method:
                await self._respond(req_id, {"approved": True})
            elif "applyPatch" in method or "fileChange" in method:
                await self._respond(req_id, {"approved": True})
            else:
                await self._respond(req_id, {"approved": True})
            return

        if "toolRequestUserInput" in method:
            # Can't get user input in Discord, just send empty
            await self._respond(req_id, {"input": ""})
            return

        if "chatgptAuthTokensRefresh" in method:
            # Token refresh — pass back current tokens
            try:
                auth_path = Path.home() / ".codex" / "auth.json"
                auth = json.loads(auth_path.read_text("utf-8"))
                tokens = auth.get("tokens", {})
                await self._respond(req_id, {
                    "accessToken": tokens.get("access_token", ""),
                    "refreshToken": tokens.get("refresh_token", ""),
                    "idToken": tokens.get("id_token", ""),
                    "expiresAt": None,
                })
            except Exception as e:
                log.warning(f"Token refresh failed: {e}")
                await self._respond(req_id, {})
            return

        log.warning(f"Unhandled server request: {method} (id={req_id})")
        # Send empty result to avoid hanging
        await self._respond(req_id, {})

    async def _handle_notification(self, method: str, params: dict):
        """Handle streaming notifications from the v2 app-server.

        v2 notification method names: item/agentMessage/delta, item/started,
        item/completed, turn/completed, error.
        All routed by params["threadId"].
        """
        thread_id = params.get("threadId", "")
        turn = self._turns.get(thread_id) if thread_id else None

        # -- Text deltas --
        if method == "item/agentMessage/delta":
            delta = params.get("delta", "")
            if turn and delta:
                turn.text += delta
                if turn.on_text:
                    try:
                        await turn.on_text(turn.text)
                    except Exception:
                        log.exception("on_text callback error")

        # -- Tool/command started --
        elif method == "item/started":
            item = params.get("item", {})
            item_type = item.get("type", "").lower()
            item_id = item.get("id", "")
            _SKIP_TYPES = ("", "message", "text", "usermessage", "agentmessage", "reasoning")
            if turn and item_id and item_id not in turn._seen_tool_ids and item_type not in _SKIP_TYPES:
                turn._seen_tool_ids.add(item_id)
                if item_type in ("commandexecution", "command_execution", "shell"):
                    cmd_str = item.get("command", "") or item.get("call", {}).get("name", "command")
                    desc = f"Shell(`{str(cmd_str)[:80]}`)"
                else:
                    call = item.get("call", {})
                    name = call.get("name", "") or item.get("name", "") or item_type
                    desc = f"{name}()" if name else f"tool:{item_type}"
                turn.tools.append(desc)
                if turn.on_tool:
                    try:
                        await turn.on_tool(desc)
                    except Exception:
                        log.exception("on_tool callback error")

        # -- Tool/command completed --
        elif method == "item/completed":
            item = params.get("item", {})
            item_type = item.get("type", "").lower()
            item_id = item.get("id", "")
            _SKIP_TYPES = ("", "message", "text", "usermessage", "agentmessage", "reasoning")
            if turn and item_id and item_id not in turn._seen_tool_ids and item_type not in _SKIP_TYPES:
                turn._seen_tool_ids.add(item_id)
                if item_type in ("commandexecution", "command_execution", "shell"):
                    cmd_str = item.get("command", "") or item.get("call", {}).get("name", "command")
                    desc = f"Shell(`{str(cmd_str)[:80]}`)"
                else:
                    call = item.get("call", {})
                    name = call.get("name", "") or item.get("name", "") or item_type
                    desc = f"{name}()" if name else f"tool:{item_type}"
                turn.tools.append(desc)
                if turn.on_tool:
                    try:
                        await turn.on_tool(desc)
                    except Exception:
                        log.exception("on_tool callback error")

        # -- Turn completed --
        elif method == "turn/completed":
            if turn:
                err = (params.get("turn", {}) or {}).get("error")
                if err:
                    if isinstance(err, dict):
                        turn.error = err.get("message", str(err))
                    else:
                        turn.error = str(err)
                turn.done.set()

        # -- Error --
        elif method == "error":
            log.warning(f"App-server error: {json.dumps(params)[:500]}")
            if thread_id and turn:
                err = params.get("error", {})
                msg = err.get("message", str(err)) if isinstance(err, dict) else params.get("message", "Server error")
                will_retry = params.get("willRetry", False)
                if not will_retry:
                    turn.error = msg
                    turn.done.set()
                else:
                    log.info(f"Server will retry after error: {msg}")

        # -- Everything else: silently ignored --

    async def new_thread(self, ctx_key: str, cwd: str,
                         system_prompt: str = "") -> str:
        """Create a new v2 thread, return threadId."""
        params = {
            "cwd": cwd.replace("\\", "/"),
            "sandbox": "danger-full-access",
            "approvalPolicy": "never",
        }
        if system_prompt:
            params["developerInstructions"] = system_prompt
        if CODEX_MODEL:
            params["model"] = CODEX_MODEL

        resp = await self._request("thread/start", params)
        thread_id = (resp.get("thread", {}) or {}).get("id", "")
        if not thread_id:
            raise RuntimeError(f"No thread.id returned: {resp}")
        model = resp.get("model", "")
        log.info(f"thread/start response: {json.dumps(resp)[:300]}")

        # v2 auto-subscribes, no addConversationListener needed
        self._threads[ctx_key] = thread_id
        if model:
            self._thread_models[thread_id] = model
        log.info(f"New thread {thread_id[:12]}... for {ctx_key} (model={model})")
        return thread_id

    async def resume_thread(self, ctx_key: str, thread_id: str,
                            cwd: str = "", system_prompt: str = "") -> str:
        """Resume a previous thread by ID (v2 loads from disk)."""
        try:
            params = {"threadId": thread_id}
            if cwd:
                params["cwd"] = cwd.replace("\\", "/")
            if system_prompt:
                params["developerInstructions"] = system_prompt
            if CODEX_MODEL:
                params["model"] = CODEX_MODEL
            params["sandbox"] = "danger-full-access"
            params["approvalPolicy"] = "never"

            resp = await self._request("thread/resume", params)
            resumed_id = (resp.get("thread", {}) or {}).get("id", thread_id)
            model = resp.get("model", "")
            # v2 auto-subscribes
            self._threads[ctx_key] = resumed_id
            if model:
                self._thread_models[resumed_id] = model
            log.info(f"Resumed thread {resumed_id[:12]}... for {ctx_key}")
            return resumed_id
        except Exception as e:
            log.warning(f"Failed to resume thread {thread_id[:12]}...: {e}")
            raise

    async def send_turn(self, ctx_key: str, cwd: str, prompt: str,
                        system_prompt: str = "",
                        on_text=None, on_tool=None,
                        timeout: float = 300) -> dict:
        """Send a user message and wait for the turn to complete.

        Creates thread if needed. Resumes from state if available.
        Returns dict with text, tools, error info.
        """
        # Ensure we have a lock for this context
        if ctx_key not in self._send_locks:
            self._send_locks[ctx_key] = asyncio.Lock()

        async with self._send_locks[ctx_key]:
            # Ensure app-server is running
            if not self._alive:
                await self.start()

            # Get or create thread
            thread_id = self._threads.get(ctx_key)
            if not thread_id:
                # Try resume from saved state
                saved = state.get_session(ctx_key)
                if saved:
                    try:
                        thread_id = await self.resume_thread(
                            ctx_key, saved["session_id"],
                            cwd=cwd, system_prompt=system_prompt)
                    except Exception:
                        thread_id = None

                if not thread_id:
                    thread_id = await self.new_thread(
                        ctx_key, cwd, system_prompt)

            for attempt in range(2):
                # Create turn state
                turn = _TurnState()
                turn.on_text = on_text
                turn.on_tool = on_tool
                self._turns[thread_id] = turn

                # Determine model - required field, use stored or default
                model = CODEX_MODEL or self._thread_models.get(thread_id, "gpt-5.3-codex")

                # Send the turn (v2 API)
                send_params = {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": prompt}],
                    "model": model,
                    "cwd": cwd.replace("\\", "/"),
                    "approvalPolicy": "never",
                    "sandboxPolicy": {"type": "dangerFullAccess"},
                    "summary": "auto",
                }

                try:
                    resp = await self._request("turn/start", send_params, timeout=30)
                    log.info(f"turn/start response: {json.dumps(resp)[:300]}")
                    # Save turn ID for interrupt support
                    turn_obj = resp.get("turn", {}) or {}
                    turn.turn_id = turn_obj.get("id")
                except Exception as e:
                    self._turns.pop(thread_id, None)
                    if attempt == 0 and self._is_stale_conversation_error(e):
                        log.warning(
                            f"Stale thread for {ctx_key} ({thread_id[:12]}...), "
                            "retrying with new thread"
                        )
                        self._threads.pop(ctx_key, None)
                        self._thread_models.pop(thread_id, None)
                        state.clear_session(ctx_key)
                        thread_id = await self.new_thread(ctx_key, cwd, system_prompt)
                        continue

                    log.error(f"turn/start failed: {e}")
                    return {
                        "text": "", "conversation_id": thread_id, "cost_usd": 0,
                        "error": True, "error_message": str(e), "tools": [],
                    }

                # Wait for turn to complete
                try:
                    await asyncio.wait_for(turn.done.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    turn.error = "Response timed out"
                    try:
                        interrupt_params = {"threadId": thread_id}
                        if turn.turn_id:
                            interrupt_params["turnId"] = turn.turn_id
                        await self._request("turn/interrupt",
                                            interrupt_params, timeout=5)
                    except Exception:
                        pass

                self._turns.pop(thread_id, None)

                # If the error is a stale/missing thread (e.g. after reload),
                # clear the session and retry with a fresh thread.
                if (attempt == 0 and turn.error and not turn.text
                        and self._is_stale_conversation_error(Exception(turn.error))):
                    log.warning(f"Stale thread via notification for {ctx_key}, retrying fresh")
                    self._threads.pop(ctx_key, None)
                    self._thread_models.pop(thread_id, None)
                    state.clear_session(ctx_key)
                    thread_id = await self.new_thread(ctx_key, cwd, system_prompt)
                    continue

                is_error = turn.error is not None and not turn.text
                return {
                    "text": turn.text,
                    "conversation_id": thread_id,
                    "cost_usd": 0,
                    "error": is_error,
                    "error_message": turn.error or "",
                    "tools": turn.tools,
                }

            # Both attempts failed
            return {
                "text": "", "conversation_id": thread_id, "cost_usd": 0,
                "error": True, "error_message": turn.error or "Failed after retry", "tools": [],
            }

    def is_busy(self, ctx_key: str) -> bool:
        lock = self._send_locks.get(ctx_key)
        return lock.locked() if lock else False

    async def interrupt(self, ctx_key: str):
        """Interrupt a running turn."""
        thread_id = self._threads.get(ctx_key)
        if thread_id:
            try:
                params = {"threadId": thread_id}
                turn = self._turns.get(thread_id)
                if turn and turn.turn_id:
                    params["turnId"] = turn.turn_id
                await self._request("turn/interrupt", params, timeout=5)
            except Exception as e:
                log.warning(f"Interrupt failed: {e}")

    async def kill(self):
        """Kill the app-server process."""
        self._alive = False
        if self._reader_task:
            self._reader_task.cancel()
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.kill()
            except Exception:
                pass
            try:
                await self.proc.wait()
            except Exception:
                pass
        self._threads.clear()
        self._thread_models.clear()
        self._turns.clear()
        self._pending.clear()
        log.info("Codex app-server killed")


# ── Bot Actions ──────────────────────────────────────────────────────────────

BOT_ACTION_RE = re.compile(
    r"(?:```bot_action\s*\n(.*?)\n```|<bot_action>\s*(.*?)\s*</bot_action>)",
    re.DOTALL,
)


def extract_bot_actions(text: str) -> tuple[str, list[dict]]:
    actions = []
    for m in BOT_ACTION_RE.finditer(text):
        raw = m.group(1) or m.group(2)
        try:
            actions.append(json.loads(raw))
        except json.JSONDecodeError:
            log.warning(f"Bad bot_action JSON: {raw[:100]}")
    cleaned = BOT_ACTION_RE.sub("", text).strip()
    return cleaned, actions


async def execute_bot_actions(
    actions: list[dict],
    message: discord.Message,
    channel: discord.abc.Messageable,
    guild_id: int = 0,
) -> tuple[list[str], bool]:
    """Execute bot actions. Returns (status_messages, should_reload)."""
    guild_config = state.get_guild_config(guild_id)
    guild_docs = Path(guild_config["docs_dir"]) if guild_config else DOCUMENTS_DIR

    results = []
    should_reload = False
    for act in actions:
        action = act.get("action")

        if action == "upload":
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

        elif action == "reload":
            try:
                r = subprocess.run(
                    [sys.executable, "-c",
                     f"compile(open(r'{_BOT_FILE}', encoding='utf-8').read(), 'codex_bot.py', 'exec')"],
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

        else:
            log.warning(f"Unknown bot_action: {action}")

    return results, should_reload


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


def sanitize(text: str) -> str:
    return text.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")


def _is_guild_channel(channel: discord.abc.Messageable) -> bool:
    return getattr(channel, "guild", None) is not None


# ── Discord Bot ──────────────────────────────────────────────────────────────

state = BotState(STATE_FILE)
bridge = CodexAppServer()
_processed_msgs: set[int] = set()
_boot_time = datetime.utcnow()
_injected_inputs: dict[str, list[dict]] = {}
_injected_lock = asyncio.Lock()

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

client = discord.Client(intents=intents)


async def _queue_injected_input(ctx_key: str, prompt: str, attachments: list[str]) -> int:
    async with _injected_lock:
        q = _injected_inputs.setdefault(ctx_key, [])
        q.append({"prompt": prompt, "attachments": attachments})
        return len(q)


async def _pop_injected_inputs(ctx_key: str) -> list[dict]:
    async with _injected_lock:
        return _injected_inputs.pop(ctx_key, [])


@client.event
async def on_ready():
    global PRIMARY_GUILD_ID
    log.info(f"Logged in as {client.user} (ID: {client.user.id})")

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
                for p in state._data.get("projects", {}).values():
                    if p.get("guild_id") == 0:
                        p["guild_id"] = PRIMARY_GUILD_ID
                state._save()
        except Exception:
            log.warning("Could not auto-detect primary guild from HOME_CHANNEL_ID")

    # Start the app-server
    try:
        await bridge.start()
        log.info("Codex app-server ready")
    except Exception as e:
        log.error(f"Failed to start codex app-server: {e}")


@client.event
async def on_message(message: discord.Message):
    if message.created_at.replace(tzinfo=None) < _boot_time:
        return

    if message.id in _processed_msgs:
        return
    _processed_msgs.add(message.id)
    if len(_processed_msgs) > 200:
        _processed_msgs.clear()

    if not _is_guild_channel(message.channel):
        return

    # ── Should we respond? ───────────────────────────────────
    mentioned = client.user in message.mentions

    if message.author.bot:
        if not mentioned:
            return
    else:
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

    # ── Extract prompt ───────────────────────────────────────
    content = (message.content or "").strip()
    content = re.sub(rf"<@!?{client.user.id}>", "", content).strip()

    # ── Collect attachments ──────────────────────────────────
    ATT_DIR = Path(__file__).parent / "codex_attachments"
    ATT_DIR.mkdir(exist_ok=True)
    att_paths = []
    for att in message.attachments:
        try:
            data = await att.read()
            safe_name = re.sub(r"[^\w.\-]", "_", att.filename or "file")
            att_path = ATT_DIR / f"{att.id}_{safe_name}"
            att_path.write_bytes(data)
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
        log.info("Manual reload requested - validating")
        try:
            r = subprocess.run(
                [sys.executable, "-c",
                 f"compile(open(r'{_BOT_FILE}', encoding='utf-8').read(), 'codex_bot.py', 'exec')"],
                capture_output=True, text=True, timeout=10,
                creationflags=CREATE_FLAGS,
            )
            if r.returncode != 0:
                err = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "unknown"
                await message.reply(f"Reload aborted - bad syntax:\n```\n{err[:500]}\n```", mention_author=False)
                return
        except Exception as e:
            await message.reply(f"Reload validation failed: {e}", mention_author=False)
            return
        await message.add_reaction("\u2705")
        await bridge.kill()
        await client.close()
        return

    # Snapshot mtime so we only warn if codex modifies the file *during this turn*
    try:
        _turn_start_mtime = _BOT_FILE.stat().st_mtime
    except Exception:
        _turn_start_mtime = 0

    # ── Check app-server health ──────────────────────────────
    if not bridge._alive:
        try:
            await bridge.start()
        except Exception as e:
            await message.reply(f"Codex app-server failed to start: {e}", mention_author=False)
            return

    # ── Resolve guild context ────────────────────────────────
    guild = message.guild
    guild_id = guild.id

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
            label = thread_name
            folder = docs_dir / thread_name
            folder.mkdir(parents=True, exist_ok=True)
            cwd = str(folder)
            ctx_key = f"thread:{channel_id}"

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

    # ── Build system prompt ──────────────────────────────────
    if is_orchestrator:
        ch_name = getattr(channel, "name", "codex")
        srv_name = guild.name if guild else ""
        sys_prompt = _build_system_context(
            channel_name=ch_name,
            server_name=srv_name,
            docs_dir=str(docs_dir),
        )
    else:
        sys_prompt = _build_thread_context()

    cleanup_paths = [p for _, p in att_paths]

    # ── If busy, queue + inject ──────────────────────────────
    if bridge.is_busy(ctx_key):
        await _queue_injected_input(ctx_key, user_msg, cleanup_paths)
        await bridge.interrupt(ctx_key)
        await message.reply("Queued. I will inject this into the active run.", mention_author=False)
        return

    # ── Run turn ─────────────────────────────────────────────

    async def _keep_typing():
        try:
            while True:
                await channel.typing()
                await asyncio.sleep(TYPING_INTERVAL)
        except asyncio.CancelledError:
            pass

    typing_task = asyncio.create_task(_keep_typing())
    await channel.typing()

    current_text = ""
    sent_text_len = 0
    tool_log = []

    async def _flush_unsent_text():
        nonlocal sent_text_len
        if len(current_text) <= sent_text_len:
            return
        unsent = current_text[sent_text_len:]
        cleaned, _ = extract_bot_actions(unsent)
        cleaned = cleaned.strip()
        if cleaned:
            for chunk in split_message(sanitize(cleaned)):
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
        await _flush_unsent_text()
        try:
            await channel.send(desc)
        except Exception as e:
            log.warning(f"Failed to send tool status: {e}")

    next_prompt = user_msg
    pending_reload = False
    try:
        while next_prompt:
            current_text = ""
            sent_text_len = 0
            tool_log = []
            try:
                result = await bridge.send_turn(
                    ctx_key, cwd, next_prompt,
                    system_prompt=sys_prompt,
                    on_text=on_text, on_tool=on_tool,
                )
            except Exception as e:
                log.exception("Codex bridge error")
                await message.reply(f"Error: {e}", mention_author=False)
                return

            # Persist conversation_id for resume across restarts
            conv_id = result.get("conversation_id")
            if conv_id:
                state.set_session(ctx_key, conv_id, cwd, label)

            log.info(f"turn finished: error={result['error']} text_len={len(result.get('text',''))} "
                     f"tools={len(result.get('tools',[]))} current_text_len={len(current_text)} "
                     f"sent_text_len={sent_text_len}")

            if result["error"]:
                err = result.get("error_message") or "Unknown error"
                await message.reply(f"Error:\n```\n{err[:1800]}\n```", mention_author=False)
                return

            text = current_text or result.get("text", "")
            if text:
                cleaned_text, actions = extract_bot_actions(text)

                if sent_text_len > 0:
                    unsent_raw = current_text[sent_text_len:] if sent_text_len < len(current_text) else ""
                    final_cleaned, _ = extract_bot_actions(unsent_raw)
                    final_text = final_cleaned.strip()
                else:
                    final_text = cleaned_text

                if final_text:
                    text_chunks = split_message(sanitize(final_text))
                    try:
                        await message.reply(text_chunks[0], mention_author=False)
                    except Exception as e:
                        log.warning(f"Failed to reply: {e}")
                        try:
                            await channel.send(text_chunks[0])
                        except Exception as e2:
                            log.warning(f"Fallback send also failed: {e2}")
                    for chunk in text_chunks[1:]:
                        try:
                            await channel.send(chunk)
                        except Exception as e:
                            log.warning(f"Failed to send chunk: {e}")

                if actions:
                    action_results, pending_reload = await execute_bot_actions(actions, message, channel, guild_id)
                    if action_results:
                        remaining = sanitize("\n".join(action_results))
                        for chunk in split_message(remaining):
                            try:
                                await channel.send(chunk)
                            except Exception:
                                pass
                    if pending_reload:
                        break

            injected = await _pop_injected_inputs(ctx_key)
            if not injected:
                await asyncio.sleep(0)
                injected = await _pop_injected_inputs(ctx_key)
                if not injected:
                    break
            cleanup_paths.extend(
                p for item in injected for p in item.get("attachments", []) if p
            )
            next_prompt = "\n\n".join(item["prompt"] for item in injected if item.get("prompt"))

    finally:
        typing_task.cancel()
        for p in cleanup_paths:
            try:
                os.unlink(p)
            except OSError:
                pass

    if pending_reload:
        log.info("bot_action reload — restarting")
        await message.add_reaction("\u2705")
        await bridge.kill()
        await client.close()
        return

    if _BOT_FILE.stat().st_mtime != _turn_start_mtime:
        log.info("codex_bot.py was modified during this turn")
        await channel.send("codex_bot.py was modified. Say `reload` to apply changes.")


# ── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("Error: CODEX_DISCORD_TOKEN not set in .env")
        raise SystemExit(1)

    try:
        r = subprocess.run(
            [CODEX_CMD, "--version"],
            capture_output=True, text=True, timeout=10,
            creationflags=CREATE_FLAGS,
        )
        print(f"Codex CLI: {r.stdout.strip()}")
    except FileNotFoundError:
        print(f"'{CODEX_CMD}' not found. Is Codex installed and in PATH?")
        raise SystemExit(1)

    print("Starting codex_bot (app-server mode)...")
    client.run(DISCORD_TOKEN)
