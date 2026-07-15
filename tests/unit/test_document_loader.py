"""
tests/unit/test_document_loader.py — unit tests for extraction/document_loader.py.

All PDF fixtures are constructed as raw byte literals so no binary files need
to be checked into the repository and no PDF-generation library is required as
a test dependency.  The fixtures were validated interactively against pdfplumber
0.11.10 to confirm extraction behaviour.

Fixture catalogue
-----------------
_TEXT_LAYER_PDF_BYTES
    Minimal valid single-page PDF (PDF 1.4, Type1/Helvetica) with a text
    content stream containing "Invoice text here".  pdfplumber extracts this
    cleanly via layout analysis.

_IMAGE_ONLY_PDF_BYTES
    Valid PDF structure with an empty content stream — no text operators, as
    would be the case for a scanned/rasterised page.  pdfplumber returns an
    empty string for extract_text(); load_document_text() must raise
    DocumentLoadError.

_CORRUPT_PDF_BYTES
    Truncated / invalid bytes prefixed with %PDF-1.4 but otherwise not a valid
    PDF.  pdfplumber raises pdfminer.pdfpage.PdfminerException; we expect
    DocumentLoadError.

_PLAIN_TEXT_BYTES
    Simple UTF-8 encoded invoice text, no PDF involved.

Cover matrix
------------
TestTextPassthrough      — .txt / text/plain → returns decoded string as-is.
TestTextDecodeError      — non-UTF-8 bytes via text/plain → DocumentLoadError.
TestPdfTextLayer         — real text-layer PDF → extracted text returned.
TestPdfImageOnly         — image-only (empty text) PDF → DocumentLoadError.
TestPdfCorrupt           — corrupt/truncated PDF bytes → DocumentLoadError.
TestPdfMultiPage         — two-page PDF → pages joined with double newline.
TestTypeDetection        — content_type precedence over extension, unsupported types.
"""

from __future__ import annotations

import pytest

from extraction.document_loader import DocumentLoadError, load_document_text


# ---------------------------------------------------------------------------
# PDF byte fixtures
# ---------------------------------------------------------------------------

# Minimal single-page PDF with "Invoice No. INV-2024-001 Total: 2500.00" in a
# Type1 text stream (>20 chars, clears the _MIN_PDF_TEXT_LENGTH threshold).
# Validated: pdfplumber.open(BytesIO(...)).pages[0].extract_text()
# → 'Invoice No. INV-2024-001 Total: 2500.00'
_TEXT_LAYER_PDF_BYTES: bytes = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R"
    b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
    b"4 0 obj<</Length 62>>\n"
    b"stream\n"
    b"BT /F1 12 Tf 100 700 Td (Invoice No. INV-2024-001 Total: 2500.00) Tj ET\n"
    b"endstream\n"
    b"endobj\n"
    b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"xref\n"
    b"0 6\n"
    b"0000000000 65535 f\r\n"
    b"0000000009 00000 n\r\n"
    b"0000000058 00000 n\r\n"
    b"0000000115 00000 n\r\n"
    b"0000000266 00000 n\r\n"
    b"0000000378 00000 n\r\n"
    b"trailer<</Size 6/Root 1 0 R>>\n"
    b"startxref\n"
    b"459\n"
    b"%%EOF"
)

# Two-page PDF: page 1 has "Page one content", page 2 has "Page two content".
# Both use the same font reference.  Used to verify double-newline joining.
_TWO_PAGE_PDF_BYTES: bytes = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R 6 0 R]/Count 2>>endobj\n"
    b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R"
    b"/Contents 4 0 R/Resources<</Font<</F1 8 0 R>>>>>>endobj\n"
    b"4 0 obj<</Length 44>>\n"
    b"stream\n"
    b"BT /F1 12 Tf 100 700 Td (Page one content) Tj ET\n"
    b"endstream\n"
    b"endobj\n"
    b"6 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R"
    b"/Contents 7 0 R/Resources<</Font<</F1 8 0 R>>>>>>endobj\n"
    b"7 0 obj<</Length 44>>\n"
    b"stream\n"
    b"BT /F1 12 Tf 100 700 Td (Page two content) Tj ET\n"
    b"endstream\n"
    b"endobj\n"
    b"8 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"xref\n"
    b"0 9\n"
    b"0000000000 65535 f\r\n"
    b"0000000009 00000 n\r\n"
    b"0000000058 00000 n\r\n"
    b"0000000115 00000 n\r\n"
    b"0000000277 00000 n\r\n"
    b"0000000000 65535 f\r\n"
    b"0000000371 00000 n\r\n"
    b"0000000536 00000 n\r\n"
    b"0000000630 00000 n\r\n"
    b"trailer<</Size 9/Root 1 0 R>>\n"
    b"startxref\n"
    b"711\n"
    b"%%EOF"
)

# Valid PDF structure, empty content stream — simulates scanned/image-only PDF.
# Validated: pdfplumber extracts '' for extract_text() on this page.
_IMAGE_ONLY_PDF_BYTES: bytes = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R"
    b"/Contents 4 0 R/Resources<<>>>>>>endobj\n"
    b"4 0 obj<</Length 0>>\n"
    b"stream\n"
    b"endstream\n"
    b"endobj\n"
    b"xref\n"
    b"0 5\n"
    b"0000000000 65535 f\r\n"
    b"0000000009 00000 n\r\n"
    b"0000000058 00000 n\r\n"
    b"0000000115 00000 n\r\n"
    b"0000000246 00000 n\r\n"
    b"trailer<</Size 5/Root 1 0 R>>\n"
    b"startxref\n"
    b"296\n"
    b"%%EOF"
)

# Corrupt / truncated bytes that start with %PDF-1.4 but are not a valid PDF.
# Validated: pdfplumber raises pdfminer.pdfpage.PdfminerException.
_CORRUPT_PDF_BYTES: bytes = b"%PDF-1.4 this is not a real PDF\x00\x01\x02\xff"

# Plain UTF-8 invoice text.
_PLAIN_TEXT_BYTES: bytes = (
    b"INVOICE\n"
    b"Vendor: Acme Corp\n"
    b"PO: PO-2024-001\n"
    b"Total: 2500.00\n"
)

# Bytes that are not valid UTF-8.
_INVALID_UTF8_BYTES: bytes = b"Valid start \xff\xfe invalid continuation"


# ---------------------------------------------------------------------------
# TestTextPassthrough
# ---------------------------------------------------------------------------


class TestTextPassthrough:
    """Plain text files are decoded as UTF-8 and returned unchanged."""

    def test_text_plain_content_type(self) -> None:
        result = load_document_text(_PLAIN_TEXT_BYTES, "invoice.txt", "text/plain")
        assert "Acme Corp" in result
        assert "PO-2024-001" in result

    def test_txt_extension_fallback(self) -> None:
        """No content_type supplied → extension .txt triggers text path."""
        result = load_document_text(_PLAIN_TEXT_BYTES, "invoice.txt", None)
        assert "Total: 2500.00" in result

    def test_text_plain_with_mime_params(self) -> None:
        """content_type with charset parameter is stripped and still recognised."""
        result = load_document_text(
            _PLAIN_TEXT_BYTES, "invoice.txt", "text/plain; charset=utf-8"
        )
        assert "INVOICE" in result

    def test_returns_exact_decoded_string(self) -> None:
        payload = "Hello café £ €".encode("utf-8")
        result = load_document_text(payload, "note.txt", "text/plain")
        assert result == "Hello café £ €"


# ---------------------------------------------------------------------------
# TestTextDecodeError
# ---------------------------------------------------------------------------


class TestTextDecodeError:
    """Non-UTF-8 bytes via text/plain raise DocumentLoadError."""

    def test_invalid_utf8_raises_document_load_error(self) -> None:
        with pytest.raises(DocumentLoadError) as exc_info:
            load_document_text(_INVALID_UTF8_BYTES, "bad.txt", "text/plain")
        assert "UTF-8" in exc_info.value.message

    def test_error_message_names_the_file(self) -> None:
        with pytest.raises(DocumentLoadError) as exc_info:
            load_document_text(_INVALID_UTF8_BYTES, "mystery_file.txt", "text/plain")
        assert "mystery_file.txt" in exc_info.value.message

    def test_not_a_raw_unicode_decode_error(self) -> None:
        """The exception must be DocumentLoadError, not UnicodeDecodeError."""
        with pytest.raises(DocumentLoadError):
            load_document_text(_INVALID_UTF8_BYTES, "bad.txt", "text/plain")


# ---------------------------------------------------------------------------
# TestPdfTextLayer
# ---------------------------------------------------------------------------


class TestPdfTextLayer:
    """A PDF with an embedded text layer extracts cleanly."""

    def test_application_pdf_content_type(self) -> None:
        result = load_document_text(
            _TEXT_LAYER_PDF_BYTES, "invoice.pdf", "application/pdf"
        )
        assert "INV-2024-001" in result

    def test_pdf_extension_fallback(self) -> None:
        """No content_type → .pdf extension triggers PDF path."""
        result = load_document_text(_TEXT_LAYER_PDF_BYTES, "invoice.pdf", None)
        assert "2500.00" in result

    def test_returns_string_not_bytes(self) -> None:
        result = load_document_text(
            _TEXT_LAYER_PDF_BYTES, "invoice.pdf", "application/pdf"
        )
        assert isinstance(result, str)

    def test_result_meets_minimum_length(self) -> None:
        result = load_document_text(
            _TEXT_LAYER_PDF_BYTES, "invoice.pdf", "application/pdf"
        )
        assert len(result) >= 20


# ---------------------------------------------------------------------------
# TestPdfMultiPage
# ---------------------------------------------------------------------------


class TestPdfMultiPage:
    """Multi-page PDFs have pages joined with a double newline."""

    def test_both_pages_present(self) -> None:
        result = load_document_text(
            _TWO_PAGE_PDF_BYTES, "multi.pdf", "application/pdf"
        )
        assert "Page one content" in result
        assert "Page two content" in result

    def test_pages_separated_by_double_newline(self) -> None:
        result = load_document_text(
            _TWO_PAGE_PDF_BYTES, "multi.pdf", "application/pdf"
        )
        assert "\n\n" in result


# ---------------------------------------------------------------------------
# TestPdfImageOnly
# ---------------------------------------------------------------------------


class TestPdfImageOnly:
    """A PDF with no text layer raises DocumentLoadError."""

    def test_empty_text_raises_document_load_error(self) -> None:
        with pytest.raises(DocumentLoadError) as exc_info:
            load_document_text(_IMAGE_ONLY_PDF_BYTES, "scan.pdf", "application/pdf")
        msg = exc_info.value.message
        assert "no extractable text" in msg.lower() or "scanned" in msg.lower()

    def test_error_message_mentions_image_pdf(self) -> None:
        with pytest.raises(DocumentLoadError) as exc_info:
            load_document_text(_IMAGE_ONLY_PDF_BYTES, "scan.pdf", "application/pdf")
        assert "scanned" in exc_info.value.message.lower() or \
               "image" in exc_info.value.message.lower()

    def test_error_message_names_the_file(self) -> None:
        with pytest.raises(DocumentLoadError) as exc_info:
            load_document_text(_IMAGE_ONLY_PDF_BYTES, "my_scan.pdf", "application/pdf")
        assert "my_scan.pdf" in exc_info.value.message


# ---------------------------------------------------------------------------
# TestPdfCorrupt
# ---------------------------------------------------------------------------


class TestPdfCorrupt:
    """Corrupt or truncated PDF bytes raise DocumentLoadError."""

    def test_corrupt_pdf_raises_document_load_error(self) -> None:
        with pytest.raises(DocumentLoadError):
            load_document_text(_CORRUPT_PDF_BYTES, "bad.pdf", "application/pdf")

    def test_not_a_raw_library_exception(self) -> None:
        """The caller must never see a pdfminer or struct exception directly."""
        with pytest.raises(DocumentLoadError):
            load_document_text(_CORRUPT_PDF_BYTES, "bad.pdf", "application/pdf")

    def test_error_message_names_the_file(self) -> None:
        with pytest.raises(DocumentLoadError) as exc_info:
            load_document_text(_CORRUPT_PDF_BYTES, "corrupt_invoice.pdf", "application/pdf")
        assert "corrupt_invoice.pdf" in exc_info.value.message

    def test_completely_random_bytes_raise(self) -> None:
        with pytest.raises(DocumentLoadError):
            load_document_text(b"\x00\x01\x02\x03" * 50, "noise.pdf", "application/pdf")


# ---------------------------------------------------------------------------
# TestTypeDetection
# ---------------------------------------------------------------------------


class TestTypeDetection:
    """content_type takes precedence over extension; unsupported types are rejected."""

    def test_content_type_overrides_extension(self) -> None:
        """text/plain content_type on a .pdf filename → text path, not PDF path."""
        result = load_document_text(_PLAIN_TEXT_BYTES, "invoice.pdf", "text/plain")
        assert "Acme Corp" in result

    def test_pdf_content_type_overrides_txt_extension(self) -> None:
        """application/pdf on a .txt filename → PDF extraction path."""
        result = load_document_text(
            _TEXT_LAYER_PDF_BYTES, "invoice.txt", "application/pdf"
        )
        assert "INV-2024-001" in result

    def test_unsupported_content_type_raises(self) -> None:
        with pytest.raises(DocumentLoadError) as exc_info:
            load_document_text(b"data", "report.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        assert "Unsupported" in exc_info.value.message

    def test_unsupported_extension_no_content_type_raises(self) -> None:
        with pytest.raises(DocumentLoadError) as exc_info:
            load_document_text(b"<html>hi</html>", "invoice.html", None)
        assert "Unsupported" in exc_info.value.message

    def test_unknown_extension_no_content_type_raises(self) -> None:
        with pytest.raises(DocumentLoadError):
            load_document_text(b"some bytes", "file.xyz", None)

    def test_error_names_unsupported_type(self) -> None:
        with pytest.raises(DocumentLoadError) as exc_info:
            load_document_text(b"data", "report.csv", "text/csv")
        assert "text/csv" in exc_info.value.message

    def test_document_load_error_is_exception_subclass(self) -> None:
        """DocumentLoadError must inherit from Exception for bare except compatibility."""
        with pytest.raises(Exception):
            load_document_text(b"data", "x.xyz", None)
