"""Claude CLI bridge (no Discord dependencies)."""

import asyncio
import json
import os
import signal
import subprocess
import time as _time
from pathlib import Path

CLAUDE_CMD = "claude"
CLAUDE_MODEL = ""
# CREATE_NEW_PROCESS_GROUP isolates the CLI so interrupt signals
# can't cascade to the bot, supervisor, or unrelated processes.
CREATE_FLAGS = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
log = None


def configure(logger, claude_cmd: str, claude_model: str, create_flags: int):
    global log, CLAUDE_CMD, CLAUDE_MODEL, CREATE_FLAGS
    log = logger
    CLAUDE_CMD = claude_cmd
    CLAUDE_MODEL = claude_model
    CREATE_FLAGS = create_flags

class _TurnState:
    """Tracks the state of a single userâ†’assistant turn."""

    def __init__(self):
        self._text_parts: list[str] = []  # accumulate deltas in a list (avoids O(N^2) string concat)
        self.last_text_snapshot = ""    # for delta tracking within a turn
        self.tools: list[str] = []
        self._seen_tool_ids: set[str] = set()  # deduplicate tool_use with partial messages
        self.result: dict | None = None
        self.done = asyncio.Event()
        self.on_text = None             # async fn(full_text_so_far)
        self.on_tool = None             # async fn(tool_description_str)
        # token usage (updated from assistant.message.usage)
        self.input_tokens: int = 0
        self.cache_creation_tokens: int = 0
        self.cache_read_tokens: int = 0

    @property
    def text(self) -> str:
        """Join accumulated text parts. Collapses list to avoid repeated joins."""
        if len(self._text_parts) > 1:
            self._text_parts = ["".join(self._text_parts)]
        return self._text_parts[0] if self._text_parts else ""

    def append_text(self, delta: str):
        """Append a delta without copying the full accumulated string."""
        self._text_parts.append(delta)


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
        self._created_at: float = 0.0  # set in start()

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
        # Write system prompt to a temp file and pass via --system-prompt-file
        # (avoids Windows command-line length limit AND uses proper system prompt mechanism)
        if self.system_prompt:
            import tempfile
            self._sys_prompt_file = tempfile.NamedTemporaryFile(
                mode="w", suffix=".md", delete=False, encoding="utf-8",
                dir=str(Path(os.environ.get("TEMP", "/tmp"))),
            )
            self._sys_prompt_file.write(self.system_prompt)
            self._sys_prompt_file.close()
            cmd += ["--system-prompt-file", self._sys_prompt_file.name]
        if session_id:
            cmd += ["--resume", session_id]
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
        self._created_at = _time.time()
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        log.info(f"Persistent process started for {self.ctx_key} (pid={self.proc.pid})")

    async def _drain_stderr(self):
        """Continuously drain stderr to prevent pipe buffer deadlock.

        On Windows, pipe buffers are ~64KB. If claude.exe writes enough to stderr
        to fill this buffer and nobody reads it, claude.exe blocks on the write.
        This blocks ALL I/O from claude.exe (including stdout), which deadlocks
        the entire bridge. We keep the last chunk for crash diagnostics.
        """
        self._last_stderr = ""
        try:
            while self._alive and self.proc and self.proc.returncode is None:
                data = await self.proc.stderr.read(8192)
                if not data:
                    break
                self._last_stderr = data.decode("utf-8", errors="replace").strip()[-1000:]
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _read_loop(self):
        """Background task: read NDJSON from stdout, dispatch to current turn."""
        try:
            while self._alive and self.proc and self.proc.returncode is None:
                # No timeout â€” tool calls can run for hours (training, research, etc.)
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
                    # extract token usage from message.usage
                    usage = data.get("message", {}).get("usage")
                    if usage:
                        turn.input_tokens = usage.get("input_tokens", 0)
                        turn.cache_creation_tokens = usage.get("cache_creation_input_tokens", 0)
                        turn.cache_read_tokens = usage.get("cache_read_input_tokens", 0)
                    for block in data.get("message", {}).get("content", []):
                        bt = block.get("type")
                        if bt == "text" and block.get("text"):
                            block_text = block["text"]
                            if block_text.startswith(turn.last_text_snapshot):
                                delta = block_text[len(turn.last_text_snapshot):]
                            else:
                                delta = block_text
                            turn.last_text_snapshot = block_text
                            turn.append_text(delta)
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
                    total_tokens = turn.input_tokens + turn.cache_creation_tokens + turn.cache_read_tokens
                    turn.result = {
                        "text": data.get("result", turn.text),
                        "session_id": sid,
                        "cost_usd": cost,
                        "error": data.get("is_error", False),
                        "error_message": data.get("result", "") if data.get("is_error") else "",
                        "tools": turn.tools,
                        "input_tokens": turn.input_tokens,
                        "cache_creation_tokens": turn.cache_creation_tokens,
                        "cache_read_tokens": turn.cache_read_tokens,
                        "total_tokens": total_tokens,
                    }
                    turn.done.set()

        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception(f"Reader loop error for {self.ctx_key}")
        finally:
            self._alive = False
            # use stderr captured by _drain_stderr task
            stderr_text = getattr(self, "_last_stderr", "")
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

            # wait for the result event â€” no total timeout here.
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
    def total_cost(self) -> float:
        return self._total_cost

    @property
    def alive(self) -> bool:
        return self._alive and self.proc is not None and self.proc.returncode is None

    async def interrupt(self):
        """Interrupt the current response (like pressing Escape in Claude Code).
        Process stays alive with context preserved."""
        if self.proc and self.proc.returncode is None and self._turn:
            # CTRL_BREAK_EVENT targets only the process's own group
            # (safe because CLI is started with CREATE_NEW_PROCESS_GROUP).
            # CTRL_C_EVENT is ignored for new process groups on Windows.
            if os.name == "nt":
                try:
                    os.kill(self.proc.pid, signal.CTRL_BREAK_EVENT)
                except OSError as e:
                    # Can happen transiently if process handle is already closing.
                    log.warning(f"interrupt failed for {self.ctx_key} pid={self.proc.pid}: {e}")
            else:
                self.proc.send_signal(signal.SIGINT)

    async def kill(self):
        """Terminate the process."""
        self._alive = False
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.kill()
            except Exception:
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except (asyncio.TimeoutError, Exception):
                pass
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        stderr_task = getattr(self, "_stderr_task", None)
        if stderr_task and not stderr_task.done():
            stderr_task.cancel()
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

    async def cleanup_dead(self):
        """Remove dead processes from the cache to free memory."""
        dead = [k for k, pp in self._procs.items() if not pp.alive]
        for k in dead:
            pp = self._procs.pop(k, None)
            if pp:
                await pp.kill()

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
    elif name == "WebSearch" and "query" in inp:
        desc += f"(`{inp['query'][:60]}`)"
    elif name == "WebFetch" and "url" in inp:
        desc += f"(`{inp['url'][:80]}`)"
    return desc


# â”€â”€ System Monitor â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

