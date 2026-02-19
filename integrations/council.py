#!/usr/bin/env python3
"""
council.py — Multi-model council for deep research and understanding.

Architecture:
  Opus (Claude Code)  = understander/orchestrator, talks to the user
  GPT-5.2             = critic, challenges Opus's understanding
  Gemini 3 Flash      = deep researcher with web access

Opus drives everything. It can call GPT and Gemini via bot_actions,
and the bot posts their responses back into the thread.
"""

import os
import json
import logging
import aiohttp
from typing import Optional

log = logging.getLogger("council")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

GPT_MODEL = "openai/gpt-5.2"
RESEARCHER_MODEL = "google/gemini-3-flash-preview:online"


# ── System Prompts ────────────────────────────────────────────────────────────

GPT_SYSTEM_PROMPT = """\
You are the Critic in a research council. Your job is to pressure-test ideas.

You're working with an Understander (Claude Opus) who has been talking to the user \
and believes they have a solid grasp of the user's vision. Your role is to make sure \
that understanding is actually solid — find the gaps, the assumptions, the parts that \
sound good but fall apart under scrutiny.

What makes you valuable:
- You're precise. You notice when something is vaguely defined or hand-wavy.
- You find edge cases and failure modes that others gloss over.
- You distinguish between "this is hard" and "this is actually impossible" — and you're \
honest about which is which.
- You ask pointed questions that force clarity.

What to keep in mind:
- The Understander genuinely cares about the user's vision. Don't be dismissive — be \
rigorous. There's a difference between "this won't work" and "this won't work *because*, \
and here's what would need to change."
- If something is genuinely clever or well-thought-out, say so. Criticism isn't about \
tearing things down, it's about making sure what stands is strong.
- Your goal isn't to kill ideas. It's to make them bulletproof.
- Be direct and concise. Say what you mean, don't hedge.

When you identify a real problem, be specific: what exactly is wrong, why it matters, \
and what information or changes would resolve it. Don't just wave at vague concerns.\
"""

RESEARCHER_SYSTEM_PROMPT = """\
You are a Deep Researcher in a research council. Your job is to find everything \
relevant to the topic you're given.

You have web search access. Use it aggressively. Your goal is to be thorough — not \
just the first few results, but deep coverage across multiple angles.

How to research well:
- Start broad, then go specific. Get the landscape first, then drill into details.
- Try MANY different search terms. Rephrase, use synonyms, try adjacent concepts. \
If "neural network pruning" doesn't give enough, try "model compression techniques", \
"sparse networks", "lottery ticket hypothesis", etc.
- Look for: academic papers, blog posts, GitHub repos, forum discussions, documentation, \
Wikipedia articles, industry reports. Different source types give different perspectives.
- When you find something promising, follow the thread — what does it cite? What cites it? \
What are the related concepts?
- Prioritize recent sources but don't ignore foundational older work.
- Note disagreements between sources. If two papers say opposite things, that's important.
- Track your confidence: "this is well-established" vs "this is one person's blog post."

Structure your findings clearly:
- Lead with the most important/relevant discoveries
- Group related findings together
- Include source URLs for everything
- Flag anything surprising or counterintuitive
- Note gaps — what you looked for but couldn't find

You may be called multiple times on the same topic with increasingly specific queries. \
Build on what you've already found rather than starting over. If you're asked to go deeper \
on something, really go deep — exhaust what's available.\
"""


# ── API Call Functions ────────────────────────────────────────────────────────

async def call_gpt(
    messages: list[dict],
    max_tokens: int = 16384,
) -> dict:
    """Call GPT-5.2 via OpenRouter with max reasoning effort.

    Args:
        messages: Chat messages (system prompt is prepended automatically if
                  the first message isn't a system message).
        max_tokens: Max output tokens.

    Returns:
        {"content": str, "reasoning": str|None, "cost": float, "error": str|None}
    """
    # prepend system prompt if not already there
    if not messages or messages[0].get("role") != "system":
        messages = [{"role": "system", "content": GPT_SYSTEM_PROMPT}] + messages

    payload = {
        "model": GPT_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "reasoning": {"effort": "xhigh"},
        "include_reasoning": True,
    }

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                OPENROUTER_URL,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return {"content": "", "reasoning": None, "cost": 0.0,
                            "error": f"API error {resp.status}: {body[:500]}"}
                data = await resp.json()
    except Exception as e:
        return {"content": "", "reasoning": None, "cost": 0.0,
                "error": f"Request failed: {e}"}

    choice = data.get("choices", [{}])[0]
    msg = choice.get("message", {})
    cost = float(data.get("usage", {}).get("cost", 0) or 0)

    return {
        "content": msg.get("content", ""),
        "reasoning": msg.get("reasoning", None),
        "cost": cost,
        "error": None,
    }


async def call_researcher(
    query: str,
    context: str = "",
    max_tokens: int = 16384,
) -> dict:
    """Call Gemini 3 Flash with web search for deep research.

    Args:
        query: The research query/topic.
        context: Optional context about what's already been found or what to focus on.
        max_tokens: Max output tokens.

    Returns:
        {"content": str, "cost": float, "error": str|None}
    """
    user_msg = query
    if context:
        user_msg = f"{query}\n\nContext from the council so far:\n{context}"

    payload = {
        "model": RESEARCHER_MODEL,
        "messages": [
            {"role": "system", "content": RESEARCHER_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": max_tokens,
        "plugins": [{
            "id": "web",
            "max_results": 20,
            "search_prompt": (
                "Search thoroughly. Try multiple phrasings and angles. "
                "Look for academic sources, technical docs, implementations, "
                "and community discussions."
            ),
        }],
        "web_search_options": {"search_context_size": "high"},
    }

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                OPENROUTER_URL,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return {"content": "", "cost": 0.0,
                            "error": f"API error {resp.status}: {body[:500]}"}
                data = await resp.json()
    except Exception as e:
        return {"content": "", "cost": 0.0, "error": f"Request failed: {e}"}

    choice = data.get("choices", [{}])[0]
    msg = choice.get("message", {})
    cost = float(data.get("usage", {}).get("cost", 0) or 0)

    # extract citations if present
    content = msg.get("content", "")
    annotations = msg.get("annotations", [])
    if annotations:
        sources = []
        for ann in annotations:
            if ann.get("type") == "url_citation":
                cite = ann.get("url_citation", {})
                url = cite.get("url", "")
                title = cite.get("title", url)
                if url:
                    sources.append(f"- [{title}]({url})")
        if sources:
            content += "\n\n**Sources found:**\n" + "\n".join(sources)

    return {
        "content": content,
        "cost": cost,
        "error": None,
    }
