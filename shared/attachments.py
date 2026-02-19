"""Attachment download and cleanup utilities."""

import logging
from pathlib import Path

log = logging.getLogger(__name__)


async def download_attachments(
    attachments: list,
    att_dir: Path,
    extract_pdf_text: bool = True,
) -> list[dict]:
    """Download Discord attachments to a local directory."""
    att_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []

    for att in attachments:
        dest = att_dir / f"{getattr(att, 'id', 'file')}_{att.filename}"
        try:
            await att.save(dest)
        except Exception as exc:
            log.warning(f"Failed to download attachment {att.filename}: {exc}")
            continue

        info = {"path": str(dest).replace("\\", "/"), "filename": att.filename}

        if extract_pdf_text and att.filename.lower().endswith(".pdf"):
            try:
                import pymupdf

                doc = pymupdf.open(str(dest))
                text_pages = []
                for i, page in enumerate(doc):
                    page_text = page.get_text()
                    if page_text.strip():
                        text_pages.append(f"--- Page {i + 1} ---\\n{page_text}")
                doc.close()

                if text_pages:
                    txt_path = dest.with_suffix(".txt")
                    txt_path.write_text("\\n\\n".join(text_pages[:20]), "utf-8")
                    info["path"] = str(txt_path).replace("\\", "/")
                    info["description"] = f"[PDF: {att.filename}, {len(text_pages)} pages extracted]"
                else:
                    info["description"] = f"[PDF: {att.filename}]"
            except ImportError:
                info["description"] = f"[PDF: {att.filename}]"
            except Exception as exc:
                info["description"] = f"[PDF: {att.filename}, extraction failed: {exc}]"
        else:
            info["description"] = f"[Attached file: {att.filename}]"

        downloaded.append(info)

    return downloaded


def cleanup_attachments(att_paths: list[str]):
    """Delete downloaded attachment files. Best-effort."""
    for p in att_paths:
        try:
            path = Path(p)
            path.unlink(missing_ok=True)
            if path.suffix.lower() == ".txt":
                path.with_suffix(".pdf").unlink(missing_ok=True)
        except Exception:
            pass
