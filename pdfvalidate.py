"""
PDF validation — runs in the WEB process before a job is enqueued, so bad
uploads are rejected in <2s without ever reaching the worker.

All checks are cheap (magic bytes, header, pypdf page count / encryption /
embedded-file scan). No text extraction happens here.
"""
from __future__ import annotations

import io

MAX_PDF_BYTES  = 2 * 1024 * 1024   # 2 MB hard cap
MAX_PDF_PAGES  = 3                  # CV must be <= 3 pages


class PdfRejected(Exception):
    """Raised when an uploaded PDF fails a safety/validation check.
    The message is safe to show the user."""


def _has_embedded_files(reader) -> bool:
    """True if the PDF carries embedded files / attachments (a malware /
    exfiltration vector — we only ever want plain text)."""
    try:
        root = reader.trailer["/Root"]
        names = root.get("/Names")
        if names and names.get("/EmbeddedFiles"):
            return True
    except Exception:
        pass
    return False


def validate_pdf(raw: bytes) -> int:
    """Reject non-PDFs, oversized, encrypted, over-length or booby-trapped
    files. Returns the page count on success. Raises PdfRejected otherwise."""
    # 1) Genuine PDF by magic bytes — not extension or Content-Type.
    if raw[:5] != b"%PDF-":
        raise PdfRejected("That doesn't look like a valid PDF file.")

    # 2) Hard size cap.
    if len(raw) > MAX_PDF_BYTES:
        raise PdfRejected("File too large — please upload a CV under 2 MB.")

    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception:
        raise PdfRejected("This file could not be processed.")

    # 3) No encrypted / password-protected PDFs.
    if getattr(reader, "is_encrypted", False):
        raise PdfRejected("Password-protected PDFs aren't supported — please upload an unlocked CV.")

    # 4) Page count — lazy, before any text extraction.
    try:
        num_pages = len(reader.pages)
    except Exception:
        raise PdfRejected("This file could not be processed.")
    if num_pages == 0:
        raise PdfRejected("This file could not be processed.")
    if num_pages > MAX_PDF_PAGES:
        raise PdfRejected(f"Please upload a CV of maximum {MAX_PDF_PAGES} pages.")

    # 5) No embedded files / attachments.
    if _has_embedded_files(reader):
        raise PdfRejected("This file could not be processed.")

    return num_pages
