"""Bot action extraction shared regex and parser."""

import json
import logging
import re

log = logging.getLogger(__name__)

BOT_ACTION_RE = re.compile(
    r"(?:```bot_action\s*\n(.*?)\n```|<bot_action>\s*(.*?)\s*</bot_action>)",
    re.DOTALL,
)


def extract_bot_actions(text: str) -> tuple[str, list[dict]]:
    """Extract bot_action blocks from response text."""
    actions = []
    for m in BOT_ACTION_RE.finditer(text):
        raw = m.group(1) or m.group(2)
        try:
            actions.append(json.loads(raw))
        except json.JSONDecodeError:
            log.warning(f"Bad bot_action JSON: {raw[:100]}")
    cleaned = BOT_ACTION_RE.sub("", text).strip()
    return cleaned, actions
