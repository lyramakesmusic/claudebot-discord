"""
Midjourney image generation via Discord slash commands.

Uses the selfbot token to send /imagine to the MJ bot in a dedicated channel,
polls for the response, downloads the grid image, and splits into 4 quadrants.
"""

import os
import re
import random
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from io import BytesIO

import aiohttp
import discord
from PIL import Image

log = logging.getLogger("claudebot")

SELFBOT_TOKEN = os.getenv("SELFBOT_TOKEN", "")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
GENERATED_IMAGES_DIR = PROJECT_ROOT / "data" / "generated_images"

# Midjourney bot / slash command constants
MJ_APP_ID = "936929561302675456"
MJ_BOT_ID = 936929561302675456
MJ_IMAGINE_CMD_ID = "938956540159881230"
MJ_IMAGINE_CMD_VERSION = "1237876415471554623"

# Channel where we send /imagine (dedicated MJ channel)
MJ_CHANNEL_ID = "1485465916866170982"
# Guild
MJ_GUILD_ID = "1061615370068303902"


# ── Slash command submission ───────────────────────────────────────────────


async def _send_imagine(prompt: str) -> bool:
    """Send /imagine slash command via selfbot. Returns True on success."""
    headers = {
        "Authorization": SELFBOT_TOKEN,
        "Content-Type": "application/json",
    }
    payload = {
        "type": 2,  # APPLICATION_COMMAND
        "application_id": MJ_APP_ID,
        "guild_id": MJ_GUILD_ID,
        "channel_id": MJ_CHANNEL_ID,
        "session_id": "%032x" % random.getrandbits(128),
        "data": {
            "version": MJ_IMAGINE_CMD_VERSION,
            "id": MJ_IMAGINE_CMD_ID,
            "name": "imagine",
            "type": 1,
            "options": [
                {"type": 3, "name": "prompt", "value": prompt},
            ],
        },
        "nonce": str(random.randint(10**17, 10**18)),
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://discord.com/api/v9/interactions",
            headers=headers,
            json=payload,
        ) as resp:
            if resp.status == 204:
                log.info(f"[MJ] /imagine sent: {prompt[:60]}")
                return True
            body = await resp.text()
            log.warning(f"[MJ] /imagine failed: {resp.status} {body[:200]}")
            return False


# ── Poll for MJ bot response ──────────────────────────────────────────────


async def _snapshot_existing_ids() -> set[str]:
    """Get IDs of messages already in the MJ channel before submitting."""
    headers = {"Authorization": SELFBOT_TOKEN}
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://discord.com/api/v9/channels/{MJ_CHANNEL_ID}/messages?limit=10"
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    return {m["id"] for m in await resp.json()}
    except Exception:
        pass
    return set()


async def _poll_for_result(prompt: str, known_ids: set[str], timeout: int = 300) -> dict | None:
    """Poll the MJ channel for the bot's finished response.

    Only matches messages with IDs not in known_ids (avoids picking up old results).
    Returns the message dict with attachments and components, or None on timeout.
    """
    headers = {"Authorization": SELFBOT_TOKEN}
    prompt_start = prompt.split("--")[0].strip().lower()[:40]

    start = asyncio.get_event_loop().time()
    async with aiohttp.ClientSession() as session:
        while (asyncio.get_event_loop().time() - start) < timeout:
            await asyncio.sleep(3)
            try:
                url = f"https://discord.com/api/v9/channels/{MJ_CHANNEL_ID}/messages?limit=5"
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        continue
                    msgs = await resp.json()
            except Exception:
                continue

            for msg in msgs:
                # Skip messages that existed before we submitted
                if msg["id"] in known_ids:
                    continue
                if int(msg["author"]["id"]) != MJ_BOT_ID:
                    continue
                if not msg.get("attachments"):
                    continue
                components = msg.get("components", [])
                if not components:
                    continue
                has_u1 = any(
                    c.get("custom_id", "").startswith("MJ::JOB::upsample::1")
                    for row in components
                    for c in row.get("components", [])
                )
                if not has_u1:
                    continue
                content = msg.get("content", "").lower()
                if prompt_start and prompt_start in content:
                    elapsed = asyncio.get_event_loop().time() - start
                    log.info(f"[MJ] Result found in {elapsed:.0f}s: msg {msg['id']}")
                    return msg

            elapsed = asyncio.get_event_loop().time() - start
            if int(elapsed) % 30 < 6:
                log.info(f"[MJ] Polling... {elapsed:.0f}s elapsed")

    log.warning("[MJ] Timed out waiting for MJ response")
    return None


# ── Image download & split ─────────────────────────────────────────────────


async def _download_and_split(attachment_url: str, job_id: str) -> tuple[list[str], str | None]:
    """Download the grid image, save it, and split into 4 quadrants.

    Returns (quadrant_paths, grid_path).
    """
    GENERATED_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    async with aiohttp.ClientSession() as session:
        async with session.get(attachment_url) as resp:
            if resp.status != 200:
                log.warning(f"[MJ] Grid download failed: {resp.status}")
                return [], None
            data = await resp.read()

    img = Image.open(BytesIO(data))
    w, h = img.size
    hw, hh = w // 2, h // 2

    # Save the full grid so Claude can view it
    grid_path = GENERATED_IMAGES_DIR / f"{timestamp}_mj_{job_id[:8]}_grid.png"
    grid_path.write_bytes(data)

    paths = []
    for i, (x, y) in enumerate([(0, 0), (hw, 0), (0, hh), (hw, hh)]):
        quad = img.crop((x, y, x + hw, y + hh))
        filename = f"{timestamp}_mj_{job_id[:8]}_{i + 1}.png"
        filepath = GENERATED_IMAGES_DIR / filename
        quad.save(filepath)
        paths.append(str(filepath))
        log.info(f"[MJ] Quad {i + 1}: {quad.size}, {filepath.stat().st_size // 1024}KB")

    return paths, str(grid_path)


# ── Main generate function ─────────────────────────────────────────────────


async def generate_image(prompt: str) -> tuple[list[str], str | None, str | None, str]:
    """Submit prompt to MJ and return (file_paths, error_or_none, grid_path, mj_prompt).

    mj_prompt is the actual prompt MJ used (with resolved --sref random etc).
    """
    if not SELFBOT_TOKEN:
        return [], "SELFBOT_TOKEN not set", None, prompt

    # Snapshot existing messages so we don't match old results
    known_ids = await _snapshot_existing_ids()

    ok = await _send_imagine(prompt)
    if not ok:
        return [], "Failed to send /imagine command", None, prompt

    msg = await _poll_for_result(prompt, known_ids)
    if not msg:
        return [], "Timed out waiting for Midjourney (5 min)", None, prompt

    # Extract the actual prompt MJ used (between ** markers in content)
    mj_prompt = prompt
    mj_content = msg.get("content", "")
    m = re.match(r"\*\*(.+?)\*\*", mj_content)
    if m:
        mj_prompt = m.group(1)

    # Extract job ID from U1 button custom_id
    job_id = "unknown"
    for row in msg.get("components", []):
        for c in row.get("components", []):
            cid = c.get("custom_id", "")
            if "upsample::1::" in cid:
                job_id = cid.split("::")[-1]
                break

    att = msg["attachments"][0]
    paths, grid_path = await _download_and_split(att["url"], job_id)
    if not paths:
        return [], "Failed to download grid image", None, mj_prompt

    return paths, None, grid_path, mj_prompt


# ── Worker queue ───────────────────────────────────────────────────────────

_mj_queue: asyncio.Queue | None = None
_mj_worker_task: asyncio.Task | None = None


async def _mj_worker():
    """Process Midjourney generation jobs one at a time."""
    log.info("[MJ] Worker started, waiting for jobs...")
    while True:
        channel, prompt, caption, ctx_key, bridge, trigger_msg = await _mj_queue.get()
        log.info(f"[MJ] Worker picked up job: {prompt[:60]}")
        typing_task = None
        try:
            # Keep typing indicator going while generating
            async def _keep_typing():
                try:
                    while True:
                        await channel.typing()
                        await asyncio.sleep(8)
                except asyncio.CancelledError:
                    pass
            typing_task = asyncio.create_task(_keep_typing())

            filepaths, err, grid_path, mj_prompt = await generate_image(prompt)
            if err:
                log.warning(f"[MJ] Generation error: {err}")
                await channel.send(f"Midjourney error: {err}")
            elif filepaths:
                files = [discord.File(p, filename=Path(p).name) for p in filepaths]
                await channel.send(caption or None, files=files)
                log.info(f"[MJ] Delivered {len(filepaths)} images")
                # Notify Claude Code so it can see the results
                if grid_path and ctx_key and bridge:
                    pp = bridge.get_process(ctx_key)
                    if pp and pp._alive:
                        notify = (
                            f"[Midjourney finished — MJ prompt was: {mj_prompt}\n"
                            f"Grid image at {grid_path}. "
                            f"Read it to see what was generated and comment on it.]"
                        )
                        try:
                            result = await pp.send(notify)
                            # Send Claude's reaction to the channel
                            text = result.get("text", "").strip()
                            if text:
                                while text:
                                    await channel.send(text[:2000])
                                    text = text[2000:]
                            # Send usage footer
                            from shared.usage import context_percent
                            total_tokens = result.get("total_tokens", 0)
                            ctx_pct = context_percent(total_tokens)
                            if ctx_pct is not None:
                                cache_read = result.get("cache_read_tokens", 0)
                                cache_hit = round((cache_read / total_tokens) * 100) if total_tokens else 0
                                footer = f"-# ctx {ctx_pct}% | {total_tokens:,} tokens ({cache_hit}% cached)"
                                if ctx_pct >= 80:
                                    footer += "\n-# \u26a0 will autocompact soon!"
                                await channel.send(footer)
                            log.info(f"[MJ] Notified Claude Code session {ctx_key}")
                        except Exception as e:
                            log.warning(f"[MJ] Failed to notify Claude: {e}")
        except Exception as e:
            log.exception("[MJ] Worker error")
            try:
                await channel.send(f"Midjourney error: {e}")
            except Exception:
                pass
        finally:
            if typing_task:
                typing_task.cancel()
            _mj_queue.task_done()


# ── Public API ─────────────────────────────────────────────────────────────


def init_mj_worker() -> asyncio.Task | None:
    """Initialize the Midjourney queue and start the worker task."""
    global _mj_queue, _mj_worker_task

    if not SELFBOT_TOKEN:
        log.warning("[MJ] SELFBOT_TOKEN not set, Midjourney disabled")
        return None

    if _mj_queue is None:
        _mj_queue = asyncio.Queue()
        _mj_worker_task = asyncio.create_task(_mj_worker())
    return _mj_worker_task


def enqueue_midjourney(
    channel: discord.abc.Messageable,
    prompt: str,
    caption: str = "",
    ctx_key: str = None,
    bridge=None,
    message=None,
):
    """Enqueue a Midjourney generation job."""
    if _mj_queue is None:
        raise RuntimeError("MJ worker not initialized")
    _mj_queue.put_nowait((channel, prompt, caption, ctx_key, bridge, message))


def shutdown_mj_browser():
    """No-op — kept for plugin compatibility."""
    pass
