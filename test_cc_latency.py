"""Measure Claude Code warm-process latency (what voice actually sees)."""
import asyncio, json, time, sys

CREATE_FLAGS = 0x08000000

async def test(label, extra_args=None):
    print(f"\n{'='*50}")
    print(f"  {label}")
    print(f"{'='*50}")

    cmd = [
        "claude", "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--include-partial-messages",
    ]
    if extra_args:
        cmd += extra_args

    print(f"  CMD: {' '.join(cmd)}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=CREATE_FLAGS,
    )

    # Read stderr in background so it doesn't block
    async def drain_stderr():
        lines = []
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            lines.append(line.decode(errors="replace").strip())
        return lines

    stderr_task = asyncio.create_task(drain_stderr())

    # Drain all init messages
    while True:
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=120)
        except asyncio.TimeoutError:
            print("  TIMEOUT waiting for init")
            proc.terminate()
            return
        if not line:
            stderr_lines = await stderr_task
            print(f"  Process died during init. Stderr:")
            for sl in stderr_lines[-10:]:
                print(f"    {sl}")
            return
        try:
            d = json.loads(line)
            if d.get("type") == "system" and d.get("subtype") == "init":
                print(f"  Process ready (pid={proc.pid})")
                break
        except:
            pass

    # Warm up with a throwaway message
    warmup = json.dumps({"type": "user", "message": {"role": "user", "content": "Say hi in one short sentence."}})
    try:
        proc.stdin.write((warmup + "\n").encode())
        await proc.stdin.drain()
    except ConnectionResetError:
        stderr_lines = await stderr_task
        print(f"  Process died before warmup. Stderr:")
        for sl in stderr_lines[-10:]:
            print(f"    {sl}")
        return

    print("  Warming up...")
    while True:
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=120)
        except asyncio.TimeoutError:
            print("  TIMEOUT during warmup")
            proc.terminate()
            return
        if not line:
            stderr_lines = await stderr_task
            print(f"  Process died during warmup. Stderr:")
            for sl in stderr_lines[-10:]:
                print(f"    {sl}")
            return
        try:
            d = json.loads(line)
            if d.get("type") == "result":
                print("  Warmup done")
                break
        except:
            pass

    # Now measure real latency on warm process
    for prompt in ["Hey! What's up?", "Tell me a joke", "What's 2+2?"]:
        msg = json.dumps({"type": "user", "message": {"role": "user", "content": prompt}})
        t0 = time.time()
        try:
            proc.stdin.write((msg + "\n").encode())
            await proc.stdin.drain()
        except ConnectionResetError:
            print(f"  Process died before sending '{prompt}'")
            return

        ft = None; rt = None; txt = ""; types = []
        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=120)
            except asyncio.TimeoutError:
                print(f"  TIMEOUT on '{prompt}'"); break
            if not line:
                print(f"  Process died during '{prompt}'"); break
            try:
                d = json.loads(line)
            except:
                continue
            tp = d.get("type")
            e = time.time() - t0
            if tp == "assistant":
                for b in d.get("message", {}).get("content", []):
                    bt = b.get("type")
                    if bt == "thinking" and not any("think" in x for x in types):
                        types.append(f"think@{e*1000:.0f}")
                    if bt == "text" and b.get("text"):
                        if ft is None:
                            ft = e; txt = b["text"][:80]
                            types.append(f"text@{e*1000:.0f}")
            elif tp == "result":
                rt = e; types.append(f"done@{e*1000:.0f}"); break

        flow = " → ".join(types)
        if ft is not None and rt is not None:
            print(f"  '{prompt}': TTFT={ft*1000:.0f}ms total={rt*1000:.0f}ms [{flow}]")
            print(f"    first text: {txt!r}")
        else:
            print(f"  '{prompt}': FAILED [{flow}]")

    proc.terminate()
    print("  Done.")

async def main():
    # Test one at a time - each spawns its own process
    await test("Opus default (thinking enabled)")
    await test("Opus no-thinking", ["--model", "claude-opus-4-6", "--settings", '{"thinking":false}'])
    await test("Sonnet no-thinking", ["--model", "claude-sonnet-4-5-20250929", "--settings", '{"thinking":false}'])
    await test("Haiku", ["--model", "claude-haiku-4-5-20251001"])

asyncio.run(main())
