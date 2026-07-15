"""
extraction/document_loader.py — File-to-text conversion for invoice documents.

Converts raw file bytes to a plain UTF-8 string that the extraction agent
can process.  All format detection, decoding, and extraction errors are raised
as DocumentLoadError so callers get a single typed exception regardless of
the underlying failure mode.

Supported formats
-----------------
- text/plain  (.txt)  — decoded as UTF-8; UnicodeDecodeError → DocumentLoadError.
- application/pdf (.pdf) — text extracted with pdfplumber.

  Library choice: pdfplumber over pypdf.
  pdfplumber uses pdfminer.six under the hood and applies layout analysis to
  reconstruct reading order from the PDF's character position data.  This is
  critical for invoice documents, which are typically multi-column or
  table-heavy: pypdf's page.extract_text() returns raw character streams
  concatenated in PDF object order, which produces garbled output for those
  layouts.  pdfplumber's page.extract_text() reconstructs lines correctly
  with no extra configuration required.

  Limitation: text extraction only works on PDFs with an embedded text layer.
  Scanned / image-only PDFs produce empty or near-empty output; these are
  rejected with a clear error rather than silently passed to the LLM as an
  empty string.

- All other MIME types / extensions → DocumentLoadError naming the type.

Public API
----------
DocumentLoadError  — raised on any unrecoverable load / decode / extract error.
load_document_text(raw_bytes, filename, content_type) -> str
"""

from __future__ import annotations

import io
from pathlib import PurePosixPath

import pdfplumber

__all__ = ["DocumentLoadError", "load_document_text"]

# Minimum character count required for extracted PDF text to be considered
# usable.  PDFs below this threshold are almost certainly scanned / image-only
# files with no text layer; sending them to the LLM would produce hallucinated
# extractions rather than a clean failure.
_MIN_PDF_TEXT_LENGTH: int = 20


class DocumentLoadError(Exception):
    """
    Raised when a document cannot be loaded or its text cannot be extracted.

    Attributes:
        message: Human-readable description of the failure.

    Never wraps a raw library exception directly — callers receive this typed
    error regardless of whether the root cause is a UnicodeDecodeError, a
    corrupt PDF, or an unsupported MIME type.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def load_document_text(
    raw_bytes: bytes,
    filename: str,
    content_type: str | None = None,
) -> str:
    """
    Convert raw file bytes to extractable plain text.

    Detection order (content_type takes precedence over filename extension):
      1. If content_type is "text/plain"        → UTF-8 decode path.
      2. If content_type is "application/pdf"   → pdfplumber extraction path.
      3. If content_type is None or unrecognised, fall back to filename
         extension: ".txt" → UTF-8, ".pdf" → pdfplumber.
      4. Anything else → DocumentLoadError(unsupported type).

    Args:
        raw_bytes:    Raw file content as bytes.
        filename:     Original filename (used for extension-based fallback and
                      error messages).  May be empty — extension fallback is
                      skipped if no extension is present.
        content_type: MIME type string (e.g. "text/plain", "application/pdf").
                      Strip parameters before passing (e.g. strip "; charset=utf-8").
                      May be None if the caller does not know the type.

    Returns:
        Non-empty string of extracted text ready for the extraction agent.

    Raises:
        DocumentLoadError: on decode failure, corrupt/empty PDF, or unsupported type.
    """
    # Normalise: strip MIME parameters (e.g. "text/plain; charset=utf-8")
    # and lower-case for comparison.
    normalised_ct = (content_type or "").split(";")[0].strip().lower()
    ext = PurePosixPath(filename).suffix.lower()  # e.g. ".pdf", ".txt", ""

    # Determine format: content_type first, then extension fallback.
    if normalised_ct == "text/plain" or (not normalised_ct and ext == ".txt"):
        return _load_text(raw_bytes, filename)

    if normalised_ct == "application/pdf" or (not normalised_ct and ext == ".pdf"):
        return _load_pdf(raw_bytes, filename)

    # Build a descriptive label for the error.
    type_label = normalised_ct or ext or "unknown"
    raise DocumentLoadError(
        f"Unsupported document type {type_label!r} for file {filename!r}.  "
        "Only text/plain and application/pdf are supported."
    )


# ---------------------------------------------------------------------------
# Format-specific loaders
# ---------------------------------------------------------------------------


def _load_text(raw_bytes: bytes, filename: str) -> str:
    """
    Decode bytes as UTF-8 text.

    Raises:
        DocumentLoadError: if the bytes are not valid UTF-8.
    """
    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DocumentLoadError(
            f"File {filename!r} could not be decoded as UTF-8: {exc.reason} "
            f"at byte offset {exc.start}."
        ) from exc


def _load_pdf(raw_bytes: bytes, filename: str) -> str:
    """
    Extract text from all pages of a PDF using pdfplumber.

    Pages are joined with double newlines so paragraph boundaries are
    preserved for the extraction agent.

    Raises:
        DocumentLoadError: if the bytes are not a valid PDF, if pdfplumber
            cannot open or parse the file, or if the extracted text is empty
            or shorter than _MIN_PDF_TEXT_LENGTH characters (scanned / image-
            only PDF with no text layer).
    """
    try:
        with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
            page_texts: list[str] = []
            for page in pdf.pages:
                text = page.extract_text() or ""
                stripped = text.strip()
                if stripped:
                    page_texts.append(stripped)
    except DocumentLoadError:
        raise  # re-raise our own errors unchanged
    except Exception as exc:
        # pdfplumber raises a variety of exceptions for corrupt / truncated
        # files (pdfminer.PDFSyntaxError, struct.error, etc.).  Normalise them
        # all to DocumentLoadError so the caller has one thing to catch.
        raise DocumentLoadError(
            f"Could not parse PDF {filename!r}: {type(exc).__name__}: {exc}"
        ) from exc

    full_text = "\n\n".join(page_texts)

    if len(full_text) < _MIN_PDF_TEXT_LENGTH:
        raise DocumentLoadError(
            f"PDF {filename!r} contains no extractable text — "
            "scanned/image PDFs are not supported.  "
            "Re-submit a PDF with an embedded text layer."
        )

    return full_text
