"""System prompt builders for kimi bot."""

from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "data" / "prompts"


def _load_custom_prompt(filename: str) -> str:
    path = _PROMPTS_DIR / filename
    try:
        text = path.read_text("utf-8").strip()
        return text if text else ""
    except Exception:
        return ""


def build_system_context(
    claude_bot_user_id: str,
    codex_bot_user_id: str,
    channel_name: str = "kimi",
    server_name: str = "",
    docs_dir: str = "~/Documents",
) -> str:
    server_note = f" in {server_name}" if server_name else ""
    custom = _load_custom_prompt("kimi_system.md")
    custom_section = f"\n{custom}" if custom else ""
    return (
        f"You are kimi_bot, a coding assistant on Discord (#{channel_name}{server_note}). "
        f"cwd: {docs_dir}. "
        f"Siblings: <@{claude_bot_user_id}> (Claude - orchestrator), <@{codex_bot_user_id}> (Codex). "
        "You just code. Respond directly - never summarize context. Max 2000 chars. "
        'Upload files: ```bot_action\n{"action":"upload","path":"/path"}\n```'
        f"{custom_section}"
    )


def build_thread_context() -> str:
    custom = _load_custom_prompt("kimi_system.md")
    custom_section = f"\n{custom}" if custom else ""
    return (
        "You are kimi_bot in a Discord thread. You just code. "
        "Respond directly - never summarize context. Max 2000 chars. "
        'Upload files: ```bot_action\n{"action":"upload","path":"/path"}\n```'
        f"{custom_section}"
    )
