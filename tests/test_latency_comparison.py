"""Compare OpenRouter vs Claude Code streaming latency — TTFT and total time."""
import asyncio
import json
import os
import subprocess
import sys
import time

import aiohttp
from dotenv import load_dotenv

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
MODEL = os.getenv("VOICE_MODEL", "anthropic/claude-haiku-4.5")

PROMPTS = [
    "Hey, how are you?",
    "What's your favorite color?",
    "Tell me a joke.",
]

SYSTEM_PROMPT = (
    "You are in a voice chat. Keep responses SHORT — 1-2 sentences max. "
    "Be conversational, not monologue-y."
)


async def test_openrouter(prompt: str) -> dict:
    """Test OpenRouter streaming TTFT."""
    t0 = time.time()
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": True,
        "max_tokens": 300,
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

    ttft = None
    full_text = ""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json=payload, headers=headers,
        ) as resp:
            t_http = time.time() - t0
            if resp.status != 200:
                body = await resp.text()
                return {"error": f"{resp.status}: {body[:200]}"}

            async for raw_line in resp.content:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    token = delta.get("content", "")
                    if token:
                        if ttft is None:
                            ttft = time.time() - t0
                        full_text += token
                except json.JSONDecodeError:
                    pass

    total = time.time() - t0
    return {
        "provider": "OpenRouter",
        "model": MODEL,
        "prompt": prompt,
        "http_response_ms": round(t_http * 1000),
        "ttft_ms": round(ttft * 1000) if ttft else None,
        "total_ms": round(total * 1000),
        "chars": len(full_text),
        "text": full_text,
    }


def test_claude_code(prompt: str) -> dict:
    """Test Claude Code CLI streaming TTFT."""
    t0 = time.time()
    cmd = [
        "claude",
        "--print", "--verbose",
        "--output-format", "stream-json",
        "--max-turns", "1",
        "--model", "haiku",
        "-p", prompt,
    ]

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )

    ttft = None
    full_text = ""
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        etype = event.get("type", "")
        if etype == "assistant" and "message" in event:
            for block in event["message"].get("content", []):
                if block.get("type") == "text" and block.get("text"):
                    if ttft is None:
                        ttft = time.time() - t0
                    full_text += block["text"]
        elif etype == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta" and delta.get("text"):
                if ttft is None:
                    ttft = time.time() - t0
                full_text += delta["text"]

    proc.wait()
    total = time.time() - t0
    return {
        "provider": "Claude Code",
        "model": "haiku",
        "prompt": prompt,
        "ttft_ms": round(ttft * 1000) if ttft else None,
        "total_ms": round(total * 1000),
        "chars": len(full_text),
        "text": full_text,
    }


async def main():
    print("=" * 70)
    print("LATENCY COMPARISON: OpenRouter vs Claude Code")
    print("=" * 70)

    for prompt in PROMPTS:
        print(f"\nPrompt: {prompt!r}")
        print("-" * 50)

        # OpenRouter
        or_result = await test_openrouter(prompt)
        if "error" in or_result:
            print(f"  OpenRouter: ERROR {or_result['error']}")
        else:
            print(f"  OpenRouter:  TTFT={or_result['ttft_ms']}ms  Total={or_result['total_ms']}ms  Chars={or_result['chars']}")
            print(f"    HTTP resp: {or_result['http_response_ms']}ms")
            print(f"    Text: {or_result['text'][:100]}")

        # Claude Code
        cc_result = test_claude_code(prompt)
        print(f"  Claude Code: TTFT={cc_result['ttft_ms']}ms  Total={cc_result['total_ms']}ms  Chars={cc_result['chars']}")
        print(f"    Text: {cc_result['text'][:100]}")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    asyncio.run(main())
