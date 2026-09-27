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
        self._thinking_parts: list[str] = []  # thinking summary blocks
        self.last_thinking_snapshot = ""  # for delta tracking thinking summaries
        self.tools: list[str] = []
        self._seen_tool_ids: set[str] = set()  # deduplicate tool_use with partial messages
        self.result: dict | None = None
        self.done = asyncio.Event()
        self.on_text = None             # async fn(full_text_so_far)
        self.on_tool = None             # async fn(tool_description_str)
        # model tracking — detects safeguard-triggered mid-turn model switches
        # (e.g. fable -> opus fallback). observed_model = model of the first
        # assistant message this turn; model_switched = (old, new) if it changes.
        self.observed_model: str = ""
        self.model_switched: tuple | None = None
        self.safeguard_flag: dict | None = None  # populated from model_refusal_fallback event
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

    @property
    def thinking(self) -> str:
        """Join accumulated thinking summary parts."""
        if len(self._thinking_parts) > 1:
            self._thinking_parts = ["".join(self._thinking_parts)]
        return self._thinking_parts[0] if self._thinking_parts else ""

    def append_thinking(self, delta: str):
        """Append a thinking summary delta."""
        self._thinking_parts.append(delta)


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
        self._injections: list[str] = []  # messages injected during current turn
        self.on_unsolicited = None  # async fn(text, tools) — called when Claude responds between turns

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
        # Extra CLI args from env (e.g. CLAUDE_EXTRA_ARGS="--tools ''")
        env_extra = os.environ.get("CLAUDE_EXTRA_ARGS", "").strip()
        if env_extra:
            import shlex
            cmd += shlex.split(env_extra)
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
            limit=64 * 1024 * 1024,  # 64MB — images come as base64 in single JSON lines
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

                if msg_type == "system":
                    subtype = data.get("subtype")
                    if subtype == "init":
                        sid = data.get("session_id")
                        if sid:
                            self.session_id = sid
                    elif subtype in ("thinking_tokens", "status"):
                        pass  # per-token / request-status chatter, not diagnostic
                    elif subtype == "model_refusal_fallback":
                        # Safeguard flag fired — capture the full event
                        flag_info = {
                            "category": data.get("api_refusal_category", "unknown"),
                            "explanation": data.get("api_refusal_explanation"),
                            "original_model": data.get("original_model", ""),
                            "fallback_model": data.get("fallback_model", ""),
                            "trigger": data.get("trigger", ""),
                            "scope": data.get("scope", ""),
                            "refused_uuid": data.get("refused_user_message_uuid", ""),
                            "request_id": data.get("request_id", ""),
                        }
                        log.warning(
                            f"SAFEGUARD FLAG for {self.ctx_key}: "
                            f"category={flag_info['category']} "
                            f"{flag_info['original_model']} -> {flag_info['fallback_model']}"
                        )
                        if turn:
                            turn.safeguard_flag = flag_info
                        try:
                            log.warning(
                                f"System msg (subtype={subtype}) for {self.ctx_key}: "
                                f"{json.dumps(data)[:2000]}"
                            )
                        except Exception:
                            pass
                    else:
                        # Don't drop non-init system messages silently — this is
                        # where a safeguard/model-switch notice may arrive in
                        # headless stream-json. Log the full payload so the next
                        # real flag is captured with fidelity.
                        try:
                            log.warning(
                                f"System msg (subtype={subtype}) for {self.ctx_key}: "
                                f"{json.dumps(data)[:2000]}"
                            )
                        except Exception:
                            pass
                    continue

                if not turn:
                    # Between turns — handle unsolicited responses (e.g. background task completions).
                    # Claude Code processes task-notifications internally and streams a response
                    # even without a user prompt. Capture and relay via callback.
                    if msg_type == "assistant" and self.on_unsolicited:
                        for block in data.get("message", {}).get("content", []):
                            bt = block.get("type")
                            if bt == "text" and block.get("text"):
                                # accumulate in a buffer, dispatch on result
                                if not hasattr(self, "_unsolicited_text"):
                                    self._unsolicited_text = []
                                    self._unsolicited_tools = []
                                self._unsolicited_text.append(block["text"])
                            elif bt == "tool_use":
                                if not hasattr(self, "_unsolicited_tools"):
                                    self._unsolicited_text = []
                                    self._unsolicited_tools = []
                                name = block.get("name", "?")
                                inp = block.get("input", {})
                                self._unsolicited_tools.append(_tool_description(name, inp))
                    elif msg_type == "result" and self.on_unsolicited:
                        text = "".join(getattr(self, "_unsolicited_text", []))
                        tools = getattr(self, "_unsolicited_tools", [])
                        self._unsolicited_text = []
                        self._unsolicited_tools = []
                        sid = data.get("session_id")
                        if sid:
                            self.session_id = sid
                        if text or tools:
                            try:
                                await self.on_unsolicited(text, tools)
                            except Exception:
                                log.exception("on_unsolicited callback error")
                    continue

                if msg_type == "assistant":
                    # extract token usage from message.usage
                    usage = data.get("message", {}).get("usage")
                    if usage:
                        turn.input_tokens = usage.get("input_tokens", 0)
                        turn.cache_creation_tokens = usage.get("cache_creation_input_tokens", 0)
                        turn.cache_read_tokens = usage.get("cache_read_input_tokens", 0)
                    # detect a mid-turn model switch (safeguard fallback, e.g.
                    # fable -> opus). Compare observed-to-observed within the turn
                    # so it's robust to alias-vs-full-id differences.
                    msg_model = data.get("message", {}).get("model")
                    if msg_model:
                        if not turn.observed_model:
                            turn.observed_model = msg_model
                        elif msg_model != turn.observed_model and turn.model_switched is None:
                            turn.model_switched = (turn.observed_model, msg_model)
                            log.warning(
                                f"MODEL SWITCH mid-turn for {self.ctx_key}: "
                                f"{turn.observed_model} -> {msg_model}"
                            )
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
                        elif bt == "thinking" and block.get("thinking"):
                            thinking_text = block["thinking"]
                            if thinking_text.startswith(turn.last_thinking_snapshot):
                                delta = thinking_text[len(turn.last_thinking_snapshot):]
                            else:
                                delta = thinking_text
                            turn.last_thinking_snapshot = thinking_text
                            turn.append_thinking(delta)
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
                        "thinking": turn.thinking,
                        "input_tokens": turn.input_tokens,
                        "cache_creation_tokens": turn.cache_creation_tokens,
                        "cache_read_tokens": turn.cache_read_tokens,
                        "total_tokens": total_tokens,
                        "observed_model": turn.observed_model,
                        "model_switched": turn.model_switched,
                        "safeguard_flag": turn.safeguard_flag,
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
            self._injections.append(prompt)  # track for drain if Claude misses it
        except Exception as e:
            log.warning(f"Failed to inject message into {self.ctx_key}: {e}")

    def pop_injections(self) -> list[str]:
        """Return and clear any messages injected during the last turn."""
        inj = self._injections
        self._injections = []
        return inj

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


def _is_tool_result_row(row: dict) -> bool:
    """True if a transcript row is a user row carrying tool_result blocks."""
    if row.get("type") != "user":
        return False
    content = row.get("message", {}).get("content")
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


def rewind_transcript(cwd: str, session_id: str, refused_uuid: str = "") -> dict:
    """Truncate a session transcript to just before the flagged step.

    Granular: tool calls are their own units. If the refused message is a
    tool_result row, the cut lands on its PARENT assistant message (the tool
    call that caused it) — a 200-call agent loop only loses the bad step, not
    the whole chain. If the refused message is the actual user prompt, the cut
    lands there (full rewind of the turn).

    Returns {"ok": True, "mode": "prompt"|"step", ...} so the caller knows
    whether to resend the original prompt or a continuation nudge, or
    {"ok": False, "reason": ...}. The original transcript is backed up first.
    """
    if not session_id:
        return {"ok": False, "reason": "no session id"}
    projects = Path.home() / ".claude" / "projects"
    candidates = list(projects.glob(f"*/{session_id}.jsonl"))
    if not candidates:
        return {"ok": False, "reason": f"transcript not found for session {session_id}"}
    path = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        return {"ok": False, "reason": f"read failed: {e}"}

    rows = []  # (parsed_obj_or_None, raw_line)
    uuids = {}
    for i, line in enumerate(raw_lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            obj = None
        rows.append((obj, line))
        if obj and obj.get("uuid"):
            uuids[obj["uuid"]] = i

    cut_idx = None
    cut_uuid = ""
    mode = None
    if refused_uuid and refused_uuid in uuids:
        i = uuids[refused_uuid]
        row = rows[i][0]
        if _is_tool_result_row(row):
            # the flagged content is a tool result — rewind the tool CALL that
            # produced it (its parent assistant message), not the whole chain
            parent = row.get("parentUuid")
            cut_idx = uuids.get(parent, i)
            mode = "step"
            cut_uuid = rows[cut_idx][0].get("uuid", "") if rows[cut_idx][0] else ""
        else:
            # the user prompt itself was refused — rewind the whole turn
            cut_idx = i
            mode = "prompt"
            cut_uuid = refused_uuid
    else:
        # no uuid (model-switch-only detection): redo just the final step
        for i in range(len(rows) - 1, -1, -1):
            if rows[i][0] and rows[i][0].get("type") == "assistant":
                cut_idx = i
                mode = "step"
                cut_uuid = rows[i][0].get("uuid", "")
                break
        if cut_idx is None:
            # no assistant rows at all — cut at the last real user message
            for i in range(len(rows) - 1, -1, -1):
                obj = rows[i][0]
                if obj and obj.get("type") == "user" and not _is_tool_result_row(obj):
                    cut_idx = i
                    mode = "prompt"
                    cut_uuid = obj.get("uuid", "")
                    break
    if cut_idx is None:
        return {"ok": False, "reason": "nothing to rewind to", "path": str(path)}

    removed = len(raw_lines) - cut_idx
    backup = path.with_suffix(f".prerewind-{int(_time.time())}.jsonl")
    try:
        backup.write_text("\n".join(raw_lines) + "\n", encoding="utf-8")
        path.write_text("".join(line + "\n" for line in raw_lines[:cut_idx]), encoding="utf-8")
    except Exception as e:
        return {"ok": False, "reason": f"write failed: {e}", "path": str(path)}
    if log:
        log.warning(
            f"Rewound transcript {path.name} ({mode}): removed {removed} line(s) "
            f"from uuid {cut_uuid or '?'} (backup: {backup.name})"
        )
    return {"ok": True, "mode": mode, "path": str(path), "removed": removed,
            "cut_uuid": cut_uuid, "backup": str(backup)}


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
        keys = list(self._procs.keys())
        if not keys:
            return
        await asyncio.gather(
            *(self.kill_process(key) for key in keys),
            return_exceptions=True,
        )

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
    elif name in ("Bash", "PowerShell") and "command" in inp:
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
    elif name == "Agent" and "prompt" in inp:
        agent_type = inp.get("subagent_type", "")
        agent_desc = inp.get("description", inp["prompt"][:40])
        if agent_type == "codex":
            desc = f"Codex({agent_desc})"
        else:
            desc += f"({agent_desc})"
    return desc


# â”€â”€ System Monitor â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
