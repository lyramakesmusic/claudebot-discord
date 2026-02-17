#!/usr/bin/env python3
"""
Minimal LLM completion caller. Prompt goes in, completions come out.

Usage:
    # Direct prompt, 8 branches from same seed
    python eval_runner.py "Her name was" --n 8 --tokens 40

    # Same thing with --branches alias
    python eval_runner.py "." --branches 100 --tokens 40

    # Pipe prompt in
    echo "." | python eval_runner.py --n 8

    # Nest: use output of one call as input to another
    python eval_runner.py "$(python eval_runner.py 'Once upon a' --n 1 --raw)" --n 4

    # Read prompt from file
    python eval_runner.py --file seed.txt --n 4

    # Chat mode instead of completions
    python eval_runner.py "Hello!" --chat --n 1

    # Override endpoint/model
    EVAL_BASE_URL=http://localhost:1234/v1 EVAL_MODEL=local python eval_runner.py "test"

Config via env:
    EVAL_BASE_URL  (default: https://inference.ggb-dev-site.com/v1)
    EVAL_MODEL     (default: moonshotai/Kimi-K2-Base)
    EVAL_API_KEY   (default: from OPENROUTER_API_KEY in .env)
"""
import httpx, asyncio, json, sys, os, io
from pathlib import Path

# Load API keys from known .env locations
for p in [Path.home()/"Documents"/"claudebot"/".env", Path.home()/"Documents"/"model_call_mcp"/".env"]:
    if p.exists():
        for line in p.read_text().splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

BASE_URL = os.getenv("EVAL_BASE_URL", "https://inference.ggb-dev-site.com/v1")
MODEL = os.getenv("EVAL_MODEL", "moonshotai/Kimi-K2-Base")
API_KEY = os.getenv("EVAL_API_KEY", os.getenv("OPENROUTER_API_KEY", ""))


async def complete(prompt, n=1, max_tokens=60, temperature=1.0, min_p=0.01):
    """Call /completions, return list of strings."""
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    async def one():
        payload = {"model": MODEL, "prompt": prompt, "max_tokens": max_tokens, "temperature": temperature}
        if min_p > 0:
            payload["min_p"] = min_p
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{BASE_URL}/completions", json=payload, headers=headers, timeout=120)
            r.raise_for_status()
            return r.json()["choices"][0]["text"]

    return await asyncio.gather(*[one() for _ in range(n)], return_exceptions=True)


async def chat(messages, n=1, max_tokens=256, temperature=0.7):
    """Call /chat/completions, return list of strings."""
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]

    async def one():
        payload = {"model": MODEL, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{BASE_URL}/chat/completions", json=payload, headers=headers, timeout=120)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

    return await asyncio.gather(*[one() for _ in range(n)], return_exceptions=True)


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

    import argparse
    p = argparse.ArgumentParser(description="Minimal LLM caller. Prompt in, completions out.")
    p.add_argument("prompt", nargs="?", default=None, help="Prompt text (or pipe via stdin)")
    p.add_argument("--file", "-f", help="Read prompt from file")
    p.add_argument("--n", "-b", "--branches", type=int, default=1, help="Number of parallel completions (branches) from same seed")
    p.add_argument("--tokens", type=int, default=60, help="Max tokens per completion")
    p.add_argument("--temp", type=float, default=1.0, help="Temperature")
    p.add_argument("--min-p", type=float, default=0.01, help="Min-p sampling")
    p.add_argument("--chat", action="store_true", help="Use chat/completions instead")
    p.add_argument("--raw", action="store_true", help="Output raw text only (first result, no formatting)")
    p.add_argument("--json", action="store_true", help="Output results as JSON array")
    args = p.parse_args()

    # Resolve prompt: arg > file > stdin
    prompt = args.prompt
    if prompt is None and args.file:
        prompt = Path(args.file).read_text(encoding='utf-8')
    if prompt is None and not sys.stdin.isatty():
        prompt = sys.stdin.read()
    if prompt is None:
        print("No prompt provided. Pass as argument, --file, or pipe via stdin.", file=sys.stderr)
        sys.exit(1)

    # Unescape literal \n in command line args to actual newlines
    prompt = prompt.replace('\\n', '\n')

    async def main():
        if args.chat:
            results = await chat(prompt, args.n, args.tokens, args.temp)
        else:
            results = await complete(prompt, args.n, args.tokens, args.temp, args.min_p)

        if args.raw:
            # Just print first non-error result, no formatting
            for r in results:
                if not isinstance(r, Exception):
                    print(prompt + r, end="")
                    return
            print("ALL CALLS FAILED", file=sys.stderr)
            sys.exit(1)

        if args.json:
            out = []
            for r in results:
                out.append(str(r) if isinstance(r, Exception) else r)
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return

        # Default: labeled output
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                print(f"[{i}] ERROR: {r}")
            else:
                display = r.replace('\n', '\\n')[:300]
                print(f"[{i}] {display}")

    asyncio.run(main())
