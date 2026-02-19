"""Codex app-server bridge (no Discord dependencies)."""

import asyncio
import json
import os
import subprocess
from pathlib import Path

CODEX_CMD = ""
CODEX_MODEL = ""
DOCUMENTS_DIR = Path.home() / "Documents"
CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
state = None
log = None


def configure(state_obj, logger, codex_cmd: str, codex_model: str, documents_dir: Path, create_flags: int):
    global state, log, CODEX_CMD, CODEX_MODEL, DOCUMENTS_DIR, CREATE_FLAGS
    state = state_obj
    log = logger
    CODEX_CMD = codex_cmd
    CODEX_MODEL = codex_model
    DOCUMENTS_DIR = documents_dir
    CREATE_FLAGS = create_flags

class _TurnState:
    """Tracks a single userâ†’assistant turn for a conversation."""

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

                # Server request (needs response) â€” has "id" + "method"
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

        # Auto-approve everything â€” we run dangerously
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
            # Token refresh â€” pass back current tokens
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
                    cmd_str = str(cmd_str).replace(
                        r"C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe", "powershell"
                    ).replace(
                        r"C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe", "powershell"
                    )
                    desc = f"Shell(`{cmd_str[:80]}`)"
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
                    cmd_str = str(cmd_str).replace(
                        r"C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe", "powershell"
                    ).replace(
                        r"C:\\WINDOWS\\System32\\WindowsPowerShell\\v1.0\\powershell.exe", "powershell"
                    )
                    desc = f"Shell(`{cmd_str[:80]}`)"
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
                        timeout: float = 72000) -> dict:
        """Send a user message and wait for the turn to complete.
        Default timeout is 20 hours — Codex can work for a very long time.

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

