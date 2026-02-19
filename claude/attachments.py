"""Attachment collection and cleanup for Claude bot messages."""

import os
import re
from pathlib import Path

import discord


async def collect_message_attachments(
    message: discord.Message, att_dir: Path, logger
) -> list[tuple[str, str]]:
    """Download attachments and optionally extract PDF text."""
    att_dir.mkdir(parents=True, exist_ok=True)
    att_paths: list[tuple[str, str]] = []

    for att in message.attachments:
        try:
            data = await att.read()
            safe_name = re.sub(r"[^\w.\-]", "_", att.filename or "file")
            att_path = att_dir / f"{att.id}_{safe_name}"
            att_path.write_bytes(data)

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
    """Best-effort cleanup for downloaded temporary attachment files."""
    for _, path in att_paths:
        try:
            os.unlink(path)
        except OSError:
            pass
        if path.endswith(".txt"):
            pdf_path = path.rsplit(".", 1)[0] + ".pdf"
            try:
                os.unlink(pdf_path)
            except OSError:
                pass
