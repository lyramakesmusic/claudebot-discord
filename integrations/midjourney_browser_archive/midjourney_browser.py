"""
Midjourney image generation via headless-ish Playwright browser.

Uses a persistent headed Chromium (window hidden via win32) to interact with
midjourney.com. Submits prompts through the web UI, monitors for job completion,
and downloads finished images through the browser's own fetch (bypasses CF).
"""

import os
import re
import json
import asyncio
import logging
import time
import base64
import ctypes
import threading
from pathlib import Path
from datetime import datetime
from ctypes import wintypes

import discord

log = logging.getLogger("claudebot")

MIDJOURNEY_COOKIE = os.getenv("MIDJOURNEY_COOKIE", "")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
GENERATED_IMAGES_DIR = PROJECT_ROOT / "data" / "generated_images"

# ── Win32 helpers ───────────────────────────────────────────────────────────

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_SW_HIDE = 0
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_playwright_pid: int | None = None  # set after browser launch


def _hide_playwright_windows():
    """Hide Playwright browser windows by PID tree lookup."""
    if not _playwright_pid:
        return

    import subprocess, time

    # Build PID set using PowerShell (more reliable than wmic)
    try:
        result = subprocess.run(
            ["powershell", "-Command",
             f"Get-CimInstance Win32_Process | Where-Object {{ $_.ProcessId -eq {_playwright_pid} -or $_.ParentProcessId -eq {_playwright_pid} }} | Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=5,
        )
        pids = {int(line.strip()) for line in result.stdout.strip().split("\n") if line.strip().isdigit()}
        # Get grandchildren too
        for pid in list(pids):
            r2 = subprocess.run(
                ["powershell", "-Command",
                 f"Get-CimInstance Win32_Process | Where-Object {{ $_.ParentProcessId -eq {pid} }} | Select-Object -ExpandProperty ProcessId"],
                capture_output=True, text=True, timeout=5,
            )
            pids |= {int(l.strip()) for l in r2.stdout.strip().split("\n") if l.strip().isdigit()}
    except Exception:
        pids = {_playwright_pid}

    _GWL_EXSTYLE = -20
    _WS_EX_TOOLWINDOW = 0x00000080
    _WS_EX_APPWINDOW = 0x00040000

    hidden = 0
    def callback(hwnd, _):
        nonlocal hidden
        window_pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if window_pid.value in pids:
            ex = _user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
            _user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, (ex & ~_WS_EX_APPWINDOW) | _WS_EX_TOOLWINDOW)
            _user32.ShowWindow(hwnd, _SW_HIDE)
            hidden += 1
        return True

    # Retry — Chromium windows appear asynchronously after launch
    for attempt in range(8):
        hidden = 0
        _user32.EnumWindows(_WNDENUMPROC(callback), 0)
        if hidden > 0:
            log.info(f"[MJ] Hidden {hidden} windows (attempt {attempt + 1})")
        time.sleep(1)


# ── Cookie helpers ──────────────────────────────────────────────────────────


def _parse_cookies() -> list[dict]:
    """Parse MIDJOURNEY_COOKIE env var into Playwright cookie dicts."""
    cookies = []
    for pair in MIDJOURNEY_COOKIE.split(";"):
        pair = pair.strip()
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        k, v = k.strip(), v.strip()
        if not k:
            continue
        cookie = {"name": k, "value": v, "path": "/", "secure": True}
        cookie["domain"] = (
            "www.midjourney.com" if k.startswith("__Host-") else ".midjourney.com"
        )
        cookies.append(cookie)
    return cookies


# ── Browser session ─────────────────────────────────────────────────────────

_browser = None
_browser_context = None
_browser_page = None
_browser_lock = asyncio.Lock()
_pw = None
_MJ_PID_FILE = PROJECT_ROOT / "data" / "mj_browser.pid"


def _kill_orphan_browser():
    """Kill any leftover Playwright browser from a previous bot run."""
    if not _MJ_PID_FILE.exists():
        return
    try:
        old_pid = int(_MJ_PID_FILE.read_text().strip())
        import subprocess
        # Kill the process tree (browser + helpers)
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(old_pid)],
            capture_output=True, timeout=10,
        )
        log.info(f"[MJ] Killed orphan browser (PID {old_pid})")
    except Exception:
        pass
    finally:
        try:
            _MJ_PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass


def _save_browser_pid(pid: int):
    """Persist the browser PID so we can clean up after reload/restart."""
    try:
        _MJ_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MJ_PID_FILE.write_text(str(pid))
    except Exception:
        pass


async def _ensure_browser():
    """Launch browser if not running, navigate to MJ imagine page."""
    global _browser, _browser_context, _browser_page, _pw

    if _browser_page:
        # Check if still alive
        try:
            await _browser_page.title()
            return _browser_page
        except Exception:
            log.warning("[MJ] Browser page dead, relaunching...")
            _browser_page = None

    # Kill any orphan browser from a previous run
    _kill_orphan_browser()

    from playwright.async_api import async_playwright

    if not _pw:
        _pw = await async_playwright().start()

    global _playwright_pid
    _browser = await _pw.chromium.launch(
        headless=False,
        args=["--window-position=-3000,-3000", "--window-size=1,1"],
    )

    # Track the browser PID so we only hide ITS windows (not the user's Chrome!)
    try:
        _playwright_pid = _browser._impl_obj._browser_process.pid
        log.info(f"[MJ] Browser PID: {_playwright_pid}")
        _save_browser_pid(_playwright_pid)
    except Exception:
        _playwright_pid = None

    # Hide windows from taskbar (run in thread to avoid blocking)
    await asyncio.sleep(1)
    await asyncio.get_event_loop().run_in_executor(None, _hide_playwright_windows)

    _browser_context = await _browser.new_context()

    # Inject auth cookies
    parsed = _parse_cookies()
    if parsed:
        await _browser_context.add_cookies(parsed)

    _browser_page = await _browser_context.new_page()

    log.info("[MJ] Navigating to midjourney.com/imagine...")
    await _browser_page.goto("https://www.midjourney.com/imagine", timeout=60_000)

    # Wait for CF to resolve
    for _ in range(15):
        await asyncio.sleep(2)
        title = await _browser_page.title()
        if "Just a moment" not in title:
            log.info(f"[MJ] Page loaded: {title}")
            break
    else:
        log.warning("[MJ] CF challenge did not resolve")

    await asyncio.sleep(3)
    # Hide any new windows that appeared
    await asyncio.get_event_loop().run_in_executor(None, _hide_playwright_windows)

    return _browser_page


# ── Image generation ────────────────────────────────────────────────────────


async def generate_image(prompt: str) -> tuple[list[str], str | None]:
    """Submit a prompt to MJ and download the resulting images."""
    GENERATED_IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    async with _browser_lock:
        try:
            page = await _ensure_browser()
        except Exception as e:
            return [], f"Browser launch failed: {e}"

        try:
            return await _submit_and_download(page, prompt)
        except Exception as e:
            log.exception("[MJ] Generation failed")
            return [], f"MJ error: {e}"


async def _submit_and_download(page, prompt: str) -> tuple[list[str], str | None]:
    """Type prompt, submit, wait for completion, download images."""

    try:
        # Find prompt input
        input_sel = 'textarea[placeholder*="imagine"]'
        try:
            await page.wait_for_selector(input_sel, timeout=10_000)
        except Exception:
            log.info("[MJ] Reloading page...")
            await page.goto("https://www.midjourney.com/imagine", timeout=60_000)
            await asyncio.sleep(5)
            await page.wait_for_selector(input_sel, timeout=15_000)

        # Snapshot existing CDN image job IDs before submitting
        existing_jobs = set()
        for img in await page.query_selector_all("img[src*='cdn.midjourney.com']"):
            src = await img.get_attribute("src") or ""
            m = re.search(r"cdn\.midjourney\.com/([^/]+)/", src)
            if m:
                existing_jobs.add(m.group(1))

        input_el = await page.query_selector(input_sel)
        if not input_el:
            return [], "Could not find prompt input"

        await input_el.click()
        await asyncio.sleep(0.3)
        await input_el.fill(prompt)
        await asyncio.sleep(0.5)
        await input_el.press("Enter")
        log.info(f"[MJ] Prompt submitted: {prompt[:60]}")

        # Wait for new images to appear that weren't there before
        log.info("[MJ] Waiting for generation to complete...")
        for wait_round in range(60):  # up to 5 minutes
            await asyncio.sleep(5)

            # Search broadly for CDN images (img src, picture source, background)
            new_jobs = await page.evaluate("""(existing) => {
                const jobs = {};
                // Check all img elements
                for (const img of document.querySelectorAll('img')) {
                    const src = img.src || img.dataset?.src || '';
                    const m = src.match(/cdn\\.midjourney\\.com\\/([0-9a-f-]{36})\\//);
                    if (m && !existing.includes(m[1])) {
                        if (!jobs[m[1]]) jobs[m[1]] = [];
                        jobs[m[1]].push(src);
                    }
                }
                // Check picture > source elements
                for (const source of document.querySelectorAll('picture source')) {
                    const srcset = source.srcset || '';
                    const m = srcset.match(/cdn\\.midjourney\\.com\\/([0-9a-f-]{36})\\//);
                    if (m && !existing.includes(m[1])) {
                        if (!jobs[m[1]]) jobs[m[1]] = [];
                        const url = srcset.split(' ')[0] || srcset;
                        if (!jobs[m[1]].includes(url)) jobs[m[1]].push(url);
                    }
                }
                return jobs;
            }""", list(existing_jobs))

            for jid, srcs in new_jobs.items():
                if len(srcs) >= 4:
                    log.info(f"[MJ] Generation complete: job {jid}, {len(srcs)} images")
                    return await _download_images(page, srcs[:4], jid)

            if (wait_round + 1) % 6 == 0:
                total_new = sum(len(v) for v in new_jobs.items()) if new_jobs else 0
                log.info(f"[MJ] Still waiting... {(wait_round + 1) * 5}s, new jobs: {list(new_jobs.keys())[:3]}, imgs: {total_new}")

        return [], "Generation timed out (5 min)"

    finally:
        pass


async def _download_images(
    page, image_urls: list[str], job_id: str
) -> tuple[list[str], str | None]:
    """Download images via the browser's fetch (bypasses CF on CDN)."""
    filepaths = []
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Get higher-res versions by modifying the URL
    # Pattern: 0_0_640_N.webp -> 0_0_1024_N.webp
    hi_res_urls = []
    for url in image_urls:
        hi_res = re.sub(r"_640_N\.webp", "_1024_N.webp", url)
        hi_res_urls.append(hi_res)

    for i, url in enumerate(hi_res_urls[:4]):
        try:
            data_url = await page.evaluate(
                """async (url) => {
                try {
                    const resp = await fetch(url);
                    if (!resp.ok) {
                        // Fall back to original URL
                        const resp2 = await fetch(url.replace('_1024_N', '_640_N'));
                        if (!resp2.ok) return null;
                        const blob2 = await resp2.blob();
                        const r2 = new FileReader();
                        return new Promise(resolve => {
                            r2.onload = () => resolve(r2.result);
                            r2.readAsDataURL(blob2);
                        });
                    }
                    const blob = await resp.blob();
                    const reader = new FileReader();
                    return new Promise(resolve => {
                        reader.onload = () => resolve(reader.result);
                        reader.readAsDataURL(blob);
                    });
                } catch(e) { return null; }
            }""",
                url,
            )
            if data_url:
                b64_data = data_url.split(",", 1)[1] if "," in data_url else data_url
                image_bytes = base64.b64decode(b64_data)
                filename = f"{timestamp}_mj_{job_id[:8]}_{i}.webp"
                filepath = GENERATED_IMAGES_DIR / filename
                filepath.write_bytes(image_bytes)
                filepaths.append(str(filepath))
                log.info(f"[MJ] Saved image {i}: {len(image_bytes)} bytes")
        except Exception as e:
            log.warning(f"[MJ] Failed to download image {i}: {e}")

    if not filepaths:
        return [], "All image downloads failed"
    return filepaths, None


# ── Worker ──────────────────────────────────────────────────────────────────

_mj_queue: asyncio.Queue | None = None
_mj_worker_task: asyncio.Task | None = None


async def _mj_worker():
    """Process Midjourney generation jobs one at a time."""
    log.info("[MJ] Worker started, waiting for jobs...")
    while True:
        channel, prompt, caption = await _mj_queue.get()
        log.info(f"[MJ] Worker picked up job: {prompt[:60]}")
        try:
            filepaths, err = await generate_image(prompt)
            if err:
                await channel.send(f"Midjourney generation failed: {err[:200]}")
            elif filepaths:
                files = [
                    discord.File(fp, filename=Path(fp).name) for fp in filepaths
                ]
                await channel.send(caption or None, files=files)
                log.info(f"[MJ] Delivered {len(filepaths)} images")
        except Exception:
            log.exception("[MJ] Worker error")
            try:
                await channel.send("Midjourney generation failed unexpectedly.")
            except Exception:
                pass
        finally:
            _mj_queue.task_done()


# ── Public API ──────────────────────────────────────────────────────────────


def init_mj_worker() -> asyncio.Task | None:
    """Initialize the Midjourney queue and start the worker task."""
    global _mj_queue, _mj_worker_task

    if not MIDJOURNEY_COOKIE:
        log.warning("[MJ] MIDJOURNEY_COOKIE not set, Midjourney disabled")
        return None

    if _mj_queue is None:
        _mj_queue = asyncio.Queue()
        _mj_worker_task = asyncio.create_task(_mj_worker())
    return _mj_worker_task


def enqueue_midjourney(
    channel: discord.abc.Messageable,
    prompt: str,
    caption: str = "",
):
    """Enqueue a Midjourney generation job."""
    if _mj_queue is None:
        raise RuntimeError("MJ worker not initialized")
    _mj_queue.put_nowait((channel, prompt, caption))


def shutdown_mj_browser():
    """Kill the Playwright browser. Call on bot shutdown/reload."""
    global _browser, _browser_page, _browser_context, _pw, _playwright_pid
    if _playwright_pid:
        try:
            import subprocess
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(_playwright_pid)],
                capture_output=True, timeout=10,
            )
            log.info(f"[MJ] Killed browser (PID {_playwright_pid})")
        except Exception:
            pass
        _playwright_pid = None
    _browser = None
    _browser_page = None
    _browser_context = None
    _pw = None
    _MJ_PID_FILE.unlink(missing_ok=True)


import atexit
atexit.register(lambda: _MJ_PID_FILE.exists() and _kill_orphan_browser())
