"""
Test Claude Code interrupt + resume latency.
"""
import asyncio
import json
import time
import os
import signal

CLAUDE_CMD = "claude"
CREATE_FLAGS = 0x08000000 if os.name == "nt" else 0

async def main():
    print("=" * 60)
    print("CLAUDE CODE INTERRUPT TEST")
    print("=" * 60)

    cmd = [
        CLAUDE_CMD, "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--include-partial-messages",
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=os.path.expanduser("~"),
        creationflags=CREATE_FLAGS,
    )
    print(f"Started pid={proc.pid}")

    async def drain_stderr():
        try:
            while True:
                d = await proc.stderr.read(4096)
                if not d:
                    break
        except:
            pass
    asyncio.create_task(drain_stderr())

    # Warmup: simple question
    msg1 = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": "What is 2+2? Just the number."},
    })
    proc.stdin.write((msg1 + "\n").encode("utf-8"))
    await proc.stdin.drain()

    t0 = time.perf_counter()
    session_id = None

    while time.perf_counter() - t0 < 30:
        try:
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=2)
        except asyncio.TimeoutError:
            continue
        if not raw:
            break
        line = raw.decode(errors="replace").strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = data.get("type")
        if t == "system" and data.get("subtype") == "init":
            session_id = data.get("session_id", "")
            print(f"  INIT {time.perf_counter()-t0:.2f}s  sid={session_id[:16]}...")
        elif t == "result":
            print(f"  Warmup done in {time.perf_counter()-t0:.2f}s")
            session_id = data.get("session_id", session_id)
            break

    # ── PHASE 2: Long response ──
    print(f"\nPHASE 2: Long essay request")
    msg2 = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": "Write a very detailed 3000-word essay about the complete history of mathematics from ancient civilizations through modern day. Be extremely thorough and cover every era."},
    })
    proc.stdin.write((msg2 + "\n").encode("utf-8"))
    await proc.stdin.drain()

    t1 = time.perf_counter()
    text_len = 0
    got_first = False
    token_count = 0

    # Stream until we have substantial content
    while time.perf_counter() - t1 < 10:
        try:
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=0.5)
        except asyncio.TimeoutError:
            continue
        if not raw:
            break
        line = raw.decode(errors="replace").strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        st = data.get("type")
        if st == "assistant":
            for b in data.get("message", {}).get("content", []):
                if b.get("type") == "text" and b.get("text"):
                    token_count += 1
                    text_len = len(b["text"])
                    if not got_first:
                        print(f"  TTFT: {time.perf_counter()-t1:.2f}s")
                        got_first = True
        elif st == "result":
            print(f"  Finished naturally in {time.perf_counter()-t1:.2f}s (too fast!)")
            break

        # Once we have enough content, interrupt
        if text_len > 200:
            break

    print(f"  Got {text_len} chars in {token_count} chunks before interrupt attempt")

    if text_len < 50:
        print("  Not enough streaming data, skipping interrupt test")
        proc.terminate()
        return

    # ── INTERRUPT ATTEMPTS ──
    # Method 1: SIGINT via os.kill (Windows CTRL_C_EVENT)
    print(f"\n  Method 1: CTRL_C_EVENT signal")
    t_int = time.perf_counter()
    try:
        os.kill(proc.pid, signal.CTRL_C_EVENT)
        print(f"    Sent CTRL_C_EVENT")
    except Exception as e:
        print(f"    Failed: {e}")

    # Check for result
    got_it = False
    while time.perf_counter() - t_int < 5:
        try:
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=0.5)
        except asyncio.TimeoutError:
            continue
        if not raw:
            break
        line = raw.decode(errors="replace").strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("type") == "result":
            int_time = time.perf_counter() - t_int
            print(f"    INTERRUPTED in {int_time:.2f}s!")
            got_it = True
            break

    if not got_it:
        print(f"    No effect (or still streaming)")

    # Method 2: CTRL_BREAK_EVENT
    if not got_it and proc.returncode is None:
        print(f"\n  Method 2: CTRL_BREAK_EVENT signal")
        t_int2 = time.perf_counter()
        try:
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
            print(f"    Sent CTRL_BREAK_EVENT")
        except Exception as e:
            print(f"    Failed: {e}")

        while time.perf_counter() - t_int2 < 5:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if not raw:
                break
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "result":
                int_time = time.perf_counter() - t_int2
                print(f"    INTERRUPTED in {int_time:.2f}s!")
                got_it = True
                break

        if not got_it:
            print(f"    No effect")

    # ── PHASE 3: Kill + Respawn fallback ──
    if proc.returncode is None and not got_it:
        print(f"\nPHASE 3: Kill + Respawn")
        t_kill = time.perf_counter()
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        kill_t = time.perf_counter() - t_kill
        print(f"  Kill: {kill_t:.3f}s")

        t_respawn = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            *(cmd + ["--resume", session_id]),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.path.expanduser("~"),
            creationflags=CREATE_FLAGS,
        )
        asyncio.create_task(drain_stderr())

        msg3 = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": "[interrupted] Never mind. Just say hello."},
        })
        proc.stdin.write((msg3 + "\n").encode("utf-8"))
        await proc.stdin.drain()

        got_init = False
        got_text = False
        while time.perf_counter() - t_respawn < 30:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=2)
            except asyncio.TimeoutError:
                continue
            if not raw:
                break
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = data.get("type")
            if t == "system" and data.get("subtype") == "init":
                print(f"  Respawn init: {time.perf_counter()-t_respawn:.2f}s")
            elif t == "assistant" and not got_text:
                for b in data.get("message", {}).get("content", []):
                    if b.get("type") == "text" and b.get("text"):
                        print(f"  Follow-up TTFT: {time.perf_counter()-t_respawn:.2f}s  \"{b['text'][:80]}\"")
                        got_text = True
            elif t == "result":
                total = time.perf_counter() - t_respawn
                print(f"  Follow-up done: {total:.2f}s")
                break

        total_interrupt = time.perf_counter() - t_kill
        print(f"\n  TOTAL kill->respawn->TTFT: {total_interrupt:.2f}s")

    # If signal interrupt worked, test follow-up in same process
    elif got_it and proc.returncode is None:
        print(f"\nPHASE 3: Follow-up in same process (no respawn needed!)")
        msg3 = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": "Are you there? Just say yes."},
        })
        proc.stdin.write((msg3 + "\n").encode("utf-8"))
        await proc.stdin.drain()
        t_fu = time.perf_counter()

        while time.perf_counter() - t_fu < 15:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=2)
            except asyncio.TimeoutError:
                continue
            if not raw:
                break
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = data.get("type")
            if t == "assistant":
                for b in data.get("message", {}).get("content", []):
                    if b.get("type") == "text" and b.get("text"):
                        print(f"  Follow-up TTFT: {time.perf_counter()-t_fu:.2f}s  \"{b['text'][:80]}\"")
                        break
            elif t == "result":
                print(f"  Follow-up done: {time.perf_counter()-t_fu:.2f}s")
                print(f"  PROCESS STAYED ALIVE + CONTEXT PRESERVED!")
                break

    # Cleanup
    print(f"\nDone!")
    if proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            proc.kill()

if __name__ == "__main__":
    asyncio.run(main())
