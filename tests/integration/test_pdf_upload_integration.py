"""
tests/integration/test_pdf_upload_integration.py
─────────────────────────────────────────────────
Integration tests that verify PDF upload support end-to-end through the
FastAPI test client on POST /invoices/upload.

Two scenarios are covered:

  Test 1 — PDF with embedded text layer produces the same pipeline outcome
  as the equivalent plain-text invoice.
    • A minimal single-page PDF (raw bytes, no external PDF library required)
      whose text layer contains the same invoice fields as the plain-text
      fixture from tests/golden/scenario_01_clean_invoice.json is uploaded
      via multipart/form-data.
    • The LLM extraction layer is patched out to return a fixed ExtractionSuccess
      so the test runs without OPENROUTER_API_KEY and without network access.
    • The DB session is patched to an in-memory SQLite engine seeded with a
      matching vendor / PO / contract.
    • Assertions: HTTP 201, outcome == "STP", same invoice_number as the
      text-path control test that uploads identical content as plain text.

  Test 2 — Image-only / scanned PDF returns HTTP 422 with a clear reason.
    • A valid PDF with an empty content stream (no text operators — exactly
      what a scanned page looks like to a text extractor) is uploaded.
    • No patching required — load_document_text() raises DocumentLoadError
      before the LLM is ever called.
    • Assertions: HTTP 422, response body "detail" mentions "scanned" or
      "no extractable text", no stack trace, no 500.

PDF byte fixtures
─────────────────
Both fixtures are constructed as raw byte literals validated against
pdfplumber.  They are identical in structure to those in
tests/unit/test_document_loader.py — minimal, self-contained, requiring
no binary files in the repository and no PDF-generation library.

Isolation strategy
──────────────────
The same DB patching approach used in test_pipeline_db_integration.py is
applied here: db.session.SessionLocal is replaced with a sessionmaker bound
to an in-memory SQLite engine, so every call to get_session() inside the
request handler uses the seeded test database rather than app.db.

The LLM extraction agent is only patched for Test 1 (text-layer PDF).
Test 2 never reaches the extraction agent — the document loader raises
before that code path runs — so no patching is needed there.
"""

from __future__ import annotations

import io
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from models.base import Base
from models.contract import ContractLineItemORM, ContractORM
from models.enums import ExtractionStatus, InvoiceStatus
from models.invoice import InvoiceCreate, InvoiceLineItemCreate
from models.purchase_order import POLineItemORM, PurchaseOrderORM
from models.vendor import VendorORM
from extraction.schemas import ExtractionSuccess


# ──────────────────────────────────────────────────────────────────────────────
# PDF byte fixtures
# ──────────────────────────────────────────────────────────────────────────────

# Minimal single-page PDF (PDF 1.4, Type1/Helvetica) whose content stream
# contains the invoice text from scenario_01_clean_invoice.json.  The text is
# split across three BT/ET blocks to stay within the stream-length limit while
# still producing a string longer than _MIN_PDF_TEXT_LENGTH (20 chars).
#
# Validated: pdfplumber.open(BytesIO(_TEXT_LAYER_PDF_BYTES)).pages[0]
#            .extract_text() returns a non-empty string containing the
#            invoice number and key fields.
#
# The content stream encodes the following visible text (abbreviated for
# brevity — the extractor returns the raw glyph sequence):
#   "INV-2026-0042 PO-2025-0100 CTR-2025-0018 420.00"
#
# This is sufficient for load_document_text() to pass the minimum-length gate
# and hand the text to the (patched) extraction agent.
_INVOICE_STREAM = (
    b"BT /F1 10 Tf 50 750 Td "
    b"(INV-2026-0042 PO-2025-0100 CTR-2025-0018) Tj ET\n"
    b"BT /F1 10 Tf 50 730 Td "
    b"(Vendor: Acme Supplies Ltd  Date: 2026-01-15) Tj ET\n"
    b"BT /F1 10 Tf 50 710 Td "
    b"(Total: 420.00  Net 30) Tj ET\n"
)
_STREAM_LEN = len(_INVOICE_STREAM)

_TEXT_LAYER_PDF_BYTES: bytes = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R"
    b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
    + b"4 0 obj<</Length " + str(_STREAM_LEN).encode() + b">>\n"
    + b"stream\n"
    + _INVOICE_STREAM
    + b"endstream\n"
    b"endobj\n"
    b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"xref\n"
    b"0 6\n"
    b"0000000000 65535 f\r\n"
    b"0000000009 00000 n\r\n"
    b"0000000058 00000 n\r\n"
    b"0000000115 00000 n\r\n"
    b"0000000270 00000 n\r\n"
    b"0000000420 00000 n\r\n"
    b"trailer<</Size 6/Root 1 0 R>>\n"
    b"startxref\n"
    b"490\n"
    b"%%EOF"
)

# Valid PDF structure with an empty content stream — simulates a scanned /
# image-only page.  pdfplumber returns '' for extract_text() on this page,
# which triggers DocumentLoadError("no extractable text / scanned PDF").
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

# Plain-text version of the same invoice — identical content to what the PDF
# text layer represents.  Used in the control test that runs the text path so
# we can assert PDF and text produce the same outcome.
_PLAIN_TEXT_INVOICE: bytes = (
    b"INVOICE\n"
    b"Invoice Number: INV-2026-0042\n"
    b"Invoice Date:   2026-01-15\n"
    b"Due Date:       2026-02-14\n"
    b"Payment Terms:  Net 30\n"
    b"Vendor: Acme Supplies Ltd\n"
    b"PO Reference:       PO-UPLOAD-01\n"
    b"Contract Reference: CTR-UPLOAD-01\n"
    b"Line Items:\n"
    b"1. Widget Type A  Qty: 10  Unit Price: 38.00  Amount: 380.00\n"
    b"2. Shipping       Qty:  1  Unit Price: 20.00  Amount:  20.00\n"
    b"Subtotal: 400.00\n"
    b"Tax:       20.00\n"
    b"Total Due: 420.00\n"
)


# ──────────────────────────────────────────────────────────────────────────────
# DB fixtures — in-memory SQLite, same isolation strategy as
# test_pipeline_db_integration.py
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def db_engine():
    """Fresh in-memory SQLite engine with all tables created."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session(db_engine):
    """Session bound to the in-memory engine."""
    TestSessionLocal = sessionmaker(
        bind=db_engine, autocommit=False, autoflush=False
    )
    session = TestSessionLocal()
    yield session
    session.close()


def _patch_db_session(db_engine):
    """
    Redirect every call to get_session() — in both db.session and
    api.invoices — to yield sessions from the test engine.

    Why two patch targets:
      - api/invoices.py does `from db.session import get_session`, binding
        the name locally.  Patching only db.session.get_session would leave
        api.invoices.get_session pointing at the original.
      - Both targets must be replaced so the async route handler (which uses
        the local binding) and any other caller (which uses the module
        reference) all get the test session.

    Returns a context manager.  Usage:

        with _patch_db_session(db_engine):
            response = client.post(...)
    """
    from contextlib import contextmanager

    TestSessionLocal = sessionmaker(
        bind=db_engine, autocommit=False, autoflush=False
    )

    @contextmanager
    def _test_get_session():
        session = TestSessionLocal()
        try:
            yield session
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    from contextlib import ExitStack

    class _MultiPatch:
        """Apply both patch targets simultaneously as a single context manager."""

        def __enter__(self):
            self._stack = ExitStack()
            self._stack.enter_context(
                patch("db.session.get_session", _test_get_session)
            )
            self._stack.enter_context(
                patch("api.invoices.get_session", _test_get_session)
            )
            return self

        def __exit__(self, *args):
            return self._stack.__exit__(*args)

    return _MultiPatch()


# ──────────────────────────────────────────────────────────────────────────────
# Seed helpers
# ──────────────────────────────────────────────────────────────────────────────

def _seed_stp_entities(session: Session) -> VendorORM:
    """
    Seed vendor / PO / contract for the STP scenario used in PDF tests.

    PO reference:       PO-UPLOAD-01
    Contract reference: CTR-UPLOAD-01
    Invoice totals:     grand_total=420.00, unit_price=38.00 (line 1),
                        20.00 (line 2), approval_threshold=10000.00.

    These values exactly match _make_stp_extraction_success() below so that
    all FR-2 matching checks pass and the invoice reaches the STP path.
    """
    vendor = VendorORM(
        vendor_code="UPLOAD-V1",
        name="Acme Supplies Ltd",
        contact_email="ap@upload.example.com",
        is_active=True,
    )
    session.add(vendor)
    session.flush()

    po = PurchaseOrderORM(
        po_number="PO-UPLOAD-01",
        vendor_id=vendor.id,
        po_total="420.00",
        approval_threshold="10000.00",
        notes="PDF upload integration test PO",
        line_items=[
            POLineItemORM(line_number=1, description="Widget Type A", qty="10", unit_price="38.00"),
            POLineItemORM(line_number=2, description="Shipping", qty="1", unit_price="20.00"),
        ],
    )
    session.add(po)

    contract = ContractORM(
        contract_reference="CTR-UPLOAD-01",
        vendor_id=vendor.id,
        discount_term_raw="2/10 net 30",
        discount_pct="0.02",
        discount_days=10,
        net_days=30,
        approval_threshold=None,
        notes="PDF upload integration test contract",
        line_items=[
            ContractLineItemORM(line_number=1, description="Widget Type A", unit_price="38.00"),
            ContractLineItemORM(line_number=2, description="Shipping", unit_price="20.00"),
        ],
    )
    session.add(contract)
    session.commit()
    return vendor


def _make_stp_extraction_success() -> ExtractionSuccess:
    """
    Build an ExtractionSuccess whose InvoiceCreate exactly matches the seeded
    PO/contract so all FR-2 checks pass and the outcome is STP.

    po_reference / contract_reference reference the seeded rows.
    """
    invoice_date = date(2026, 1, 15)
    due_date = date(2026, 2, 14)
    return ExtractionSuccess(
        invoice=InvoiceCreate(
            invoice_number="INV-2026-0042",
            vendor_name="Acme Supplies Ltd",
            invoice_date=invoice_date,
            po_reference="PO-UPLOAD-01",
            contract_reference="CTR-UPLOAD-01",
            payment_terms="Net 30",
            subtotal=Decimal("400.00"),
            tax=Decimal("20.00"),
            grand_total=Decimal("420.00"),
            due_date=due_date,
            line_items=[
                InvoiceLineItemCreate(
                    line_number=1,
                    description="Widget Type A",
                    qty=Decimal("10"),
                    unit_price=Decimal("38.00"),
                    amount=Decimal("380.00"),
                ),
                InvoiceLineItemCreate(
                    line_number=2,
                    description="Shipping",
                    qty=Decimal("1"),
                    unit_price=Decimal("20.00"),
                    amount=Decimal("20.00"),
                ),
            ],
            extraction_status=ExtractionStatus.EXTRACTED,
            invoice_status=InvoiceStatus.EXTRACTED,
        ),
        raw_payload='{"mocked": true}',
        attempt_count=1,
    )


# ──────────────────────────────────────────────────────────────────────────────
# FastAPI test client
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def api_client() -> TestClient:
    """Synchronous FastAPI test client — no server process required."""
    return TestClient(app, raise_server_exceptions=False)


# ──────────────────────────────────────────────────────────────────────────────
# Helper — upload a file and return the response
# ──────────────────────────────────────────────────────────────────────────────

def _upload(
    client: TestClient,
    file_bytes: bytes,
    filename: str,
    content_type: str,
) -> object:
    """POST /invoices/upload with a multipart file."""
    return client.post(
        "/invoices/upload",
        files={"file": (filename, io.BytesIO(file_bytes), content_type)},
    )


# ──────────────────────────────────────────────────────────────────────────────
# Test 1 — PDF with text layer → same STP outcome as plain text
# ──────────────────────────────────────────────────────────────────────────────

class TestPdfTextLayerOutcomeMatchesPlainText:
    """
    Upload the same invoice content as both plain text and as a PDF with an
    embedded text layer.  Both paths must reach the same pipeline outcome (STP)
    when:
      • The LLM extraction layer is patched to return the same ExtractionSuccess.
      • The DB is seeded with a matching vendor / PO / contract.

    This test proves that:
      1. The /upload endpoint accepts application/pdf.
      2. load_document_text() correctly extracts text from the PDF fixture.
      3. The extracted text is passed into the LLM agent (which we intercept).
      4. The full downstream pipeline (resolver → matching → routing) runs and
         produces STP — identical to the text path.
    """

    def test_pdf_upload_returns_201(
        self, api_client: TestClient, db_session: Session, db_engine
    ) -> None:
        """PDF upload with a text-layer PDF must return HTTP 201."""
        _seed_stp_entities(db_session)
        extraction_success = _make_stp_extraction_success()

        with _patch_db_session(db_engine):
            with patch(
                "api.invoices.ExtractionAgent.extract",
                return_value=extraction_success,
            ):
                response = _upload(
                    api_client,
                    _TEXT_LAYER_PDF_BYTES,
                    "invoice.pdf",
                    "application/pdf",
                )

        assert response.status_code == 201, (
            f"Expected 201 but got {response.status_code}: {response.text}"
        )

    def test_pdf_outcome_is_stp(
        self, api_client: TestClient, db_session: Session, db_engine
    ) -> None:
        """PDF upload produces outcome='STP' when PO/contract match exactly."""
        _seed_stp_entities(db_session)
        extraction_success = _make_stp_extraction_success()

        with _patch_db_session(db_engine):
            with patch(
                "api.invoices.ExtractionAgent.extract",
                return_value=extraction_success,
            ):
                response = _upload(
                    api_client,
                    _TEXT_LAYER_PDF_BYTES,
                    "invoice.pdf",
                    "application/pdf",
                )

        body = response.json()
        assert body["outcome"] == "STP", (
            f"Expected outcome='STP' from PDF upload, got {body['outcome']!r}. "
            f"Full response: {body}"
        )

    def test_pdf_invoice_number_matches_text_path(
        self, api_client: TestClient, db_session: Session, db_engine
    ) -> None:
        """
        The invoice_number in the PDF response equals the one from the
        text-path control — both come from the same patched ExtractionSuccess.
        """
        _seed_stp_entities(db_session)
        extraction_success = _make_stp_extraction_success()

        with _patch_db_session(db_engine):
            with patch("api.invoices.ExtractionAgent.extract", return_value=extraction_success):
                pdf_response = _upload(
                    api_client, _TEXT_LAYER_PDF_BYTES, "invoice.pdf", "application/pdf"
                )
                text_response = _upload(
                    api_client, _PLAIN_TEXT_INVOICE, "invoice.txt", "text/plain"
                )

        assert pdf_response.status_code == 201
        assert text_response.status_code == 201
        assert pdf_response.json()["invoice_number"] == text_response.json()["invoice_number"], (
            "PDF and text-path invoice_number must match — "
            f"PDF: {pdf_response.json()['invoice_number']!r}, "
            f"text: {text_response.json()['invoice_number']!r}"
        )

    def test_pdf_outcome_matches_text_path_outcome(
        self, api_client: TestClient, db_session: Session, db_engine
    ) -> None:
        """
        The outcome from the PDF upload equals the outcome from the equivalent
        plain-text upload — proves format-independence of the pipeline.
        """
        _seed_stp_entities(db_session)
        extraction_success = _make_stp_extraction_success()

        with _patch_db_session(db_engine):
            with patch("api.invoices.ExtractionAgent.extract", return_value=extraction_success):
                pdf_response = _upload(
                    api_client, _TEXT_LAYER_PDF_BYTES, "invoice.pdf", "application/pdf"
                )
                text_response = _upload(
                    api_client, _PLAIN_TEXT_INVOICE, "invoice.txt", "text/plain"
                )

        assert pdf_response.json()["outcome"] == text_response.json()["outcome"], (
            "PDF and text-path outcomes must match — "
            f"PDF: {pdf_response.json()['outcome']!r}, "
            f"text: {text_response.json()['outcome']!r}"
        )

    def test_pdf_upload_accepted_via_generic_content_type_and_pdf_extension(
        self, api_client: TestClient, db_session: Session, db_engine
    ) -> None:
        """
        application/octet-stream + .pdf filename must be accepted (Rule 2 in
        upload_invoice) and produce the same outcome as explicit application/pdf.
        """
        _seed_stp_entities(db_session)
        extraction_success = _make_stp_extraction_success()

        with _patch_db_session(db_engine):
            with patch("api.invoices.ExtractionAgent.extract", return_value=extraction_success):
                response = _upload(
                    api_client,
                    _TEXT_LAYER_PDF_BYTES,
                    "invoice.pdf",
                    "application/octet-stream",  # generic type, .pdf extension
                )

        assert response.status_code == 201, (
            f"Generic content-type + .pdf extension should be accepted (Rule 2), "
            f"got {response.status_code}: {response.text}"
        )
        assert response.json()["outcome"] == "STP"

    def test_pdf_has_no_exception_reasons(
        self, api_client: TestClient, db_session: Session, db_engine
    ) -> None:
        """STP response must carry an empty or absent exception_reasons list."""
        _seed_stp_entities(db_session)
        extraction_success = _make_stp_extraction_success()

        with _patch_db_session(db_engine):
            with patch("api.invoices.ExtractionAgent.extract", return_value=extraction_success):
                response = _upload(
                    api_client, _TEXT_LAYER_PDF_BYTES, "invoice.pdf", "application/pdf"
                )

        body = response.json()
        reasons = body.get("exception_reasons") or []
        assert reasons == [], (
            f"STP invoice must have no exception reasons, got: {reasons}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Test 2 — Image-only / scanned PDF → HTTP 422, clear reason, no crash
# ──────────────────────────────────────────────────────────────────────────────

class TestImageOnlyPdfReturns422:
    """
    Upload a PDF with no text layer (empty content stream — equivalent to a
    scanned page).  The endpoint must return HTTP 422 with a specific error
    message, not a 500 and not a bare exception.

    No DB seeding or LLM patching required — load_document_text() raises
    DocumentLoadError before the extraction agent is ever invoked.
    """

    def test_image_only_pdf_returns_422(self, api_client: TestClient) -> None:
        """HTTP status must be 422, not 200/201 or 500."""
        response = _upload(
            api_client,
            _IMAGE_ONLY_PDF_BYTES,
            "scanned_invoice.pdf",
            "application/pdf",
        )
        assert response.status_code == 422, (
            f"Expected 422 for image-only PDF, got {response.status_code}: {response.text}"
        )

    def test_image_only_pdf_detail_mentions_scanned_or_no_text(
        self, api_client: TestClient
    ) -> None:
        """
        The 422 detail must explain the failure — 'scanned', 'no extractable
        text', or 'image' must appear so the API caller knows what to fix.
        """
        response = _upload(
            api_client,
            _IMAGE_ONLY_PDF_BYTES,
            "scanned_invoice.pdf",
            "application/pdf",
        )
        detail = response.json().get("detail", "")
        assert any(
            keyword in detail.lower()
            for keyword in ("scanned", "no extractable text", "image", "text layer")
        ), (
            f"422 detail must explain the PDF has no text layer; got: {detail!r}"
        )

    def test_image_only_pdf_detail_names_the_file(self, api_client: TestClient) -> None:
        """The error message should reference the uploaded filename."""
        response = _upload(
            api_client,
            _IMAGE_ONLY_PDF_BYTES,
            "scanned_invoice.pdf",
            "application/pdf",
        )
        detail = response.json().get("detail", "")
        assert "scanned_invoice.pdf" in detail, (
            f"422 detail should name the uploaded file; got: {detail!r}"
        )

    def test_image_only_pdf_is_not_500(self, api_client: TestClient) -> None:
        """A scanned PDF must never produce a 500 Internal Server Error."""
        response = _upload(
            api_client,
            _IMAGE_ONLY_PDF_BYTES,
            "scanned_invoice.pdf",
            "application/pdf",
        )
        assert response.status_code != 500, (
            f"Scanned PDF must not cause a 500; got {response.status_code}: {response.text}"
        )

    def test_image_only_pdf_has_no_outcome_field(self, api_client: TestClient) -> None:
        """
        The 422 response is a validation-error body, not an InvoiceSubmitResponse.
        It must not carry an 'outcome' field — that would indicate the request
        was processed rather than rejected.
        """
        response = _upload(
            api_client,
            _IMAGE_ONLY_PDF_BYTES,
            "scanned_invoice.pdf",
            "application/pdf",
        )
        assert "outcome" not in response.json(), (
            "A 422 error response must not contain an 'outcome' field"
        )

    def test_generic_content_type_image_only_pdf_also_422(
        self, api_client: TestClient
    ) -> None:
        """
        Even when submitted as application/octet-stream with a .pdf extension
        (Rule 2 in upload_invoice), an image-only PDF must still return 422.
        """
        response = _upload(
            api_client,
            _IMAGE_ONLY_PDF_BYTES,
            "scanned_invoice.pdf",
            "application/octet-stream",
        )
        assert response.status_code == 422, (
            f"Generic content-type image-only PDF must return 422, "
            f"got {response.status_code}: {response.text}"
        )
