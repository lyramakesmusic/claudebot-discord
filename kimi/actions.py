"""Bot action dispatcher for kimi bot."""

from pathlib import Path
import discord

from shared.hotreload import validate_syntax

state = None
log = None
_BOT_FILE = None
DOCUMENTS_DIR = None


def configure(state_obj, logger, bot_file: Path, documents_dir: Path):
    global state, log, _BOT_FILE, DOCUMENTS_DIR
    state = state_obj
    log = logger
    _BOT_FILE = bot_file
    DOCUMENTS_DIR = documents_dir

async def execute_bot_actions(
    actions: list[dict],
    message: discord.Message,
    channel: discord.abc.Messageable,
    guild_id: int = 0,
) -> tuple[list[str], bool]:
    """Execute bot actions. Returns (status_messages, should_reload)."""
    guild_config = state.get_guild_config(guild_id)
    guild_docs = Path(guild_config["docs_dir"]) if guild_config else DOCUMENTS_DIR

    results = []
    should_reload = False
    for act in actions:
        action = act.get("action")

        if action == "upload":
            file_path = act.get("path", "").strip()
            caption = act.get("caption", "").strip()
            if not file_path:
                results.append("(upload skipped - no path given)")
                continue
            p = Path(file_path).expanduser()
            if not p.exists():
                results.append(f"Upload failed - file not found: `{file_path}`")
                continue
            size_mb = p.stat().st_size / (1024 * 1024)
            if size_mb > 500:
                results.append(f"Upload failed - file too large ({size_mb:.1f} MB, max 500 MB)")
                continue
            try:
                f = discord.File(str(p), filename=p.name)
                await channel.send(caption or None, file=f)
                log.info(f"Uploaded {p.name} ({size_mb:.1f} MB)")
            except Exception as e:
                results.append(f"Upload failed: {e}")

        elif action == "reload":
            ok, err = validate_syntax(_BOT_FILE)
            if not ok:
                results.append(f"Reload aborted - bad syntax:\n```\n{(err or 'unknown')[:500]}\n```")
            else:
                should_reload = True

        else:
            log.warning(f"Unknown bot_action: {action}")

    return results, should_reload
