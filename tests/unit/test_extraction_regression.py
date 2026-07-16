"""
tests/unit/test_extraction_regression.py — Regression tests proving InvoiceCreate
extraction behavior is unaffected by the BaseExtractionAgent refactor.

These tests are a full re-run of the original test_extraction.py scenarios using
the refactored ExtractionAgent (backed by BaseExtractionAgent).  They are
intentionally a close mirror of the original test_extraction.py so that any
regression in the shared retry/fail-closed logic is immediately visible.

All LLM calls are mocked — no network required.
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from extraction.agent import ExtractionAgent, InvoiceExtractionAgent
from extraction.llm_client import LLMCallError
from extraction.schemas import ExtractionFailure, ExtractionSuccess, FailureReason
from models.enums import ExtractionStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(responses: list[str]) -> MagicMock:
    client = MagicMock()
    client.complete.side_effect = responses
    return client


def _valid_invoice_json(**overrides) -> str:
    base = {
        "invoice_number": "INV-REGR-001",
        "vendor_name": "Regression Vendor Inc.",
        "invoice_date": "2026-03-01",
        "po_reference": "PO-REGR-100",
        "contract_reference": "CTR-REGR-042",
        "subtotal": 500.00,
        "tax": 50.00,
        "grand_total": 550.00,
        "due_date": "2026-03-31",
        "payment_terms": "Net 30",
        "line_items": [
            {
                "line_number": 1,
                "description": "Widget Alpha",
                "qty": 10,
                "unit_price": 50.00,
                "amount": 500.00,
            }
        ],
    }
    base.update(overrides)
    return json.dumps(base)


# ---------------------------------------------------------------------------
# Regression: agent class identity and import aliases
# ---------------------------------------------------------------------------


class TestAgentIdentity:
    """InvoiceExtractionAgent is ExtractionAgent (alias, not a separate class)."""

    def test_invoice_extraction_agent_is_extraction_agent(self):
        assert InvoiceExtractionAgent is ExtractionAgent

    def test_instantiation_with_old_kwarg(self):
        """prompt_template_path kwarg still accepted (backward compat)."""
        client = _make_client([_valid_invoice_json()])
        agent = ExtractionAgent(llm_client=client, prompt_template_path=None)
        result = agent.extract("invoice text")
        assert isinstance(result, ExtractionSuccess)

    def test_extract_method_exists(self):
        client = _make_client([_valid_invoice_json()])
        agent = ExtractionAgent(llm_client=client)
        assert callable(agent.extract)


# ---------------------------------------------------------------------------
# Regression: happy-path — valid JSON on first attempt
# ---------------------------------------------------------------------------


class TestInvoiceHappyPath:
    def test_succeeds_on_first_attempt(self):
        client = _make_client([_valid_invoice_json()])
        result = ExtractionAgent(llm_client=client).extract("invoice text")

        assert isinstance(result, ExtractionSuccess)
        assert result.outcome == "success"
        assert result.attempt_count == 1
        assert result.extraction_status == ExtractionStatus.EXTRACTED
        assert client.complete.call_count == 1

    def test_invoice_fields_populated_correctly(self):
        client = _make_client([_valid_invoice_json()])
        result = ExtractionAgent(llm_client=client).extract("invoice text")

        assert isinstance(result, ExtractionSuccess)
        inv = result.invoice
        assert inv.invoice_number == "INV-REGR-001"
        assert inv.vendor_name == "Regression Vendor Inc."
        assert inv.grand_total == Decimal("550.00")
        assert inv.subtotal == Decimal("500.00")
        assert len(inv.line_items) == 1

    def test_raw_payload_preserved_verbatim(self):
        raw = _valid_invoice_json()
        client = _make_client([raw])
        result = ExtractionAgent(llm_client=client).extract("invoice text")

        assert isinstance(result, ExtractionSuccess)
        assert result.raw_payload == raw

    def test_fenced_json_still_extracted(self):
        fenced = f"```json\n{_valid_invoice_json()}\n```"
        client = _make_client([fenced])
        result = ExtractionAgent(llm_client=client).extract("invoice text")

        assert isinstance(result, ExtractionSuccess)
        assert result.attempt_count == 1

    def test_prose_prefix_handled(self):
        with_prose = f"Here is the extracted JSON:\n{_valid_invoice_json()}"
        client = _make_client([with_prose])
        result = ExtractionAgent(llm_client=client).extract("invoice text")

        assert isinstance(result, ExtractionSuccess)

    def test_fence_without_language_tag(self):
        fenced = f"```\n{_valid_invoice_json()}\n```"
        client = _make_client([fenced])
        result = ExtractionAgent(llm_client=client).extract("invoice text")

        assert isinstance(result, ExtractionSuccess)


# ---------------------------------------------------------------------------
# Regression: malformed JSON — both attempts
# ---------------------------------------------------------------------------


class TestInvoiceMalformedJSON:
    def test_both_attempts_malformed_fails_closed(self):
        client = _make_client([
            "This is not JSON!",
            "Still broken { json",
        ])
        result = ExtractionAgent(llm_client=client).extract("invoice text")

        assert isinstance(result, ExtractionFailure)
        assert result.outcome == "failure"
        assert result.extraction_status == ExtractionStatus.NEEDS_REEXTRACTION
        assert result.reason == FailureReason.UNPARSEABLE_JSON
        assert client.complete.call_count == 2

    def test_malformed_first_valid_second_succeeds_attempt2(self):
        client = _make_client(["garbage", _valid_invoice_json()])
        result = ExtractionAgent(llm_client=client).extract("invoice text")

        assert isinstance(result, ExtractionSuccess)
        assert result.attempt_count == 2
        assert client.complete.call_count == 2

    def test_retry_message_references_previous_failure(self):
        client = _make_client(["not json", _valid_invoice_json()])
        ExtractionAgent(llm_client=client).extract("ORIGINAL TEXT")

        retry_call = client.complete.call_args_list[1]
        retry_user_msg = (
            retry_call.kwargs.get("user_message")
            or (retry_call.args[1] if len(retry_call.args) > 1 else "")
        )
        # Must mention the failure in the retry message.
        assert (
            "failed validation" in retry_user_msg.lower()
            or "previous response" in retry_user_msg.lower()
        )


# ---------------------------------------------------------------------------
# Regression: missing required field
# ---------------------------------------------------------------------------


class TestInvoiceMissingField:
    def _missing_grand_total(self) -> str:
        data = json.loads(_valid_invoice_json())
        del data["grand_total"]
        return json.dumps(data)

    def test_missing_required_field_both_attempts_fails_closed(self):
        missing = self._missing_grand_total()
        client = _make_client([missing, missing])
        result = ExtractionAgent(llm_client=client).extract("invoice text")

        assert isinstance(result, ExtractionFailure)
        assert result.extraction_status == ExtractionStatus.NEEDS_REEXTRACTION
        assert result.reason in (
            FailureReason.SCHEMA_VALIDATION_FAILED,
            FailureReason.RETRY_VALIDATION_FAILED,
        )
        assert client.complete.call_count == 2

    def test_missing_field_then_valid_succeeds_on_retry(self):
        client = _make_client([self._missing_grand_total(), _valid_invoice_json()])
        result = ExtractionAgent(llm_client=client).extract("invoice text")

        assert isinstance(result, ExtractionSuccess)
        assert result.attempt_count == 2

    def test_missing_field_error_detail_mentions_field_name(self):
        missing = self._missing_grand_total()
        client = _make_client([missing, missing])
        result = ExtractionAgent(llm_client=client).extract("invoice text")

        assert isinstance(result, ExtractionFailure)
        assert "grand_total" in result.error_detail.lower()

    def test_null_grand_total_fails(self):
        with_null = _valid_invoice_json(grand_total=None)
        client = _make_client([with_null, with_null])
        result = ExtractionAgent(llm_client=client).extract("invoice text")

        assert isinstance(result, ExtractionFailure)
        assert result.extraction_status == ExtractionStatus.NEEDS_REEXTRACTION


# ---------------------------------------------------------------------------
# Regression: LLM call error
# ---------------------------------------------------------------------------


class TestInvoiceLLMCallError:
    def test_llm_call_error_fails_immediately(self):
        client = MagicMock()
        client.complete.side_effect = LLMCallError("timeout")
        result = ExtractionAgent(llm_client=client).extract("invoice text")

        assert isinstance(result, ExtractionFailure)
        assert result.reason == FailureReason.LLM_CALL_FAILED
        assert result.extraction_status == ExtractionStatus.NEEDS_REEXTRACTION
        assert client.complete.call_count == 1

    def test_llm_call_error_detail_preserved(self):
        client = MagicMock()
        client.complete.side_effect = LLMCallError("Network timeout")
        result = ExtractionAgent(llm_client=client).extract("invoice text")

        assert isinstance(result, ExtractionFailure)
        assert "Network timeout" in result.error_detail


# ---------------------------------------------------------------------------
# Regression: attempt_count tracking
# ---------------------------------------------------------------------------


class TestInvoiceAttemptCount:
    def test_first_attempt_success_count_is_1(self):
        client = _make_client([_valid_invoice_json()])
        result = ExtractionAgent(llm_client=client).extract("invoice text")
        assert isinstance(result, ExtractionSuccess)
        assert result.attempt_count == 1

    def test_second_attempt_success_count_is_2(self):
        client = _make_client(["garbage", _valid_invoice_json()])
        result = ExtractionAgent(llm_client=client).extract("invoice text")
        assert isinstance(result, ExtractionSuccess)
        assert result.attempt_count == 2

    def test_both_fail_attempt_count_is_2(self):
        client = _make_client(["garbage", "more garbage"])
        result = ExtractionAgent(llm_client=client).extract("invoice text")
        assert isinstance(result, ExtractionFailure)
        assert result.attempt_count == 2
