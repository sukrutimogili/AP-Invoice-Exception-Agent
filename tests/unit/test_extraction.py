"""
tests/unit/test_extraction.py — Phase 2 unit tests for the extraction agent.

spec.md Phase 2 testing requirement:
  "unit tests with mocked LLM responses covering: valid JSON, malformed JSON,
   JSON missing a required field, JSON wrapped in markdown fences."

All LLM calls are mocked — no network required.
The mock satisfies the LLMClient Protocol via a simple callable wrapper.

Test classes:
  TestParser            — parser.py in isolation
  TestExtractionAgent   — agent.py with mocked LLMClient
    - valid JSON on first attempt
    - JSON wrapped in markdown fences (stripped, succeeds)
    - malformed JSON (both attempts) → NEEDS_REEXTRACTION
    - missing required field (grand_total) → retry → still missing → NEEDS_REEXTRACTION
    - LLM call error → NEEDS_REEXTRACTION immediately
    - succeeds on second attempt after error feedback
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from extraction.agent import ExtractionAgent
from extraction.parser import ParseError, parse_llm_response
from extraction.schemas import ExtractionFailure, ExtractionSuccess, FailureReason
from models.enums import ExtractionStatus


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_client(responses: list[str]) -> MagicMock:
    """
    Build a mock LLMClient that returns responses in sequence.

    The mock satisfies the LLMClient Protocol because it has a `complete`
    attribute that is callable.
    """
    client = MagicMock()
    client.complete.side_effect = responses
    return client


def _valid_json_str(**overrides) -> str:
    """Return a valid invoice JSON string with optional field overrides."""
    base = {
        "invoice_number": "INV-2026-0042",
        "vendor_name": "Acme Supplies Ltd",
        "invoice_date": "2026-01-15",
        "po_reference": "PO-2025-0100",
        "contract_reference": "CTR-2025-0018",
        "subtotal": 400.00,
        "tax": 20.00,
        "grand_total": 420.00,
        "due_date": "2026-02-14",
        "payment_terms": "Net 30",
        "line_items": [
            {
                "line_number": 1,
                "description": "Widget Type A",
                "qty": 10,
                "unit_price": 38.00,
                "amount": 380.00,
            },
            {
                "line_number": 2,
                "description": "Shipping & Handling",
                "qty": 1,
                "unit_price": 20.00,
                "amount": 20.00,
            },
        ],
    }
    base.update(overrides)
    return json.dumps(base)


# ---------------------------------------------------------------------------
# Parser tests (isolation)
# ---------------------------------------------------------------------------


class TestParser:
    """Direct tests for parse_llm_response() in extraction/parser.py."""

    def test_plain_json_parsed(self):
        raw = _valid_json_str()
        result = parse_llm_response(raw)
        assert result["invoice_number"] == "INV-2026-0042"

    def test_fenced_json_stripped_and_parsed(self):
        raw = f"```json\n{_valid_json_str()}\n```"
        result = parse_llm_response(raw)
        assert result["vendor_name"] == "Acme Supplies Ltd"

    def test_fence_without_language_tag(self):
        raw = f"```\n{_valid_json_str()}\n```"
        result = parse_llm_response(raw)
        assert result["grand_total"] == 420.00

    def test_trailing_comma_repaired(self):
        broken = '{"invoice_number": "INV-001", "vendor_name": "Acme",}'
        # Should repair and parse successfully (other fields missing but that's
        # a validation concern, not a parse concern).
        result = parse_llm_response(broken)
        assert result["invoice_number"] == "INV-001"

    def test_prose_prefix_stripped(self):
        raw = f"Sure, here is the extracted JSON:\n{_valid_json_str()}"
        result = parse_llm_response(raw)
        assert result["po_reference"] == "PO-2025-0100"

    def test_empty_string_raises_parse_error(self):
        with pytest.raises(ParseError):
            parse_llm_response("")

    def test_pure_prose_raises_parse_error(self):
        with pytest.raises(ParseError):
            parse_llm_response("I cannot extract this invoice.")

    def test_array_raises_parse_error(self):
        """Top-level JSON array is rejected — we expect an object."""
        with pytest.raises(ParseError):
            parse_llm_response("[1, 2, 3]")


# ---------------------------------------------------------------------------
# Agent unit tests (mocked LLM)
# ---------------------------------------------------------------------------


class TestExtractionAgentValidJSON:
    """Agent receives valid JSON on the first attempt."""

    def test_succeeds_on_first_attempt(self):
        client = _make_client([_valid_json_str()])
        agent = ExtractionAgent(llm_client=client)
        result = agent.extract("some invoice text")

        assert isinstance(result, ExtractionSuccess)
        assert result.outcome == "success"
        assert result.attempt_count == 1
        assert result.extraction_status == ExtractionStatus.EXTRACTED
        assert result.invoice.invoice_number == "INV-2026-0042"
        assert result.invoice.vendor_name == "Acme Supplies Ltd"
        # LLM called exactly once
        assert client.complete.call_count == 1

    def test_success_result_carries_raw_payload(self):
        raw = _valid_json_str()
        client = _make_client([raw])
        agent = ExtractionAgent(llm_client=client)
        result = agent.extract("invoice")

        assert isinstance(result, ExtractionSuccess)
        assert result.raw_payload == raw

    def test_invoice_fields_correct(self):
        client = _make_client([_valid_json_str()])
        agent = ExtractionAgent(llm_client=client)
        result = agent.extract("invoice")

        assert isinstance(result, ExtractionSuccess)
        inv = result.invoice
        from decimal import Decimal
        assert inv.grand_total == Decimal("420.00")
        assert inv.subtotal == Decimal("400.00")
        assert len(inv.line_items) == 2


class TestExtractionAgentFencedJSON:
    """Agent receives JSON wrapped in markdown fences."""

    def test_fenced_json_extracted_and_validated(self):
        fenced = f"```json\n{_valid_json_str()}\n```"
        client = _make_client([fenced])
        agent = ExtractionAgent(llm_client=client)
        result = agent.extract("invoice")

        assert isinstance(result, ExtractionSuccess)
        assert result.attempt_count == 1
        assert result.invoice.invoice_number == "INV-2026-0042"

    def test_fence_without_language_tag(self):
        fenced = f"```\n{_valid_json_str()}\n```"
        client = _make_client([fenced])
        agent = ExtractionAgent(llm_client=client)
        result = agent.extract("invoice")

        assert isinstance(result, ExtractionSuccess)

    def test_prose_prefix_then_json(self):
        """LLM adds explanation text before the JSON despite being told not to."""
        with_prose = f"Here is the extracted data:\n{_valid_json_str()}"
        client = _make_client([with_prose])
        agent = ExtractionAgent(llm_client=client)
        result = agent.extract("invoice")

        assert isinstance(result, ExtractionSuccess)


class TestExtractionAgentMalformedJSON:
    """Agent receives unparseable JSON on both attempts → NEEDS_REEXTRACTION."""

    def test_malformed_both_attempts_fails_closed(self):
        client = _make_client([
            "This is not JSON at all!!!",
            "Still not JSON { broken",
        ])
        agent = ExtractionAgent(llm_client=client)
        result = agent.extract("invoice")

        assert isinstance(result, ExtractionFailure)
        assert result.outcome == "failure"
        assert result.extraction_status == ExtractionStatus.NEEDS_REEXTRACTION
        assert result.reason == FailureReason.UNPARSEABLE_JSON
        # Both attempts made
        assert client.complete.call_count == 2

    def test_malformed_first_valid_second_succeeds(self):
        """First attempt malformed, second succeeds — attempt_count == 2."""
        client = _make_client([
            "garbage output",
            _valid_json_str(),
        ])
        agent = ExtractionAgent(llm_client=client)
        result = agent.extract("invoice")

        assert isinstance(result, ExtractionSuccess)
        assert result.attempt_count == 2
        assert client.complete.call_count == 2

    def test_retry_message_contains_error_feedback(self):
        """The retry call's user message must contain the validation error."""
        client = _make_client([
            "not json",
            _valid_json_str(),
        ])
        agent = ExtractionAgent(llm_client=client)
        agent.extract("MY INVOICE TEXT")

        retry_call = client.complete.call_args_list[1]
        # complete() is called with keyword args: complete(system_prompt=..., user_message=...)
        retry_user_msg = (
            retry_call.kwargs.get("user_message")
            or (retry_call.args[1] if len(retry_call.args) > 1 else "")
        )
        assert "failed validation" in retry_user_msg.lower() or "previous response" in retry_user_msg.lower()


class TestExtractionAgentMissingField:
    """Agent receives JSON missing a required field (grand_total absent)."""

    def _missing_grand_total(self) -> str:
        data = json.loads(_valid_json_str())
        del data["grand_total"]
        return json.dumps(data)

    def test_missing_required_field_triggers_retry(self):
        """First attempt missing grand_total → retry → still missing → NEEDS_REEXTRACTION."""
        missing = self._missing_grand_total()
        client = _make_client([missing, missing])
        agent = ExtractionAgent(llm_client=client)
        result = agent.extract("invoice text")

        assert isinstance(result, ExtractionFailure)
        assert result.extraction_status == ExtractionStatus.NEEDS_REEXTRACTION
        assert result.reason in (
            FailureReason.SCHEMA_VALIDATION_FAILED,
            FailureReason.RETRY_VALIDATION_FAILED,
        )
        assert client.complete.call_count == 2

    def test_missing_field_then_valid_succeeds_on_retry(self):
        """First attempt missing grand_total, second attempt provides it."""
        client = _make_client([self._missing_grand_total(), _valid_json_str()])
        agent = ExtractionAgent(llm_client=client)
        result = agent.extract("invoice text")

        assert isinstance(result, ExtractionSuccess)
        assert result.attempt_count == 2

    def test_missing_field_error_detail_mentions_field(self):
        """The failure's error_detail should reference the missing field."""
        missing = self._missing_grand_total()
        client = _make_client([missing, missing])
        agent = ExtractionAgent(llm_client=client)
        result = agent.extract("invoice text")

        assert isinstance(result, ExtractionFailure)
        # error_detail should mention grand_total somewhere
        assert "grand_total" in result.error_detail.lower()

    def test_null_grand_total_fails(self):
        """grand_total: null in JSON — field present but null → validation fails."""
        with_null = _valid_json_str(grand_total=None)
        client = _make_client([with_null, with_null])
        agent = ExtractionAgent(llm_client=client)
        result = agent.extract("invoice")

        assert isinstance(result, ExtractionFailure)
        assert result.extraction_status == ExtractionStatus.NEEDS_REEXTRACTION


class TestExtractionAgentLLMCallError:
    """Agent fails immediately when LLM call itself errors."""

    def test_llm_call_error_returns_failure(self):
        from extraction.llm_client import LLMCallError
        client = MagicMock()
        client.complete.side_effect = LLMCallError("Network timeout")
        agent = ExtractionAgent(llm_client=client)
        result = agent.extract("invoice")

        assert isinstance(result, ExtractionFailure)
        assert result.reason == FailureReason.LLM_CALL_FAILED
        assert result.extraction_status == ExtractionStatus.NEEDS_REEXTRACTION
        # Only one call attempted when LLM errors
        assert client.complete.call_count == 1



# ---------------------------------------------------------------------------
# OpenRouterClient fallback chain unit tests
# ---------------------------------------------------------------------------


class TestOpenRouterClientFallbackChain:
    """
    Unit tests for the 429-retry and model-fallback logic in OpenRouterClient.

    httpx is patched so no real network calls are made.
    """

    def _make_settings(self) -> object:
        """Return a minimal settings-like object with required attributes."""
        from unittest.mock import MagicMock
        s = MagicMock()
        s.openrouter_api_key = "sk-or-v1-test"
        s.openrouter_base_url = "https://openrouter.ai/api/v1"
        s.openrouter_fallback_chain = [
            "model-a",
            "model-b",
            "model-c",
        ]
        return s

    def _ok_response(self, content: str = "PONG") -> object:
        from unittest.mock import MagicMock
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {
            "choices": [{"message": {"content": content}}]
        }
        return r

    def _rate_limited_response(self, retry_after: int = 1) -> object:
        from unittest.mock import MagicMock
        r = MagicMock()
        r.status_code = 429
        r.text = "rate limited"
        r.headers = {"retry-after": str(retry_after)}
        r.json.return_value = {
            "error": {
                "metadata": {
                    "retry_after_seconds": retry_after,
                }
            }
        }
        return r

    def test_first_model_succeeds_no_fallback(self):
        """Happy path: first model returns 200 immediately."""
        from unittest.mock import MagicMock, patch
        from extraction.llm_client import OpenRouterClient

        settings = self._make_settings()
        client = OpenRouterClient(settings=settings)

        with patch("httpx.post", return_value=self._ok_response("PONG")) as mock_post:
            result = client.complete("sys", "user")

        assert result == "PONG"
        # Only one HTTP call made.
        assert mock_post.call_count == 1
        # Correct model used.
        body = mock_post.call_args.kwargs["json"]
        assert body["model"] == "model-a"

    def test_first_call_429_retried_then_succeeds(self):
        """429 on first call → sleep Retry-After → retry same model → 200."""
        from unittest.mock import MagicMock, patch, call
        from extraction.llm_client import OpenRouterClient

        settings = self._make_settings()
        client = OpenRouterClient(settings=settings)

        responses = [self._rate_limited_response(retry_after=1), self._ok_response("RETRY_OK")]

        with patch("httpx.post", side_effect=responses) as mock_post, \
             patch("time.sleep") as mock_sleep:
            result = client.complete("sys", "user")

        assert result == "RETRY_OK"
        assert mock_post.call_count == 2
        # Both calls used model-a (same model retried, not fallback).
        for c in mock_post.call_args_list:
            assert c.kwargs["json"]["model"] == "model-a"
        # Slept for the retry-after value.
        mock_sleep.assert_called_once_with(1.0)

    def test_first_model_429_twice_falls_back_to_second(self):
        """429 on attempt + retry for model-a → falls through to model-b which succeeds."""
        from unittest.mock import patch
        from extraction.llm_client import OpenRouterClient

        settings = self._make_settings()
        client = OpenRouterClient(settings=settings)

        responses = [
            self._rate_limited_response(retry_after=1),  # model-a attempt 1
            self._rate_limited_response(retry_after=1),  # model-a retry
            self._ok_response("MODEL_B_OK"),              # model-b attempt 1
        ]

        with patch("httpx.post", side_effect=responses) as mock_post, \
             patch("time.sleep"):
            result = client.complete("sys", "user")

        assert result == "MODEL_B_OK"
        assert mock_post.call_count == 3
        calls = mock_post.call_args_list
        assert calls[0].kwargs["json"]["model"] == "model-a"
        assert calls[1].kwargs["json"]["model"] == "model-a"
        assert calls[2].kwargs["json"]["model"] == "model-b"

    def test_all_models_429_raises_llm_call_error(self):
        """Every model in the chain rate-limits → LLMCallError(status_code=429)."""
        from unittest.mock import patch
        from extraction.llm_client import OpenRouterClient, LLMCallError

        settings = self._make_settings()
        client = OpenRouterClient(settings=settings)

        # 2 responses per model (attempt + retry) × 3 models = 6 total
        responses = [self._rate_limited_response(1)] * 6

        with patch("httpx.post", side_effect=responses), \
             patch("time.sleep"):
            with pytest.raises(LLMCallError) as exc_info:
                client.complete("sys", "user")

        assert exc_info.value.status_code == 429

    def test_non_429_error_propagates_immediately(self):
        """HTTP 500 on model-a → raises immediately, model-b never tried."""
        from unittest.mock import MagicMock, patch
        from extraction.llm_client import OpenRouterClient, LLMCallError

        settings = self._make_settings()
        client = OpenRouterClient(settings=settings)

        bad = MagicMock()
        bad.status_code = 500
        bad.text = "internal server error"
        bad.json.side_effect = ValueError("not json")

        with patch("httpx.post", return_value=bad) as mock_post:
            with pytest.raises(LLMCallError) as exc_info:
                client.complete("sys", "user")

        assert exc_info.value.status_code == 500
        # Only one call — did not fall through to model-b.
        assert mock_post.call_count == 1

    def test_fallback_chain_uses_settings_chain(self):
        """Fallback chain is taken from settings, not hardcoded."""
        from unittest.mock import patch
        from extraction.llm_client import OpenRouterClient

        settings = self._make_settings()
        settings.openrouter_fallback_chain = ["custom-model-x", "custom-model-y"]
        client = OpenRouterClient(settings=settings)

        assert client._fallback_chain == ["custom-model-x", "custom-model-y"]

    def test_retry_after_capped_at_max(self):
        """Retry-After values larger than _MAX_RETRY_AFTER_SECONDS are clamped."""
        from unittest.mock import MagicMock, patch
        from extraction.llm_client import OpenRouterClient, _MAX_RETRY_AFTER_SECONDS

        settings = self._make_settings()
        client = OpenRouterClient(settings=settings)

        huge_retry = MagicMock()
        huge_retry.status_code = 429
        huge_retry.text = "rate limited"
        huge_retry.headers = {"retry-after": "9999"}
        huge_retry.json.return_value = {
            "error": {"metadata": {"retry_after_seconds": 9999}}
        }

        with patch("httpx.post", side_effect=[huge_retry, self._ok_response()]), \
             patch("time.sleep") as mock_sleep:
            client.complete("sys", "user")

        mock_sleep.assert_called_once_with(_MAX_RETRY_AFTER_SECONDS)
