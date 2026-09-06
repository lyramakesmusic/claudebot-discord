"""Attachment collection and cleanup for Claude bot messages."""

import io
import os
import re
import time
from pathlib import Path

import discord

# 24 hours in seconds
_MAX_AGE = 24 * 60 * 60
# max image file size in bytes before we resize (1MB)
_MAX_IMAGE_BYTES = 1 * 1024 * 1024
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"}


def _shrink_image(path: Path, logger) -> bool:
    """Resize an image file to fit under _MAX_IMAGE_BYTES. Returns True if resized."""
    if path.suffix.lower() not in _IMAGE_EXTS:
        return False
    if path.stat().st_size <= _MAX_IMAGE_BYTES:
        return False
    try:
        from PIL import Image
        img = Image.open(path)
        # convert RGBA to RGB for JPEG output
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        # iteratively scale down until under limit
        quality = 85
        scale = 0.8
        for _ in range(10):
            w, h = int(img.width * scale), int(img.height * scale)
            if w < 100 or h < 100:
                break
            resized = img.resize((w, h), Image.LANCZOS)
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=quality)
            if buf.tell() <= _MAX_IMAGE_BYTES:
                # write the resized image as .jpg
                out_path = path.with_suffix(".jpg")
                out_path.write_bytes(buf.getvalue())
                if out_path != path:
                    path.unlink()  # remove original
                logger.info(f"Resized image to {w}x{h} ({buf.tell() // 1024}KB)")
                return True
            scale *= 0.8
        logger.warning(f"Could not shrink {path.name} under {_MAX_IMAGE_BYTES // 1024}KB")
    except ImportError:
        logger.warning("Pillow not installed — skipping image resize")
    except Exception as e:
        logger.warning(f"Image resize failed for {path.name}: {e}")
    return False


async def collect_message_attachments(
    message: discord.Message, att_dir: Path, logger
) -> list[tuple[str, str]]:
    """Download attachments and optionally extract PDF text."""
    att_dir.mkdir(parents=True, exist_ok=True)
    # opportunistically prune old files each time we download new ones
    cleanup_old_attachments(att_dir)
    att_paths: list[tuple[str, str]] = []

    for att in message.attachments:
        try:
            data = await att.read()
            safe_name = re.sub(r"[^\w.\-]", "_", att.filename or "file")
            att_path = att_dir / f"{att.id}_{safe_name}"
            att_path.write_bytes(data)

            # shrink large images to save tokens
            if att_path.suffix.lower() in _IMAGE_EXTS and att_path.stat().st_size > _MAX_IMAGE_BYTES:
                if _shrink_image(att_path, logger):
                    att_path = att_path.with_suffix(".jpg")

            if att_path.suffix.lower() == ".pdf":
                try:
                    import pymupdf

                    doc = pymupdf.open(str(att_path))
                    pages = []
                    for i, page in enumerate(doc):
                        text = page.get_text()
                        if text.strip():
                            pages.append(f"--- Page {i + 1} ---\n{text}")
                    doc.close()
                    if pages:
                        txt_path = att_path.with_suffix(".txt")
                        txt_path.write_text("\n\n".join(pages), "utf-8")
                        att_paths.append((att.filename or safe_name, str(txt_path).replace("\\", "/")))
                        logger.info(f"PDF extracted: {att.filename} -> {len(pages)} pages")
                        continue
                except Exception as exc:
                    logger.warning(f"PDF extraction failed for {att.filename}: {exc}")

            att_paths.append((att.filename or safe_name, str(att_path).replace("\\", "/")))
        except Exception:
            logger.warning(f"Failed to download attachment {att.filename}")

    return att_paths


def cleanup_message_attachments(att_paths: list[tuple[str, str]]):
    """No-op: attachments are kept for 24h and cleaned up by cleanup_old_attachments."""
    pass


def cleanup_old_attachments(att_dir: Path):
    """Delete attachment files older than 24 hours."""
    if not att_dir.is_dir():
        return
    now = time.time()
    for f in att_dir.iterdir():
        if not f.is_file():
            continue
        try:
            age = now - f.stat().st_mtime
            if age > _MAX_AGE:
                f.unlink()
        except OSError:
            pass
