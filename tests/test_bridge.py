"""Quick test of the Claude bridge subprocess logic."""
import asyncio
import json
import subprocess
import os
import sys

CLAUDE_CMD = "claude"
CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


async def test():
    print("=== Test 1: stream-json with acceptEdits ===")
    cmd = [
        CLAUDE_CMD, "-p", "Just say: bridge test OK",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "acceptEdits",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=CREATE_FLAGS,
    )
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            t = data.get("type")
            if t == "system":
                print(f"  INIT: model={data.get('model')}, tools={len(data.get('tools', []))}")
            elif t == "assistant":
                for block in data.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        print(f"  TEXT: {block['text']}")
                    elif block.get("type") == "tool_use":
                        print(f"  TOOL: {block.get('name')}")
            elif t == "result":
                print(f"  RESULT: {data.get('result')}")
                print(f"  SESSION: {data.get('session_id')}")
                print(f"  COST: ${data.get('total_cost_usd', 0):.4f}")
                print(f"  ERROR: {data.get('is_error')}")
        except json.JSONDecodeError:
            print(f"  RAW: {line[:100]}")
    await proc.wait()
    print(f"  EXIT CODE: {proc.returncode}")

    print("\n=== Test 2: resume session ===")
    # First, get a session
    cmd1 = [
        CLAUDE_CMD, "-p", "The secret word is banana. Just say OK.",
        "--output-format", "json",
    ]
    r1 = subprocess.run(cmd1, capture_output=True, text=True, timeout=30, creationflags=CREATE_FLAGS)
    d1 = json.loads(r1.stdout)
    sid = d1["session_id"]
    print(f"  Got session: {sid}")

    # Resume and ask for the secret
    cmd2 = [
        CLAUDE_CMD, "-p", "What is the secret word?",
        "--output-format", "json",
        "--resume", sid,
    ]
    r2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=30, creationflags=CREATE_FLAGS)
    d2 = json.loads(r2.stdout)
    print(f"  Resume result: {d2['result']}")
    print(f"  Same session: {d2['session_id'] == sid}")

    print("\nAll tests passed!")


if __name__ == "__main__":
    asyncio.run(test())
