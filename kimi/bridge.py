"""Kimi CLI bridge (no Discord dependencies).

Kimi uses OpenAI-compatible stream-json: input is {"role":"user","content":"..."},
output is assistant messages with optional tool_calls, tool results, and final text.
A turn ends when an assistant message arrives with no tool_calls field.
"""

import asyncio
import json
import os
import signal
import subprocess
import time as _time
from pathlib import Path

KIMI_CMD = "kimi"
KIMI_MODEL = ""
# CREATE_NEW_PROCESS_GROUP isolates the CLI so interrupt signals
# can't cascade to the bot, supervisor, or unrelated processes.
CREATE_FLAGS = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
KIMI_JSON_PATH = Path.home() / ".kimi" / "kimi.json"
log = None


def configure(logger, kimi_cmd: str, kimi_model: str, create_flags: int):
    global log, KIMI_CMD, KIMI_MODEL, CREATE_FLAGS
    log = logger
    KIMI_CMD = kimi_cmd
    KIMI_MODEL = kimi_model
    CREATE_FLAGS = create_flags


def _read_session_id(cwd: str) -> str | None:
    """Read last session ID for a work dir from ~/.kimi/kimi.json."""
    try:
        if not KIMI_JSON_PATH.exists():
            return None
        data = json.loads(KIMI_JSON_PATH.read_text("utf-8"))
        norm = os.path.normpath(cwd).replace("/", "\\")
        for entry in data.get("work_dirs", []):
            entry_path = os.path.normpath(entry.get("path", "")).replace("/", "\\")
            if entry_path.lower() == norm.lower():
                return entry.get("last_session_id")
    except Exception:
        pass
    return None


class _TurnState:
    """Tracks the state of a single user->assistant turn."""

    def __init__(self):
        self.text = ""
        self.tools: list[str] = []
        self._seen_tool_ids: set[str] = set()
        self.result: dict | None = None
        self.done = asyncio.Event()
        self.on_text = None
        self.on_tool = None
        self._pending_text = ""  # text from assistant messages with tool_calls


class _PersistentProcess:
    """A long-lived Kimi CLI process for a single context (channel/thread)."""

    def __init__(self, ctx_key: str, cwd: str, system_prompt: str = "",
                 model: str = "", extra_env: dict[str, str] = None):
        self.ctx_key = ctx_key
        self.cwd = cwd
        self.system_prompt = system_prompt
        self.model = model
        self.extra_env = extra_env or {}
        self.proc: asyncio.subprocess.Process | None = None
        self.session_id: str | None = None
        self._reader_task: asyncio.Task | None = None
        self._turn: _TurnState | None = None
        self._alive = False
        self._send_lock = asyncio.Lock()
        self._first_msg = True
        self._created_at: float = 0.0

    async def start(self, session_id: str = None):
        """Spawn the kimi process."""
        cmd = [
            KIMI_CMD,
            "--print",
            "--input-format=stream-json",
            "--output-format=stream-json",
            "--yolo",
            f"--work-dir={self.cwd}",
        ]
        model = self.model or KIMI_MODEL
        if model:
            cmd += ["--model", model]
        if session_id:
            cmd += ["--session", session_id]

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        if self.extra_env:
            env.update(self.extra_env)

        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            creationflags=CREATE_FLAGS,
            limit=64 * 1024 * 1024,  # 64 MB — ReadMediaFile can return huge base64 lines
            env=env,
        )
        self._alive = True
        self._created_at = _time.time()
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        log.info(f"Kimi process started for {self.ctx_key} (pid={self.proc.pid})")

    async def _drain_stderr(self):
        """Continuously drain stderr to prevent pipe buffer deadlock."""
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
        """Background task: read NDJSON from stdout, dispatch to current turn.

        Kimi output format (one JSON per line):
        - assistant with tool_calls: {"role":"assistant","content":[],"tool_calls":[...]}
        - tool result: {"role":"tool","content":[...],"tool_call_id":"..."}
        - final assistant text: {"role":"assistant","content":"the response text"}
        """
        try:
            while self._alive and self.proc and self.proc.returncode is None:
                try:
                    raw = await self.proc.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    # Line exceeded buffer limit (e.g. base64 media) — skip it
                    log.warning(f"Oversized line skipped for {self.ctx_key}")
                    # Drain the oversized data so the stream isn't stuck
                    try:
                        await self.proc.stdout.readuntil(b"\n")
                    except Exception:
                        pass
                    continue
                if not raw:
                    break

                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                role = data.get("role")
                turn = self._turn
                if not turn:
                    continue

                if role == "assistant":
                    tool_calls = data.get("tool_calls")
                    content = data.get("content", "")

                    if tool_calls:
                        # Assistant is making tool calls — extract tool descriptions
                        for tc in tool_calls:
                            tc_id = tc.get("id", "")
                            if tc_id and tc_id in turn._seen_tool_ids:
                                continue
                            if tc_id:
                                turn._seen_tool_ids.add(tc_id)
                            func = tc.get("function", {})
                            name = func.get("name", "?")
                            args_str = func.get("arguments", "")
                            desc = _tool_description(name, args_str)
                            turn.tools.append(desc)
                            if turn.on_tool:
                                try:
                                    await turn.on_tool(desc)
                                except Exception:
                                    log.exception("on_tool callback error")
                    else:
                        # Final assistant message (no tool_calls) — turn is done
                        if isinstance(content, str) and content:
                            turn.text = content
                            if turn.on_text:
                                try:
                                    await turn.on_text(turn.text)
                                except Exception:
                                    log.exception("on_text callback error")

                        # Read session ID from kimi.json (not in stream output)
                        sid = _read_session_id(self.cwd)
                        if sid:
                            self.session_id = sid

                        turn.result = {
                            "text": turn.text,
                            "session_id": self.session_id,
                            "cost_usd": 0,
                            "error": False,
                            "error_message": "",
                            "tools": turn.tools,
                            "input_tokens": 0,
                            "cache_creation_tokens": 0,
                            "cache_read_tokens": 0,
                            "total_tokens": 0,
                        }
                        turn.done.set()

                elif role == "tool":
                    # Tool result — just log it, kimi handles the loop
                    pass

        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception(f"Reader loop error for {self.ctx_key}")
        finally:
            self._alive = False
            stderr_text = getattr(self, "_last_stderr", "")
            rc = self.proc.returncode if self.proc else "?"
            if stderr_text:
                log.warning(f"Kimi {self.ctx_key} stderr (rc={rc}): {stderr_text[:500]}")
            if self._turn and not self._turn.done.is_set():
                err_msg = stderr_text[:500] if stderr_text else "Process ended unexpectedly"
                if self._turn.result is None:
                    self._turn.result = {
                        "text": self._turn.text, "session_id": self.session_id,
                        "cost_usd": 0, "error": True,
                        "error_message": err_msg, "tools": self._turn.tools,
                    }
                self._turn.done.set()
            log.info(f"Kimi reader loop ended for {self.ctx_key} (rc={rc})")

    async def send(self, prompt: str, on_text=None, on_tool=None) -> dict:
        """Send a user message and wait for the result."""
        async with self._send_lock:
            if not self._alive or not self.proc or self.proc.returncode is not None:
                return {
                    "text": "", "session_id": self.session_id, "cost_usd": 0,
                    "error": True, "error_message": "Process not running", "tools": [],
                }

            content = prompt
            if self._first_msg and self.system_prompt:
                content = f"{self.system_prompt}\n\n{prompt}"
                self._first_msg = False

            turn = _TurnState()
            turn.on_text = on_text
            turn.on_tool = on_tool
            self._turn = turn

            msg = json.dumps({"role": "user", "content": content})
            try:
                self.proc.stdin.write((msg + "\n").encode("utf-8"))
                await self.proc.stdin.drain()
            except Exception as e:
                self._turn = None
                return {
                    "text": "", "session_id": self.session_id, "cost_usd": 0,
                    "error": True, "error_message": f"Failed to write to stdin: {e}", "tools": [],
                }

            await turn.done.wait()

            self._turn = None
            return turn.result or {
                "text": turn.text, "session_id": self.session_id, "cost_usd": 0,
                "error": True, "error_message": "No result received", "tools": turn.tools,
            }

    async def inject(self, prompt: str):
        """Inject a user message mid-turn."""
        if not self._alive or not self.proc or self.proc.returncode is not None:
            return
        msg = json.dumps({"role": "user", "content": prompt})
        try:
            self.proc.stdin.write((msg + "\n").encode("utf-8"))
            await self.proc.stdin.drain()
        except Exception as e:
            log.warning(f"Failed to inject message into {self.ctx_key}: {e}")

    @property
    def is_busy(self) -> bool:
        return self._send_lock.locked()

    @property
    def alive(self) -> bool:
        return self._alive and self.proc is not None and self.proc.returncode is None

    async def interrupt(self):
        """Send CTRL+C to interrupt current turn."""
        if self.proc and self.proc.returncode is None and self._turn:
            if os.name == "nt":
                os.kill(self.proc.pid, signal.CTRL_BREAK_EVENT)
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
                await self.proc.wait()
            except Exception:
                pass
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        stderr_task = getattr(self, "_stderr_task", None)
        if stderr_task and not stderr_task.done():
            stderr_task.cancel()
        log.info(f"Killed kimi process for {self.ctx_key}")


class KimiBridge:
    """Manages persistent Kimi CLI processes per context."""

    def __init__(self):
        self._procs: dict[str, _PersistentProcess] = {}

    async def get_or_create(
        self, ctx_key: str, cwd: str, session_id: str = None,
        system_prompt: str = "", model: str = "",
        extra_env: dict[str, str] = None,
    ) -> _PersistentProcess:
        pp = self._procs.get(ctx_key)
        if pp and pp.alive:
            return pp
        if pp:
            await pp.kill()
        pp = _PersistentProcess(ctx_key, cwd, system_prompt, model=model,
                                extra_env=extra_env)
        await pp.start(session_id)
        self._procs[ctx_key] = pp
        return pp

    async def kill_process(self, ctx_key: str):
        pp = self._procs.pop(ctx_key, None)
        if pp:
            await pp.kill()

    async def kill_all(self):
        for key in list(self._procs.keys()):
            await self.kill_process(key)

    def get_process(self, ctx_key: str) -> _PersistentProcess | None:
        pp = self._procs.get(ctx_key)
        if pp and pp.alive:
            return pp
        return None


def _tool_description(name: str, args_str: str) -> str:
    """Human-readable one-liner for a tool call."""
    try:
        args = json.loads(args_str) if args_str else {}
    except (json.JSONDecodeError, TypeError):
        args = {}

    if name == "Shell" and "command" in args:
        cmd = args["command"][:60].replace("\n", " ")
        return f"Shell(`{cmd}`)"
    elif name in ("ReadFile", "read_file") and "path" in args:
        return f"Read({Path(args['path']).name})"
    elif name in ("WriteFile", "write_file") and "path" in args:
        return f"Write({Path(args['path']).name})"
    elif name in ("StrReplaceFile", "str_replace_file") and "path" in args:
        return f"Edit({Path(args['path']).name})"
    elif name in ("Glob", "glob") and "pattern" in args:
        return f"Glob({args['pattern'][:30]})"
    elif name in ("Grep", "grep") and "pattern" in args:
        return f"Grep({args['pattern'][:30]})"
    elif name in ("SearchWeb", "search_web") and "query" in args:
        return f"Search({args['query'][:30]})"
    elif name in ("FetchURL", "fetch_url") and "url" in args:
        return f"Fetch({args['url'][:40]})"
    elif name == "Task":
        desc = args.get("description", args.get("prompt", ""))[:30]
        return f"Task({desc})" if desc else "Task()"
    else:
        return f"{name}()"
