"""
tests/unit/test_field_confidence.py — field_confidence extraction and routing tests.

Covers three scenarios per the spec:

1. Unambiguous price  → "high" confidence (or no entry) → ExtractionSuccess with
   empty/high field_confidence → pipeline does NOT route to LOW_CONFIDENCE_EXTRACTION.

2. Price in unusual format the LLM can still read → "low" confidence in
   field_confidence → pipeline routes to EXCEPTION with reason
   LOW_CONFIDENCE_EXTRACTION, NOT a hard extraction failure (ExtractionSuccess
   is still returned by the agent), and NOT STP.

3. No price in the document at all → null grand_total → ExtractionFailure
   (NEEDS_REEXTRACTION) — unchanged behavior, completely unaffected by this
   change.

All LLM calls are mocked — no network required.
Pipeline DB calls are patched at the module boundary to keep tests fast and
hermetic.
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from extraction.agent import ExtractionAgent
from extraction.schemas import ExtractionFailure, ExtractionSuccess, FailureReason
from models.enums import ExceptionReasonCode, ExtractionStatus
from ui.components.pipeline_runner import (
    PipelineResult,
    _collect_low_confidence_fields,
    run_extraction_pipeline,
)


# ---------------------------------------------------------------------------
# Shared fixture data
# ---------------------------------------------------------------------------

# A fully valid invoice payload — all fields present, no confidence issues.
_BASE_INVOICE: dict = {
    "invoice_number": "INV-2026-9999",
    "vendor_name": "Acme Corp",
    "invoice_date": "2026-03-01",
    "po_reference": "PO-2026-001",
    "contract_reference": "CTR-2026-001",
    "subtotal": 1000.00,
    "tax": 100.00,
    "grand_total": 1100.00,
    "due_date": "2026-04-01",
    "payment_terms": "Net 30",
    "line_items": [
        {
            "line_number": 1,
            "description": "Widget Type A",
            "qty": 10,
            "unit_price": 100.00,
            "amount": 1000.00,
        }
    ],
}


def _make_llm_client(responses: list[str]) -> MagicMock:
    """Return a mock LLMClient whose complete() yields responses in sequence."""
    client = MagicMock()
    client.complete.side_effect = responses
    return client


def _invoice_json(**extra_fields) -> str:
    """Serialise _BASE_INVOICE with optional extra top-level fields merged in."""
    payload = {**_BASE_INVOICE, **extra_fields}
    return json.dumps(payload)



# ---------------------------------------------------------------------------
# Scenario 1 — Unambiguous price → "high" confidence → no exception
# ---------------------------------------------------------------------------

class TestHighConfidenceExtraction:
    """
    When the LLM reports all fields as "high" (or omits field_confidence
    entirely), the agent returns ExtractionSuccess with an empty or all-high
    field_confidence dict.  The pipeline helper _collect_low_confidence_fields
    returns None, so no LOW_CONFIDENCE_EXTRACTION exception is raised.
    """

    def test_agent_high_confidence_no_flag(self):
        """LLM returns valid JSON with field_confidence all "high" — success."""
        raw = _invoice_json(field_confidence={"grand_total": "high", "unit_price": "high"})
        client = _make_llm_client([raw])
        agent = ExtractionAgent(llm_client=client)
        result = agent.extract("invoice text")

        assert isinstance(result, ExtractionSuccess)
        assert result.field_confidence == {"grand_total": "high", "unit_price": "high"}

    def test_agent_no_field_confidence_key(self):
        """LLM omits field_confidence entirely — defaults to empty dict."""
        raw = _invoice_json()  # no field_confidence key
        client = _make_llm_client([raw])
        agent = ExtractionAgent(llm_client=client)
        result = agent.extract("invoice text")

        assert isinstance(result, ExtractionSuccess)
        assert result.field_confidence == {}

    def test_collect_helper_returns_none_for_all_high(self):
        """_collect_low_confidence_fields returns None when all entries are high."""
        raw = _invoice_json(field_confidence={"grand_total": "high"})
        client = _make_llm_client([raw])
        agent = ExtractionAgent(llm_client=client)
        result = agent.extract("invoice text")

        assert isinstance(result, ExtractionSuccess)
        low = _collect_low_confidence_fields(result, result.invoice)
        assert low is None

    def test_collect_helper_returns_none_for_empty_dict(self):
        """_collect_low_confidence_fields returns None for empty field_confidence."""
        raw = _invoice_json()
        client = _make_llm_client([raw])
        agent = ExtractionAgent(llm_client=client)
        result = agent.extract("invoice text")

        assert isinstance(result, ExtractionSuccess)
        low = _collect_low_confidence_fields(result, result.invoice)
        assert low is None

    def test_pipeline_does_not_raise_low_confidence_exception(self):
        """
        End-to-end: high-confidence extraction → pipeline does NOT short-circuit
        to LOW_CONFIDENCE_EXTRACTION.

        DB and LLM calls are both patched so the test is hermetic.  The
        run_extraction_pipeline path calls run_extraction_pipeline_with_documents
        which calls run_pipeline; we patch run_pipeline to return a canned STP
        result so the test focuses solely on the low-confidence gate.
        """
        from services.invoice_service import InvoiceProcessingResult

        raw = _invoice_json(field_confidence={"grand_total": "high"})

        stp_service_result = InvoiceProcessingResult(
            invoice_id="test-stp-id",
            invoice_number="INV-2026-9999",
            outcome="STP",
            exception_reasons=[],
            processed_at="2026-03-01T00:00:00Z",
        )

        with (
            patch(
                "ui.components.pipeline_runner.OpenRouterClient"
            ) as mock_llm_cls,
            patch(
                "ui.components.pipeline_runner.ExtractionAgent"
            ) as mock_agent_cls,
            patch(
                "ui.components.pipeline_runner.get_session"
            ),
            patch(
                "ui.components.pipeline_runner.resolve_invoice_entities"
            ) as mock_resolve,
            patch(
                "ui.components.pipeline_runner.run_pipeline",
                return_value=stp_service_result,
            ),
            patch("ui.components.pipeline_runner.audit_writer"),
        ):
            # Wire the mock agent to return a high-confidence ExtractionSuccess.
            import json as _json
            from extraction.schemas import ExtractionSuccess as _ES
            from models.invoice import InvoiceCreate as _IC

            parsed = _json.loads(raw)
            parsed.pop("field_confidence", None)
            invoice_obj = _IC(**parsed)
            mock_extraction = _ES(
                invoice=invoice_obj,
                raw_payload=raw,
                attempt_count=1,
                field_confidence={"grand_total": "high"},
            )
            mock_agent_cls.return_value.extract.return_value = mock_extraction

            # resolve_invoice_entities must return a valid-ish object
            from db.resolver import ResolvedEntities
            mock_resolve.return_value = ResolvedEntities(
                vendor=None, po=None, contract=None
            )

            pipeline_result = run_extraction_pipeline("some invoice text")

        # The gate must not have fired — outcome is driven by run_pipeline mock
        assert pipeline_result.outcome == "STP"
        assert pipeline_result.low_confidence_fields is None
        assert ExceptionReasonCode.LOW_CONFIDENCE_EXTRACTION.value not in (
            pipeline_result.exception_reasons or []
        )



# ---------------------------------------------------------------------------
# Scenario 2 — Ambiguous price the LLM can still read → "low" → EXCEPTION
# ---------------------------------------------------------------------------

class TestLowConfidenceExtraction:
    """
    When the LLM reports one or more fields as "low" confidence, the agent
    still returns ExtractionSuccess (not a failure — the value was found).
    The pipeline gate then routes to EXCEPTION with LOW_CONFIDENCE_EXTRACTION.
    """

    def test_agent_returns_success_with_low_confidence_flag(self):
        """
        LLM reads grand_total from a European-format string "1.100,00" and
        correctly returns 1100.00 — but flags it as low confidence because
        the format was non-standard.  Agent returns ExtractionSuccess, not
        ExtractionFailure.
        """
        raw = _invoice_json(
            grand_total=1100.00,
            field_confidence={"grand_total": "low"},
        )
        client = _make_llm_client([raw])
        agent = ExtractionAgent(llm_client=client)
        result = agent.extract("invoice with 1.100,00 grand total")

        # Must be a SUCCESS — the value was extracted, just flagged uncertain.
        assert isinstance(result, ExtractionSuccess), (
            "Expected ExtractionSuccess but got ExtractionFailure — "
            "low confidence should not be a hard extraction failure."
        )
        assert result.extraction_status == ExtractionStatus.EXTRACTED
        assert result.invoice.grand_total == Decimal("1100.00")
        assert result.field_confidence.get("grand_total") == "low"

    def test_agent_preserves_extracted_value_when_low(self):
        """The extracted value under 'low' confidence is real, not null or invented."""
        raw = _invoice_json(
            unit_price_override=None,  # ignored — we use line_items
            field_confidence={"grand_total": "low", "payment_terms": "low"},
        )
        client = _make_llm_client([raw])
        agent = ExtractionAgent(llm_client=client)
        result = agent.extract("invoice text")

        assert isinstance(result, ExtractionSuccess)
        # The real extracted value is present
        assert result.invoice.grand_total == Decimal("1100.00")
        # Both fields are flagged
        assert result.field_confidence == {"grand_total": "low", "payment_terms": "low"}

    def test_collect_helper_returns_low_fields(self):
        """_collect_low_confidence_fields returns only the 'low' entries."""
        raw = _invoice_json(
            field_confidence={"grand_total": "low", "invoice_number": "high"},
        )
        client = _make_llm_client([raw])
        agent = ExtractionAgent(llm_client=client)
        result = agent.extract("invoice text")

        assert isinstance(result, ExtractionSuccess)
        low = _collect_low_confidence_fields(result, result.invoice)

        assert low is not None
        assert "grand_total" in low
        # high-confidence fields must NOT appear
        assert "invoice_number" not in low

    def test_collect_helper_value_is_extracted_value(self):
        """The dict value should be the string representation of what was extracted."""
        raw = _invoice_json(field_confidence={"grand_total": "low"})
        client = _make_llm_client([raw])
        agent = ExtractionAgent(llm_client=client)
        result = agent.extract("invoice text")

        assert isinstance(result, ExtractionSuccess)
        low = _collect_low_confidence_fields(result, result.invoice)

        assert low is not None
        # grand_total was extracted as 1100.00; Decimal str() may render as "1100.0"
        assert low["grand_total"] == str(result.invoice.grand_total)

    def test_pipeline_routes_to_low_confidence_exception(self):
        """
        End-to-end: low-confidence extraction → outcome is EXCEPTION with
        LOW_CONFIDENCE_EXTRACTION reason code; run_pipeline is never called
        (the gate fires before any DB work).
        """
        raw = _invoice_json(field_confidence={"grand_total": "low"})

        with (
            patch("ui.components.pipeline_runner.OpenRouterClient"),
            patch(
                "ui.components.pipeline_runner.ExtractionAgent"
            ) as mock_agent_cls,
            patch("ui.components.pipeline_runner.get_session"),
            patch("ui.components.pipeline_runner.resolve_invoice_entities"),
            patch(
                "ui.components.pipeline_runner.run_pipeline"
            ) as mock_run_pipeline,
            patch("ui.components.pipeline_runner.audit_writer"),
        ):
            import json as _json
            from extraction.schemas import ExtractionSuccess as _ES
            from models.invoice import InvoiceCreate as _IC

            parsed = _json.loads(raw)
            parsed.pop("field_confidence", None)
            invoice_obj = _IC(**parsed)
            mock_extraction = _ES(
                invoice=invoice_obj,
                raw_payload=raw,
                attempt_count=1,
                field_confidence={"grand_total": "low"},
            )
            mock_agent_cls.return_value.extract.return_value = mock_extraction

            pipeline_result = run_extraction_pipeline("invoice with unusual price format")

        assert pipeline_result.outcome == "EXCEPTION"
        assert ExceptionReasonCode.LOW_CONFIDENCE_EXTRACTION.value in (
            pipeline_result.exception_reasons
        )
        # run_pipeline must NOT have been called — gate fires before DB work
        mock_run_pipeline.assert_not_called()

    def test_pipeline_surfaces_low_confidence_fields(self):
        """
        The PipelineResult.low_confidence_fields dict contains the flagged
        field names and their extracted values for human review.
        """
        raw = _invoice_json(field_confidence={"grand_total": "low"})

        with (
            patch("ui.components.pipeline_runner.OpenRouterClient"),
            patch(
                "ui.components.pipeline_runner.ExtractionAgent"
            ) as mock_agent_cls,
            patch("ui.components.pipeline_runner.get_session"),
            patch("ui.components.pipeline_runner.resolve_invoice_entities"),
            patch("ui.components.pipeline_runner.run_pipeline"),
            patch("ui.components.pipeline_runner.audit_writer"),
        ):
            import json as _json
            from extraction.schemas import ExtractionSuccess as _ES
            from models.invoice import InvoiceCreate as _IC

            parsed = _json.loads(raw)
            parsed.pop("field_confidence", None)
            invoice_obj = _IC(**parsed)
            mock_extraction = _ES(
                invoice=invoice_obj,
                raw_payload=raw,
                attempt_count=1,
                field_confidence={"grand_total": "low"},
            )
            mock_agent_cls.return_value.extract.return_value = mock_extraction

            pipeline_result = run_extraction_pipeline("invoice text")

        assert pipeline_result.low_confidence_fields is not None
        assert "grand_total" in pipeline_result.low_confidence_fields
        # Value is the string representation of the extracted Decimal amount
        assert Decimal(pipeline_result.low_confidence_fields["grand_total"]) == Decimal("1100.00")

    def test_pipeline_invoice_fields_present_for_human_review(self):
        """
        Even though the invoice is routed to EXCEPTION, the extracted invoice
        fields are still present on PipelineResult so the human reviewer can
        see what was extracted.
        """
        raw = _invoice_json(field_confidence={"grand_total": "low"})

        with (
            patch("ui.components.pipeline_runner.OpenRouterClient"),
            patch(
                "ui.components.pipeline_runner.ExtractionAgent"
            ) as mock_agent_cls,
            patch("ui.components.pipeline_runner.get_session"),
            patch("ui.components.pipeline_runner.resolve_invoice_entities"),
            patch("ui.components.pipeline_runner.run_pipeline"),
            patch("ui.components.pipeline_runner.audit_writer"),
        ):
            import json as _json
            from extraction.schemas import ExtractionSuccess as _ES
            from models.invoice import InvoiceCreate as _IC

            parsed = _json.loads(raw)
            parsed.pop("field_confidence", None)
            invoice_obj = _IC(**parsed)
            mock_extraction = _ES(
                invoice=invoice_obj,
                raw_payload=raw,
                attempt_count=1,
                field_confidence={"grand_total": "low"},
            )
            mock_agent_cls.return_value.extract.return_value = mock_extraction

            pipeline_result = run_extraction_pipeline("invoice text")

        assert pipeline_result.invoice_fields is not None
        assert pipeline_result.invoice_fields["invoice_number"] == "INV-2026-9999"

    def test_low_confidence_is_not_hard_extraction_failure(self):
        """
        Confirm the outcome is EXCEPTION (not NEEDS_REEXTRACTION).
        Low confidence means "found something uncertain", not "couldn't extract".
        """
        raw = _invoice_json(field_confidence={"grand_total": "low"})

        with (
            patch("ui.components.pipeline_runner.OpenRouterClient"),
            patch(
                "ui.components.pipeline_runner.ExtractionAgent"
            ) as mock_agent_cls,
            patch("ui.components.pipeline_runner.get_session"),
            patch("ui.components.pipeline_runner.resolve_invoice_entities"),
            patch("ui.components.pipeline_runner.run_pipeline"),
            patch("ui.components.pipeline_runner.audit_writer"),
        ):
            import json as _json
            from extraction.schemas import ExtractionSuccess as _ES
            from models.invoice import InvoiceCreate as _IC

            parsed = _json.loads(raw)
            parsed.pop("field_confidence", None)
            invoice_obj = _IC(**parsed)
            mock_extraction = _ES(
                invoice=invoice_obj,
                raw_payload=raw,
                attempt_count=1,
                field_confidence={"grand_total": "low"},
            )
            mock_agent_cls.return_value.extract.return_value = mock_extraction

            pipeline_result = run_extraction_pipeline("invoice text")

        assert pipeline_result.outcome != "NEEDS_REEXTRACTION", (
            "LOW_CONFIDENCE_EXTRACTION must route to EXCEPTION, "
            "not be treated as a hard extraction failure."
        )
        assert pipeline_result.outcome == "EXCEPTION"



# ---------------------------------------------------------------------------
# Scenario 3 — No price at all → null → NEEDS_REEXTRACTION (unchanged behavior)
# ---------------------------------------------------------------------------

class TestNullFieldUnchangedBehavior:
    """
    When a required field (e.g. grand_total) is genuinely absent from the
    document, the LLM sets it to null.  Pydantic validation fails →
    ExtractionFailure with NEEDS_REEXTRACTION.

    This behavior is completely unaffected by the field_confidence change.
    There is no confidence flag for an absent field — null means absent.
    """

    def test_agent_null_grand_total_fails_validation(self):
        """
        LLM returns null for grand_total (field absent in doc).
        Pydantic rejects it → ExtractionFailure on both attempts.
        """
        null_payload = {**_BASE_INVOICE, "grand_total": None}
        raw1 = json.dumps(null_payload)
        raw2 = json.dumps(null_payload)  # retry also null
        client = _make_llm_client([raw1, raw2])
        agent = ExtractionAgent(llm_client=client)
        result = agent.extract("invoice with no total stated")

        assert isinstance(result, ExtractionFailure)
        assert result.extraction_status == ExtractionStatus.NEEDS_REEXTRACTION
        assert result.attempt_count == 2

    def test_null_field_has_no_confidence_flag(self):
        """
        The LLM correctly omits the field from field_confidence when it is null
        (absent), not just uncertain.  Parsing still produces ExtractionFailure —
        but we also verify the concept: no confidence entry for an absent field.
        """
        # Simulate the LLM correctly omitting confidence for a null field.
        null_payload = {
            **_BASE_INVOICE,
            "grand_total": None,
            # field_confidence should NOT contain grand_total
            "field_confidence": {"invoice_number": "high"},
        }
        raw = json.dumps(null_payload)
        client = _make_llm_client([raw, raw])
        agent = ExtractionAgent(llm_client=client)
        result = agent.extract("invoice text")

        # Validation still fails — grand_total is required and is null.
        assert isinstance(result, ExtractionFailure)
        assert result.extraction_status == ExtractionStatus.NEEDS_REEXTRACTION

    def test_null_field_pipeline_returns_needs_reextraction(self):
        """
        End-to-end: LLM returns ExtractionFailure → pipeline outcome is
        NEEDS_REEXTRACTION — same as before, unaffected by field_confidence.
        """
        null_payload = {**_BASE_INVOICE, "grand_total": None}
        raw = json.dumps(null_payload)

        from extraction.schemas import ExtractionFailure as _EF

        mock_failure = _EF(
            reason=FailureReason.SCHEMA_VALIDATION_FAILED,
            error_detail="grand_total is required",
            raw_payload=raw,
            attempt_count=2,
        )

        with (
            patch("ui.components.pipeline_runner.OpenRouterClient"),
            patch(
                "ui.components.pipeline_runner.ExtractionAgent"
            ) as mock_agent_cls,
            patch("ui.components.pipeline_runner.audit_writer"),
        ):
            mock_agent_cls.return_value.extract.return_value = mock_failure

            pipeline_result = run_extraction_pipeline("invoice with no price")

        assert pipeline_result.outcome == "NEEDS_REEXTRACTION"
        assert pipeline_result.extraction_failure_reason is not None
        # low_confidence_fields is None — we never reached the confidence gate
        assert pipeline_result.low_confidence_fields is None

    def test_null_field_does_not_trigger_low_confidence_path(self):
        """
        Absence of a field (null) must NOT be confused with low-confidence
        presence of a field.  The two code paths are orthogonal.
        """
        # If by some bug the agent returns ExtractionSuccess with a null-valued
        # field and a "low" confidence entry for a DIFFERENT field, only the
        # actually-low-confidence field should be flagged.
        # (In practice, a null required field would be caught by Pydantic first.)
        raw = _invoice_json(
            field_confidence={"payment_terms": "low"},
            # grand_total is still present and valid at 1100.00
        )
        client = _make_llm_client([raw])
        agent = ExtractionAgent(llm_client=client)
        result = agent.extract("invoice text")

        assert isinstance(result, ExtractionSuccess)
        low = _collect_low_confidence_fields(result, result.invoice)
        # Only payment_terms is low — grand_total is not involved
        assert low == {"payment_terms": "Net 30"}


# ---------------------------------------------------------------------------
# Unit tests for _collect_low_confidence_fields in isolation
# ---------------------------------------------------------------------------

class TestCollectLowConfidenceHelper:
    """Direct unit tests for the pipeline helper function."""

    def _make_success_with_confidence(self, confidence: dict) -> ExtractionSuccess:
        """Build a minimal ExtractionSuccess with the given field_confidence."""
        raw = _invoice_json(field_confidence=confidence)
        client = _make_llm_client([raw])
        agent = ExtractionAgent(llm_client=client)
        result = agent.extract("text")
        assert isinstance(result, ExtractionSuccess)
        return result

    def test_empty_dict_returns_none(self):
        result = self._make_success_with_confidence({})
        assert _collect_low_confidence_fields(result, result.invoice) is None

    def test_all_high_returns_none(self):
        result = self._make_success_with_confidence(
            {"grand_total": "high", "vendor_name": "high"}
        )
        assert _collect_low_confidence_fields(result, result.invoice) is None

    def test_mixed_returns_only_low_keys(self):
        result = self._make_success_with_confidence(
            {"grand_total": "low", "vendor_name": "high", "due_date": "low"}
        )
        low = _collect_low_confidence_fields(result, result.invoice)
        assert low is not None
        assert set(low.keys()) == {"grand_total", "due_date"}
        assert "vendor_name" not in low

    def test_line_item_field_renders_placeholder(self):
        result = self._make_success_with_confidence(
            {"line_items[0].unit_price": "low"}
        )
        low = _collect_low_confidence_fields(result, result.invoice)
        assert low is not None
        assert low["line_items[0].unit_price"] == "(see line items)"

    def test_unknown_field_name_renders_placeholder(self):
        result = self._make_success_with_confidence({"nonexistent_field": "low"})
        low = _collect_low_confidence_fields(result, result.invoice)
        assert low is not None
        assert low["nonexistent_field"] == "(unknown field)"
