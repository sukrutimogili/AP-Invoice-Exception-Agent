"""
tests/unit/test_contract_extraction.py — Unit tests for ContractExtractionAgent.

All LLM calls are mocked — no network required.

Coverage:
  - Valid contract JSON → ContractExtractionSuccess with correct fields
  - JSON wrapped in markdown fences → still succeeds
  - Malformed JSON on both attempts → ContractExtractionFailure (NEEDS_REEXTRACTION)
  - Missing required field (contract_reference) on both attempts → NEEDS_REEXTRACTION
  - LLM call error → immediate failure
  - Malformed first / valid second → succeeds on attempt 2
  - vendor_code_extracted carried through correctly
  - discount_term_raw surfaced verbatim on success (not parsed)
  - discount_term on ContractCreate is always None at extraction time
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from extraction.agent import ContractExtractionAgent
from extraction.contract_schemas import (
    ContractExtractionFailure,
    ContractExtractionSuccess,
)
from extraction.llm_client import LLMCallError
from extraction.schemas import FailureReason
from models.enums import ExtractionStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(responses: list[str]) -> MagicMock:
    client = MagicMock()
    client.complete.side_effect = responses
    return client


def _valid_contract_json(**overrides) -> str:
    base = {
        "contract_reference": "CTR-2026-ACME-007",
        "vendor_code": "ACME-001",
        "discount_term_raw": "2/10 net 30",
        "approval_threshold": 50000.00,
        "notes": "Annual framework agreement",
        "line_items": [
            {
                "line_number": 1,
                "description": "Enterprise Software License",
                "unit_price": 9999.00,
            }
        ],
    }
    base.update(overrides)
    return json.dumps(base)


def _valid_contract_json_no_discount(**overrides) -> str:
    """Contract without a discount term."""
    base = {
        "contract_reference": "CTR-2026-NO-DISC",
        "vendor_code": "VENDOR-X",
        "discount_term_raw": None,
        "approval_threshold": None,
        "notes": None,
        "line_items": [
            {
                "line_number": 1,
                "description": "Consulting Services",
                "unit_price": 200.00,
            }
        ],
    }
    base.update(overrides)
    return json.dumps(base)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestContractExtractionHappyPath:
    def test_valid_contract_json_succeeds_on_first_attempt(self):
        client = _make_client([_valid_contract_json()])
        agent = ContractExtractionAgent(llm_client=client)
        result = agent.extract("raw contract document text")

        assert isinstance(result, ContractExtractionSuccess)
        assert result.outcome == "success"
        assert result.attempt_count == 1
        assert result.extraction_status == ExtractionStatus.EXTRACTED
        assert client.complete.call_count == 1

    def test_contract_fields_populated_correctly(self):
        client = _make_client([_valid_contract_json()])
        result = ContractExtractionAgent(llm_client=client).extract("contract text")

        assert isinstance(result, ContractExtractionSuccess)
        contract = result.contract
        assert contract.contract_reference == "CTR-2026-ACME-007"
        assert len(contract.line_items) == 1
        assert contract.line_items[0].description == "Enterprise Software License"
        assert contract.line_items[0].unit_price == Decimal("9999.00")

    def test_vendor_code_extracted_field_set(self):
        client = _make_client([_valid_contract_json()])
        result = ContractExtractionAgent(llm_client=client).extract("contract text")

        assert isinstance(result, ContractExtractionSuccess)
        assert result.vendor_code_extracted == "ACME-001"

    def test_vendor_id_matches_extracted_vendor_code(self):
        """contract.vendor_id is initially set to the extracted vendor_code."""
        client = _make_client([_valid_contract_json()])
        result = ContractExtractionAgent(llm_client=client).extract("contract text")

        assert isinstance(result, ContractExtractionSuccess)
        assert result.contract.vendor_id == "ACME-001"

    def test_discount_term_raw_surfaced_verbatim(self):
        """discount_term_raw is carried in the success result for downstream parsing."""
        client = _make_client([_valid_contract_json()])
        result = ContractExtractionAgent(llm_client=client).extract("contract text")

        assert isinstance(result, ContractExtractionSuccess)
        assert result.discount_term_raw == "2/10 net 30"

    def test_discount_term_on_contract_model_is_none(self):
        """contract.discount_term must be None — parsing is deferred to caller."""
        client = _make_client([_valid_contract_json()])
        result = ContractExtractionAgent(llm_client=client).extract("contract text")

        assert isinstance(result, ContractExtractionSuccess)
        assert result.contract.discount_term is None

    def test_no_discount_term_raw_is_none(self):
        """When contract has no discount term, discount_term_raw is None."""
        client = _make_client([_valid_contract_json_no_discount()])
        result = ContractExtractionAgent(llm_client=client).extract("contract text")

        assert isinstance(result, ContractExtractionSuccess)
        assert result.discount_term_raw is None

    def test_raw_payload_preserved_verbatim(self):
        raw = _valid_contract_json()
        client = _make_client([raw])
        result = ContractExtractionAgent(llm_client=client).extract("contract text")

        assert isinstance(result, ContractExtractionSuccess)
        assert result.raw_payload == raw

    def test_fenced_json_accepted(self):
        fenced = f"```json\n{_valid_contract_json()}\n```"
        client = _make_client([fenced])
        result = ContractExtractionAgent(llm_client=client).extract("contract text")

        assert isinstance(result, ContractExtractionSuccess)
        assert result.attempt_count == 1

    def test_fence_without_language_tag(self):
        fenced = f"```\n{_valid_contract_json()}\n```"
        client = _make_client([fenced])
        result = ContractExtractionAgent(llm_client=client).extract("contract text")

        assert isinstance(result, ContractExtractionSuccess)

    def test_prose_prefix_handled(self):
        with_prose = f"Here is the extracted contract:\n{_valid_contract_json()}"
        client = _make_client([with_prose])
        result = ContractExtractionAgent(llm_client=client).extract("contract text")

        assert isinstance(result, ContractExtractionSuccess)

    def test_notes_nullable(self):
        raw = _valid_contract_json(notes=None)
        client = _make_client([raw])
        result = ContractExtractionAgent(llm_client=client).extract("contract text")

        assert isinstance(result, ContractExtractionSuccess)
        assert result.contract.notes is None

    def test_approval_threshold_nullable(self):
        """approval_threshold is optional on contracts."""
        raw = _valid_contract_json(approval_threshold=None)
        client = _make_client([raw])
        result = ContractExtractionAgent(llm_client=client).extract("contract text")

        assert isinstance(result, ContractExtractionSuccess)
        assert result.contract.approval_threshold is None

    def test_various_discount_term_formats(self):
        """discount_term_raw must be preserved verbatim, not interpreted."""
        for term in ["1.5/15 net 45", "2% 10 days net 30", "0.5/10 net 15", "1/15 net 60"]:
            raw = _valid_contract_json(discount_term_raw=term)
            client = _make_client([raw])
            result = ContractExtractionAgent(llm_client=client).extract("contract text")

            assert isinstance(result, ContractExtractionSuccess)
            assert result.discount_term_raw == term


# ---------------------------------------------------------------------------
# Malformed JSON
# ---------------------------------------------------------------------------


class TestContractExtractionMalformedJSON:
    def test_both_attempts_malformed_fails_closed(self):
        client = _make_client(["not json!", "still broken {"])
        result = ContractExtractionAgent(llm_client=client).extract("contract text")

        assert isinstance(result, ContractExtractionFailure)
        assert result.outcome == "failure"
        assert result.extraction_status == ExtractionStatus.NEEDS_REEXTRACTION
        assert result.reason == FailureReason.UNPARSEABLE_JSON
        assert client.complete.call_count == 2

    def test_malformed_first_valid_second_succeeds_attempt2(self):
        client = _make_client(["garbage", _valid_contract_json()])
        result = ContractExtractionAgent(llm_client=client).extract("contract text")

        assert isinstance(result, ContractExtractionSuccess)
        assert result.attempt_count == 2
        assert client.complete.call_count == 2

    def test_retry_message_references_previous_failure(self):
        client = _make_client(["not json", _valid_contract_json()])
        ContractExtractionAgent(llm_client=client).extract("ORIGINAL CONTRACT TEXT")

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


class TestContractExtractionMissingField:
    def _missing_contract_reference(self) -> str:
        data = json.loads(_valid_contract_json())
        del data["contract_reference"]
        return json.dumps(data)

    def test_missing_contract_reference_both_attempts_fails_closed(self):
        missing = self._missing_contract_reference()
        client = _make_client([missing, missing])
        result = ContractExtractionAgent(llm_client=client).extract("contract text")

        assert isinstance(result, ContractExtractionFailure)
        assert result.extraction_status == ExtractionStatus.NEEDS_REEXTRACTION
        assert result.reason in (
            FailureReason.SCHEMA_VALIDATION_FAILED,
            FailureReason.RETRY_VALIDATION_FAILED,
        )
        assert client.complete.call_count == 2

    def test_missing_contract_reference_error_detail_mentions_field(self):
        missing = self._missing_contract_reference()
        client = _make_client([missing, missing])
        result = ContractExtractionAgent(llm_client=client).extract("contract text")

        assert isinstance(result, ContractExtractionFailure)
        assert "contract_reference" in result.error_detail.lower()

    def test_missing_field_then_valid_succeeds_on_retry(self):
        client = _make_client([self._missing_contract_reference(), _valid_contract_json()])
        result = ContractExtractionAgent(llm_client=client).extract("contract text")

        assert isinstance(result, ContractExtractionSuccess)
        assert result.attempt_count == 2

    def test_missing_vendor_code_fails(self):
        """vendor_code is required to populate vendor_id."""
        data = json.loads(_valid_contract_json())
        del data["vendor_code"]
        raw = json.dumps(data)
        client = _make_client([raw, raw])
        result = ContractExtractionAgent(llm_client=client).extract("contract text")

        assert isinstance(result, ContractExtractionFailure)

    def test_missing_line_items_fails(self):
        """ContractCreate requires at least one line item."""
        data = json.loads(_valid_contract_json())
        data["line_items"] = []
        raw = json.dumps(data)
        client = _make_client([raw, raw])
        result = ContractExtractionAgent(llm_client=client).extract("contract text")

        assert isinstance(result, ContractExtractionFailure)


# ---------------------------------------------------------------------------
# LLM call error
# ---------------------------------------------------------------------------


class TestContractExtractionLLMCallError:
    def test_llm_call_error_fails_immediately(self):
        client = MagicMock()
        client.complete.side_effect = LLMCallError("timeout")
        result = ContractExtractionAgent(llm_client=client).extract("contract text")

        assert isinstance(result, ContractExtractionFailure)
        assert result.reason == FailureReason.LLM_CALL_FAILED
        assert result.extraction_status == ExtractionStatus.NEEDS_REEXTRACTION
        assert client.complete.call_count == 1

    def test_llm_call_error_detail_preserved(self):
        client = MagicMock()
        client.complete.side_effect = LLMCallError("HTTP 503")
        result = ContractExtractionAgent(llm_client=client).extract("contract text")

        assert isinstance(result, ContractExtractionFailure)
        assert "HTTP 503" in result.error_detail


# ---------------------------------------------------------------------------
# Attempt count tracking
# ---------------------------------------------------------------------------


class TestContractAttemptCount:
    def test_first_attempt_success_count_is_1(self):
        client = _make_client([_valid_contract_json()])
        result = ContractExtractionAgent(llm_client=client).extract("contract text")
        assert isinstance(result, ContractExtractionSuccess)
        assert result.attempt_count == 1

    def test_second_attempt_success_count_is_2(self):
        client = _make_client(["bad json", _valid_contract_json()])
        result = ContractExtractionAgent(llm_client=client).extract("contract text")
        assert isinstance(result, ContractExtractionSuccess)
        assert result.attempt_count == 2

    def test_both_fail_attempt_count_is_2(self):
        client = _make_client(["bad", "also bad"])
        result = ContractExtractionAgent(llm_client=client).extract("contract text")
        assert isinstance(result, ContractExtractionFailure)
        assert result.attempt_count == 2


# ---------------------------------------------------------------------------
# Discount term raw — isolation
# ---------------------------------------------------------------------------


class TestContractDiscountTermRaw:
    """
    discount_term_raw must be passed through verbatim and kept out of the
    ContractCreate model (which reserves that work for discount.parser).
    """

    def test_discount_term_raw_not_parsed_into_model(self):
        """contract.discount_term is always None; parsing is deferred."""
        for term in ["2/10 net 30", "1.5/15 net 45"]:
            raw = _valid_contract_json(discount_term_raw=term)
            client = _make_client([raw])
            result = ContractExtractionAgent(llm_client=client).extract("contract text")

            assert isinstance(result, ContractExtractionSuccess)
            assert result.contract.discount_term is None

    def test_discount_term_raw_null_yields_none_on_success(self):
        raw = _valid_contract_json(discount_term_raw=None)
        client = _make_client([raw])
        result = ContractExtractionAgent(llm_client=client).extract("contract text")

        assert isinstance(result, ContractExtractionSuccess)
        assert result.discount_term_raw is None

    def test_discount_term_raw_surfaced_on_retry_success(self):
        """discount_term_raw is preserved even when extracted on attempt 2."""
        client = _make_client(["garbage", _valid_contract_json(discount_term_raw="1/15 net 60")])
        result = ContractExtractionAgent(llm_client=client).extract("contract text")

        assert isinstance(result, ContractExtractionSuccess)
        assert result.attempt_count == 2
        assert result.discount_term_raw == "1/15 net 60"
