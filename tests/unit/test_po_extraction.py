"""
tests/unit/test_po_extraction.py — Unit tests for PurchaseOrderExtractionAgent.

All LLM calls are mocked — no network required.

Coverage:
  - Valid PO JSON → POExtractionSuccess with correct fields
  - JSON wrapped in markdown fences → still succeeds
  - Malformed JSON on both attempts → POExtractionFailure (NEEDS_REEXTRACTION)
  - Missing required field (po_number) on both attempts → NEEDS_REEXTRACTION
  - LLM call error → immediate failure
  - Malformed first / valid second → succeeds on attempt 2
  - vendor_code_extracted carried through correctly
  - discount_term_raw NOT expected on PO (absent from success type)
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from extraction.agent import PurchaseOrderExtractionAgent
from extraction.llm_client import LLMCallError
from extraction.po_schemas import POExtractionFailure, POExtractionSuccess
from extraction.schemas import FailureReason
from models.enums import ExtractionStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(responses: list[str]) -> MagicMock:
    client = MagicMock()
    client.complete.side_effect = responses
    return client


def _valid_po_json(**overrides) -> str:
    base = {
        "po_number": "PO-2026-TEST-001",
        "vendor_code": "ACME-001",
        "po_total": 12500.00,
        "approval_threshold": 15000.00,
        "notes": "Quarterly supply order",
        "line_items": [
            {
                "line_number": 1,
                "description": "Widget Type B",
                "qty": 100,
                "unit_price": 125.00,
            }
        ],
    }
    base.update(overrides)
    return json.dumps(base)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestPOExtractionHappyPath:
    def test_valid_po_json_succeeds_on_first_attempt(self):
        client = _make_client([_valid_po_json()])
        agent = PurchaseOrderExtractionAgent(llm_client=client)
        result = agent.extract("raw PO document text")

        assert isinstance(result, POExtractionSuccess)
        assert result.outcome == "success"
        assert result.attempt_count == 1
        assert result.extraction_status == ExtractionStatus.EXTRACTED
        assert client.complete.call_count == 1

    def test_po_fields_populated_correctly(self):
        client = _make_client([_valid_po_json()])
        result = PurchaseOrderExtractionAgent(llm_client=client).extract("po text")

        assert isinstance(result, POExtractionSuccess)
        po = result.po
        assert po.po_number == "PO-2026-TEST-001"
        assert po.po_total == Decimal("12500.00")
        assert po.approval_threshold == Decimal("15000.00")
        assert len(po.line_items) == 1
        assert po.line_items[0].description == "Widget Type B"

    def test_vendor_code_extracted_field_set(self):
        client = _make_client([_valid_po_json()])
        result = PurchaseOrderExtractionAgent(llm_client=client).extract("po text")

        assert isinstance(result, POExtractionSuccess)
        assert result.vendor_code_extracted == "ACME-001"

    def test_vendor_id_matches_extracted_vendor_code(self):
        """po.vendor_id is initially set to the extracted vendor_code."""
        client = _make_client([_valid_po_json()])
        result = PurchaseOrderExtractionAgent(llm_client=client).extract("po text")

        assert isinstance(result, POExtractionSuccess)
        assert result.po.vendor_id == "ACME-001"

    def test_raw_payload_preserved_verbatim(self):
        raw = _valid_po_json()
        client = _make_client([raw])
        result = PurchaseOrderExtractionAgent(llm_client=client).extract("po text")

        assert isinstance(result, POExtractionSuccess)
        assert result.raw_payload == raw

    def test_fenced_json_accepted(self):
        fenced = f"```json\n{_valid_po_json()}\n```"
        client = _make_client([fenced])
        result = PurchaseOrderExtractionAgent(llm_client=client).extract("po text")

        assert isinstance(result, POExtractionSuccess)
        assert result.attempt_count == 1

    def test_fence_without_language_tag(self):
        fenced = f"```\n{_valid_po_json()}\n```"
        client = _make_client([fenced])
        result = PurchaseOrderExtractionAgent(llm_client=client).extract("po text")

        assert isinstance(result, POExtractionSuccess)

    def test_prose_prefix_handled(self):
        with_prose = f"Here is the extracted PO JSON:\n{_valid_po_json()}"
        client = _make_client([with_prose])
        result = PurchaseOrderExtractionAgent(llm_client=client).extract("po text")

        assert isinstance(result, POExtractionSuccess)

    def test_po_notes_nullable(self):
        """notes field is allowed to be null."""
        raw = _valid_po_json(notes=None)
        client = _make_client([raw])
        result = PurchaseOrderExtractionAgent(llm_client=client).extract("po text")

        assert isinstance(result, POExtractionSuccess)
        assert result.po.notes is None

    def test_approval_threshold_nullable(self):
        """approval_threshold being null should trigger retry/fail behavior
        because PurchaseOrderCreate requires approval_threshold > 0."""
        raw = _valid_po_json(approval_threshold=None)
        client = _make_client([raw, raw])
        result = PurchaseOrderExtractionAgent(llm_client=client).extract("po text")

        # approval_threshold is required and must be > 0 — None should fail.
        assert isinstance(result, POExtractionFailure)
        assert result.extraction_status == ExtractionStatus.NEEDS_REEXTRACTION


# ---------------------------------------------------------------------------
# Malformed JSON
# ---------------------------------------------------------------------------


class TestPOExtractionMalformedJSON:
    def test_both_attempts_malformed_fails_closed(self):
        client = _make_client(["not json!", "still broken {"])
        result = PurchaseOrderExtractionAgent(llm_client=client).extract("po text")

        assert isinstance(result, POExtractionFailure)
        assert result.outcome == "failure"
        assert result.extraction_status == ExtractionStatus.NEEDS_REEXTRACTION
        assert result.reason == FailureReason.UNPARSEABLE_JSON
        assert client.complete.call_count == 2

    def test_malformed_first_valid_second_succeeds_attempt2(self):
        client = _make_client(["garbage", _valid_po_json()])
        result = PurchaseOrderExtractionAgent(llm_client=client).extract("po text")

        assert isinstance(result, POExtractionSuccess)
        assert result.attempt_count == 2
        assert client.complete.call_count == 2

    def test_retry_message_references_previous_failure(self):
        client = _make_client(["not json", _valid_po_json()])
        PurchaseOrderExtractionAgent(llm_client=client).extract("ORIGINAL PO TEXT")

        retry_call = client.complete.call_args_list[1]
        retry_user_msg = (
            retry_call.kwargs.get("user_message")
            or (retry_call.args[1] if len(retry_call.args) > 1 else "")
        )
        assert (
            "failed validation" in retry_user_msg.lower()
            or "previous response" in retry_user_msg.lower()
        )


# ---------------------------------------------------------------------------
# Missing required field
# ---------------------------------------------------------------------------


class TestPOExtractionMissingField:
    def _missing_po_number(self) -> str:
        data = json.loads(_valid_po_json())
        del data["po_number"]
        return json.dumps(data)

    def test_missing_po_number_both_attempts_fails_closed(self):
        missing = self._missing_po_number()
        client = _make_client([missing, missing])
        result = PurchaseOrderExtractionAgent(llm_client=client).extract("po text")

        assert isinstance(result, POExtractionFailure)
        assert result.extraction_status == ExtractionStatus.NEEDS_REEXTRACTION
        assert result.reason in (
            FailureReason.SCHEMA_VALIDATION_FAILED,
            FailureReason.RETRY_VALIDATION_FAILED,
        )
        assert client.complete.call_count == 2

    def test_missing_po_number_error_detail_mentions_field(self):
        missing = self._missing_po_number()
        client = _make_client([missing, missing])
        result = PurchaseOrderExtractionAgent(llm_client=client).extract("po text")

        assert isinstance(result, POExtractionFailure)
        assert "po_number" in result.error_detail.lower()

    def test_missing_field_then_valid_succeeds_on_retry(self):
        client = _make_client([self._missing_po_number(), _valid_po_json()])
        result = PurchaseOrderExtractionAgent(llm_client=client).extract("po text")

        assert isinstance(result, POExtractionSuccess)
        assert result.attempt_count == 2

    def test_missing_vendor_code_fails(self):
        """vendor_code is required to populate vendor_id."""
        data = json.loads(_valid_po_json())
        del data["vendor_code"]
        raw = json.dumps(data)
        client = _make_client([raw, raw])
        result = PurchaseOrderExtractionAgent(llm_client=client).extract("po text")

        assert isinstance(result, POExtractionFailure)


# ---------------------------------------------------------------------------
# LLM call error
# ---------------------------------------------------------------------------


class TestPOExtractionLLMCallError:
    def test_llm_call_error_fails_immediately(self):
        client = MagicMock()
        client.complete.side_effect = LLMCallError("connection refused")
        result = PurchaseOrderExtractionAgent(llm_client=client).extract("po text")

        assert isinstance(result, POExtractionFailure)
        assert result.reason == FailureReason.LLM_CALL_FAILED
        assert result.extraction_status == ExtractionStatus.NEEDS_REEXTRACTION
        assert client.complete.call_count == 1

    def test_llm_call_error_detail_preserved(self):
        client = MagicMock()
        client.complete.side_effect = LLMCallError("rate limited")
        result = PurchaseOrderExtractionAgent(llm_client=client).extract("po text")

        assert isinstance(result, POExtractionFailure)
        assert "rate limited" in result.error_detail


# ---------------------------------------------------------------------------
# Attempt count tracking
# ---------------------------------------------------------------------------


class TestPOAttemptCount:
    def test_first_attempt_success_count_is_1(self):
        client = _make_client([_valid_po_json()])
        result = PurchaseOrderExtractionAgent(llm_client=client).extract("po text")
        assert isinstance(result, POExtractionSuccess)
        assert result.attempt_count == 1

    def test_second_attempt_success_count_is_2(self):
        client = _make_client(["bad json", _valid_po_json()])
        result = PurchaseOrderExtractionAgent(llm_client=client).extract("po text")
        assert isinstance(result, POExtractionSuccess)
        assert result.attempt_count == 2

    def test_both_fail_attempt_count_is_2(self):
        client = _make_client(["bad", "also bad"])
        result = PurchaseOrderExtractionAgent(llm_client=client).extract("po text")
        assert isinstance(result, POExtractionFailure)
        assert result.attempt_count == 2
