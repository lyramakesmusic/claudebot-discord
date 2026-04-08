"""
Suno music generation module for claudebot.

Extracted from bot.py - handles Suno API authentication (via Clerk),
music generation, polling, and download.
"""

import os
import re
import json
import asyncio
import base64
import time
import logging
from pathlib import Path
from datetime import datetime

import aiohttp
import discord

log = logging.getLogger("claudebot")

# â"€â"€ Config â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

SUNO_COOKIE = os.getenv("SUNO_COOKIE", "")
SUNO_MODEL = "chirp-crow"  # fallback default
SUNO_CUSTOM_MODELS: dict[str, str] = {}
SUNO_DEFAULT_MODEL = "chirp-crow"  # updated by _fetch_models
SUNO_MODEL_DESCRIPTIONS: dict[str, str] = {}  # name -> description for sysprompt
PROJECT_ROOT = Path(__file__).resolve().parent.parent
GENERATED_MUSIC_DIR = PROJECT_ROOT / "data" / "generated_music"

# â"€â"€ Auth â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

class _SunoAuth:
    """Manages Suno authentication via Clerk token refresh."""

    def __init__(self, cookie_str: str):
        self._raw_cookie = cookie_str
        self._session_id: str | None = None
        self._token: str | None = None
        self._lock = asyncio.Lock()

    def _parse_cookies(self) -> dict[str, str]:
        """Parse cookie string into dict."""
        cookies = {}
        for pair in self._raw_cookie.split(";"):
            pair = pair.strip()
            if "=" in pair:
                k, v = pair.split("=", 1)
                cookies[k.strip()] = v.strip()
        return cookies

    def _get_device_id(self) -> str:
        """Extract device-id (ajs_anonymous_id) from cookie."""
        cookies = self._parse_cookies()
        return cookies.get("ajs_anonymous_id", "")

    async def _get_session_id(self, session: aiohttp.ClientSession) -> str:
        """Fetch session_id from Clerk client endpoint."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
            "Cookie": self._raw_cookie,
        }
        async with session.get(
            "https://auth.suno.com/v1/client?__clerk_api_version=2025-11-10&_clerk_js_version=5.117.0",
            headers=headers,
        ) as resp:
            data = await resp.json()
            response = data.get("response", data)
            sid = response.get("last_active_session_id")
            if not sid:
                sessions = response.get("sessions", [])
                if sessions:
                    sid = sessions[0].get("id")
            if not sid:
                raise ValueError(f"No session_id in Clerk response: {json.dumps(data)[:300]}")
            return sid

    def _update_cookies(self, resp_headers):
        """Merge Set-Cookie headers into our cookie string without duplicates."""
        cookies = self._parse_cookies()
        for cookie_header in resp_headers.getall("Set-Cookie", []):
            name_val = cookie_header.split(";")[0]
            if "=" in name_val:
                k, v = name_val.split("=", 1)
                cookies[k.strip()] = v.strip()
        self._raw_cookie = "; ".join(f"{k}={v}" for k, v in cookies.items())

    def reset_session(self):
        """Force re-fetch of session_id on next get_token call."""
        self._session_id = None
        self._token = None
        log.info("Suno session reset - will re-fetch on next call")

    async def get_token(self) -> str:
        """Get a fresh JWT token, refreshing if needed."""
        async with self._lock:
            async with aiohttp.ClientSession() as session:
                if not self._session_id:
                    self._session_id = await self._get_session_id(session)
                    log.info(f"Suno session_id: {self._session_id}")

                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
                    "Cookie": self._raw_cookie,
                }
                # retry token fetch - Clerk 429s with text/html sometimes
                for token_attempt in range(3):
                    async with session.post(
                        f"https://auth.suno.com/v1/client/sessions/{self._session_id}/tokens?__clerk_api_version=2025-11-10&_clerk_js_version=5.117.0",
                        headers=headers,
                    ) as resp:
                        if resp.status == 429:
                            wait = 10 * (token_attempt + 1)
                            log.warning(f"Clerk token 429, waiting {wait}s (attempt {token_attempt + 1}/3)")
                            await asyncio.sleep(wait)
                            continue
                        if resp.status != 200:
                            body = await resp.text()
                            raise ValueError(f"Clerk token error {resp.status}: {body[:200]}")
                        data = await resp.json()
                        jwt = data.get("jwt")
                        if not jwt:
                            raise ValueError(f"No JWT in token response: {json.dumps(data)[:300]}")
                        self._token = jwt
                        self._update_cookies(resp.headers)
                        return jwt
                raise ValueError("Clerk token endpoint rate limited after 3 retries")


# â"€â"€ Module state â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

_suno_auth: _SunoAuth | None = None
_suno_queue: asyncio.Queue | None = None
_suno_worker_task: asyncio.Task | None = None


def _get_suno_auth() -> _SunoAuth:
    global _suno_auth
    if _suno_auth is None:
        if not SUNO_COOKIE:
            raise ValueError("SUNO_COOKIE not set in .env")
        _suno_auth = _SunoAuth(SUNO_COOKIE)
    return _suno_auth


# â"€â"€ Worker â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

async def _suno_worker():
    """Process music generation jobs one at a time."""
    log.info("Suno worker started, waiting for jobs...")
    while True:
        channel, style, lyrics, title, model = await _suno_queue.get()
        log.info(f"Suno worker picked up job: style={style[:50]} title={title} model={model or 'default'}")
        try:
            filepaths, err = await generate_music(style, lyrics, title, model)
            if err:
                # keep error messages short
                short_err = err.split(":")[0] if len(err) > 120 else err
                await channel.send(f"Music generation failed: {short_err}")
            elif filepaths:
                files = [discord.File(fp, filename=Path(fp).name) for fp in filepaths]
                await channel.send(None, files=files)
                log.info(f"BG music delivered: {len(filepaths)} clips")
        except Exception:
            log.exception("Suno worker error")
            try:
                await channel.send("Music generation failed unexpectedly.")
            except Exception:
                pass
        finally:
            _suno_queue.task_done()


# â"€â"€ Model discovery â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

# Version name mapping for human-readable display
_VERSION_NAMES = {
    "chirp-crow": "v5", "chirp-fenix": "v5.5", "chirp-bluejay": "v4.5",
    "chirp-auk": "v4", "chirp-auk-turbo": "v4 turbo",
    "chirp-v4": "v4 (legacy)", "chirp-v3-5": "v3.5", "chirp-v3-0": "v3",
    "chirp-v2-xxl-alpha": "v2",
}


async def _fetch_models():
    """Fetch available models from Suno billing API and populate globals."""
    global SUNO_MODEL, SUNO_DEFAULT_MODEL, SUNO_CUSTOM_MODELS, SUNO_MODEL_DESCRIPTIONS
    try:
        auth = _get_suno_auth()
        token = await auth.get_token()
    except Exception as e:
        log.warning(f"[Suno] Can't fetch models (auth failed): {e}")
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0",
        "Accept": "*/*",
        "Referer": "https://suno.com/",
        "Origin": "https://suno.com",
        "Cookie": auth._raw_cookie,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://studio-api.prod.suno.com/api/billing/info/",
                headers=headers, timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    log.warning(f"[Suno] billing/info returned {resp.status}")
                    return
                data = await resp.json()
    except Exception as e:
        log.warning(f"[Suno] Failed to fetch billing info: {e}")
        return

    models = data.get("models", [])
    if not models:
        log.warning("[Suno] No models in billing response")
        return

    custom = {}
    default_key = None
    best_version = 0
    descriptions = {}

    for m in models:
        key = m.get("external_key", "")
        name = m.get("name", "")
        is_custom = "custom" in m.get("badges", [])
        is_default = m.get("is_default_model", False)
        major = m.get("major_version", 0)
        can_use = m.get("can_use", False)

        if not can_use or not key:
            continue

        if is_custom:
            custom[name] = key
            descriptions[name] = f"custom finetune"
        elif is_default:
            default_key = key
        elif major > best_version and not is_custom:
            best_version = major
            if not default_key:
                default_key = key

        # Track version name for non-custom
        if not is_custom and key in _VERSION_NAMES:
            descriptions[name] = _VERSION_NAMES[key]

    if custom:
        SUNO_CUSTOM_MODELS.update(custom)
    if default_key:
        SUNO_MODEL = default_key
        SUNO_DEFAULT_MODEL = default_key

    # Build description map for custom models
    SUNO_MODEL_DESCRIPTIONS = {n: f"custom finetune" for n in custom}

    default_label = _VERSION_NAMES.get(SUNO_MODEL, SUNO_MODEL)
    log.info(
        f"[Suno] Models loaded: default={default_label} ({SUNO_MODEL}), "
        f"custom={list(custom.keys()) or 'none'}"
    )


def get_suno_model_info() -> str:
    """Return a string describing available models for system prompts."""
    default_label = _VERSION_NAMES.get(SUNO_MODEL, SUNO_MODEL)
    if not SUNO_CUSTOM_MODELS:
        return f"Default model: {default_label}."
    parts = [f'"{name}"' for name in SUNO_CUSTOM_MODELS]
    return f"Default: {default_label}. Custom models: {', '.join(parts)}."


# â"€â"€ Public API â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

def init_suno_worker() -> asyncio.Task:
    """Initialize the suno queue, fetch models, and start the worker task."""
    global _suno_queue, _suno_worker_task
    if _suno_queue is None:
        _suno_queue = asyncio.Queue()
        _suno_worker_task = asyncio.create_task(_suno_worker())
        # Fire-and-forget model discovery
        asyncio.create_task(_fetch_models())
    return _suno_worker_task


def enqueue_music(
    channel: discord.abc.Messageable,
    style: str,
    lyrics: str,
    title: str,
    model: str = "",
):
    """Enqueue a music generation job. Processed one at a time by _suno_worker."""
    _suno_queue.put_nowait((channel, style, lyrics, title, model))


async def generate_music(
    style: str, lyrics: str = "", title: str = "", model: str = "",
) -> tuple[list[str], str | None]:
    """Generate music via Suno. Returns (list_of_filepaths, error)."""
    GENERATED_MUSIC_DIR.mkdir(parents=True, exist_ok=True)

    try:
        auth = _get_suno_auth()
        token = await auth.get_token()
    except Exception as e:
        return None, f"Suno auth failed: {e}"

    browser_token = base64.b64encode(
        json.dumps({"timestamp": int(time.time() * 1000)}).encode()
    ).decode()
    device_id = auth._get_device_id()

    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://suno.com/",
        "Origin": "https://suno.com",
        "Content-Type": "application/json",
        "Cookie": auth._raw_cookie,
        "browser-token": json.dumps({"token": browser_token}),
        "device-id": device_id,
        "sec-ch-ua": '"Chromium";v="144", "Google Chrome";v="144", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
    }

    # Resolve model name — check custom models, fall back to default
    resolved_model = SUNO_CUSTOM_MODELS.get(model.lower(), model) if model else SUNO_MODEL

    # custom mode: provide lyrics + style tags
    payload = {
        "prompt": lyrics or "",
        "tags": style,
        "mv": resolved_model,
        "title": title or "Untitled",
        "make_instrumental": not bool(lyrics),
    }

    try:
        async with aiohttp.ClientSession() as session:
            # submit generation - retry on 429 with backoff
            gen_data = None
            for gen_attempt in range(5):
                async with session.post(
                    "https://studio-api.prod.suno.com/api/generate/v2-web/",
                    headers=headers, json=payload,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 429:
                        body_429 = await resp.text()
                        log.warning(f"Suno 429 (attempt {gen_attempt + 1}/5) body={body_429[:500]}")
                        log.warning(f"Suno 429 resp headers: {dict(resp.headers)}")
                        wait = 60 * (gen_attempt + 1)
                        await asyncio.sleep(wait)
                        continue
                    if resp.status == 422:
                        body = await resp.text()
                        log.warning(f"Suno 422 (attempt {gen_attempt + 1}/5) body={body[:500]}")
                        if "token" in body.lower() or "validation" in body.lower():
                            # stale token - reset auth and retry quickly
                            auth.reset_session()
                            try:
                                token = await auth.get_token()
                                headers["Authorization"] = f"Bearer {token}"
                            except Exception as e:
                                log.warning(f"Auth refresh failed: {e}")
                            await asyncio.sleep(2)
                            continue
                        return None, f"Suno generate error 422: {body[:300]}"
                    if resp.status != 200:
                        body = await resp.text()
                        return None, f"Suno generate error {resp.status}: {body[:300]}"
                    gen_data = await resp.json()
                    break
            if gen_data is None:
                return None, "Suno rate limited (429) after retries - try again in a few minutes"

            # extract clip IDs to poll
            clips = gen_data.get("clips", [])
            if not clips:
                return None, f"No clips in response: {json.dumps(gen_data)[:300]}"
            clip_ids = [c["id"] for c in clips]
            log.info(f"Suno generation started: {len(clip_ids)} clips - {clip_ids}")

            # poll for ALL clips to complete (up to 5 minutes)
            completed_urls: dict[str, str] = {}  # clip_id -> audio_url
            failed_clips: dict[str, str] = {}  # clip_id -> error_message
            poll_ids = "%2C".join(clip_ids)
            for attempt in range(60):
                await asyncio.sleep(5)
                # refresh token every ~30s (every 6th poll), not every poll
                if attempt % 6 == 0:
                    try:
                        token = await auth.get_token()
                        headers["Authorization"] = f"Bearer {token}"
                    except Exception as e:
                        log.warning(f"Suno token refresh failed (attempt {attempt}): {e}")

                try:
                    async with session.get(
                        f"https://studio-api.prod.suno.com/api/feed/?ids={poll_ids}",
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            log.warning(f"Suno poll {attempt}: status={resp.status} body={body[:200]}")
                            continue
                        feed = await resp.json()
                except Exception as e:
                    log.warning(f"Suno poll {attempt} request failed: {e}")
                    continue

                for item in feed:
                    cid = item.get("id", "")
                    if cid in clip_ids and cid not in completed_urls and cid not in failed_clips:
                        status = item.get("status", "")
                        url = item.get("audio_url", "")
                        log.info(f"Suno poll {attempt}: clip={cid[:8]} status={status} has_url={bool(url)}")
                        if status == "error":
                            err_msg = item.get("metadata", {}).get("error_message", "unknown error")
                            log.warning(f"Suno clip {cid} failed: {err_msg}")
                            failed_clips[cid] = err_msg
                        elif status == "complete" and url:
                            completed_urls[cid] = url

                # break when all clips are resolved (complete or error)
                if len(completed_urls) + len(failed_clips) >= len(clip_ids):
                    break

            if not completed_urls:
                if failed_clips:
                    first_err = next(iter(failed_clips.values()))
                    return [], f"Suno generation failed: {first_err}"
                return [], "Suno generation timed out (5 min) - no clips completed"

            # download all completed clips
            filepaths = []
            for i, (cid, audio_url) in enumerate(completed_urls.items()):
                try:
                    async with session.get(audio_url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                        if resp.status != 200:
                            log.warning(f"Failed to download clip {cid}: status={resp.status}")
                            continue
                        audio_data = await resp.read()

                    suffix = f"_{i+1}" if len(completed_urls) > 1 else ""
                    safe_title = re.sub(r"[^\w\-]", "_", title or "suno")[:40]
                    filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_title}{suffix}.mp3"
                    filepath = GENERATED_MUSIC_DIR / filename
                    filepath.write_bytes(audio_data)
                    filepaths.append(str(filepath))
                    log.info(f"Music downloaded: {filepath} ({len(audio_data)} bytes)")
                except Exception as e:
                    log.warning(f"Failed to download clip {cid}: {e}")

            if not filepaths:
                return [], "All clip downloads failed"
            return filepaths, None

    except Exception as e:
        return None, f"Suno request failed: {e}"

