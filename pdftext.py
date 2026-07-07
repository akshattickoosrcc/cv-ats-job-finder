"""
PDF text extraction with a fast primary path and safe fallbacks.

Primary:   PyMuPDF (fitz)  — ~4x faster than pdfplumber, robust on multi-column
                             CVs. NOTE: PyMuPDF is AGPL-licensed; for a closed
                             commercial product either comply with AGPL or set
                             PDF_EXTRACTOR=pypdf to use the BSD-licensed path.
Fallback:  pdfplumber -> pypdf (both already dependencies).

Text output is capped at MAX_TEXT_BYTES (decompression-bomb guard). Extraction
is plain-text only — no JS execution, no external reference resolution.
"""
from __future__ import annotations

import io
import os

MAX_TEXT_BYTES = 500 * 1024        # abort extraction past 500 KB
MAX_PDF_PAGES  = 3


class ExtractionError(Exception):
    pass


def _cap(parts: list[str]) -> str:
    total = 0
    out = []
    for p in parts:
        total += len(p.encode("utf-8", "ignore"))
        out.append(p)
        if total > MAX_TEXT_BYTES:
            raise ExtractionError("text too large")
    return "\n".join(out).strip()


def _pymupdf(raw: bytes) -> str:
    import fitz  # PyMuPDF
    doc = fitz.open(stream=raw, filetype="pdf")
    try:
        parts = [doc[i].get_text() for i in range(min(len(doc), MAX_PDF_PAGES))]
    finally:
        doc.close()
    return _cap(parts)


def _pdfplumber(raw: bytes) -> str:
    import pdfplumber
    parts = []
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        for page in pdf.pages[:MAX_PDF_PAGES]:
            parts.append(page.extract_text() or "")
    return _cap(parts)


def _pypdf(raw: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(raw))
    parts = [(p.extract_text() or "") for p in reader.pages[:MAX_PDF_PAGES]]
    return _cap(parts)


def extract_text(raw: bytes) -> str:
    """Extract text using the configured primary extractor, falling back to the
    others if it errors. Raises ExtractionError only if ALL extractors fail."""
    primary = os.environ.get("PDF_EXTRACTOR", "pymupdf").lower()
    order = {
        "pymupdf":    [_pymupdf, _pdfplumber, _pypdf],
        "pdfplumber": [_pdfplumber, _pymupdf, _pypdf],
        "pypdf":      [_pypdf, _pdfplumber, _pymupdf],
    }.get(primary, [_pymupdf, _pdfplumber, _pypdf])

    last_err = None
    for fn in order:
        try:
            txt = fn(raw)
            if txt.strip():
                return txt
        except ExtractionError:
            raise
        except Exception as e:  # extractor not installed or choked — try next
            last_err = e
            continue
    raise ExtractionError(f"all extractors failed: {last_err}")
