"""Usage tracking: per-turn token stats, context window %, and plan utilization."""

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

CONTEXT_WINDOW = 190_000  # effective limit (Claude Code reserves ~10k for compaction)
CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"


# ── Context window percentage ───────────────────────────────────────────────

def context_percent(total_tokens: int) -> int | None:
    """Return context usage as integer percentage, or None if no data."""
    if total_tokens <= 0:
        return None
    return round((total_tokens / CONTEXT_WINDOW) * 100)


# ── Plan usage (5h/7d windows from Anthropic OAuth API) ─────────────────────

async def fetch_plan_usage() -> dict | None:
    """Fetch plan utilization from Anthropic API.

    Returns dict with keys: five_hour, seven_day, seven_day_opus, seven_day_sonnet
    Each value is {"utilization": int, "resets_at": str} or None.
    Returns None on failure.
    """
    try:
        creds = json.loads(CREDENTIALS_PATH.read_text("utf-8"))
        token = creds.get("claudeAiOauth", {}).get("accessToken")
        if not token:
            return None
    except Exception:
        return None

    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.anthropic.com/api/oauth/usage",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token}",
                    "anthropic-beta": "oauth-2025-04-20",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
    except Exception:
        log.debug("Failed to fetch plan usage", exc_info=True)
        return None

    def _parse(key: str):
        v = data.get(key)
        if v and v.get("utilization") is not None and v.get("resets_at"):
            return {"utilization": v["utilization"], "resets_at": v["resets_at"]}
        return None

    return {
        "five_hour": _parse("five_hour"),
        "seven_day": _parse("seven_day"),
        "seven_day_opus": _parse("seven_day_opus"),
        "seven_day_sonnet": _parse("seven_day_sonnet"),
    }


def format_reset_time(resets_at: str) -> str:
    """Format a resets_at ISO timestamp as a human-readable relative time."""
    from datetime import datetime, timezone
    try:
        reset_dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        ms = int((reset_dt - now).total_seconds() * 1000)
        if ms <= 0:
            return "now"
        h = ms // 3600000
        m = (ms % 3600000) // 60000
        return f"{h}h {m}m" if h > 0 else f"{m}m"
    except Exception:
        return "?"
