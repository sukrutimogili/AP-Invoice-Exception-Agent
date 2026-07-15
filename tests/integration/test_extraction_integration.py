"""
tests/integration/test_extraction_integration.py — Phase 2 integration tests.

spec.md Phase 2 testing requirement:
  "golden dataset run against the real configured OpenRouter model (integration
   test, not mocked) for at least the clean and malformed scenarios."

These tests call the REAL OpenRouter API using OPENROUTER_API_KEY from .env.
They are marked with @pytest.mark.integration so they can be run separately:
    pytest -m integration

They are NOT run as part of the default unit-test suite (pytest without -m).

Scenarios covered:
  - Scenario 1 (clean invoice): expects ExtractionSuccess with correct fields.
  - Scenario 4 (malformed — grand_total absent): expects ExtractionFailure /
    NEEDS_REEXTRACTION. The system must NEVER fabricate the grand_total.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import get_settings
from extraction.agent import ExtractionAgent
from extraction.llm_client import OpenRouterClient
from extraction.schemas import ExtractionFailure, ExtractionSuccess
from models.enums import ExtractionStatus

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

GOLDEN_DIR = Path(__file__).parent.parent / "golden"


def _load_fixture(filename: str) -> dict:
    return json.loads((GOLDEN_DIR / filename).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def agent() -> ExtractionAgent:
    """Real ExtractionAgent wired to OpenRouter via settings."""
    settings = get_settings()
    client = OpenRouterClient(settings=settings)
    return ExtractionAgent(llm_client=client)


def _extract_with_retry(agent: ExtractionAgent, invoice_text: str, max_attempts: int = 3):
    """
    Thin wrapper kept for test readability.

    Retry logic (429 handling + Retry-After sleep + model fallback chain) is
    now inside OpenRouterClient.complete() itself, so this wrapper simply calls
    agent.extract() once.  The max_attempts parameter is retained for
    compatibility but is no longer used.
    """
    return agent.extract(invoice_text)


# ---------------------------------------------------------------------------
# Scenario 1 — Clean invoice (requirements.md §8 scenario 1)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestScenario01CleanInvoice:
    """
    Given a fully-formed invoice with all fields present, the extraction agent
    must return ExtractionSuccess with the correct field values.

    Pass criteria (requirements.md §8 scenario 1):
      - outcome == "success"
      - extraction_status == EXTRACTED
      - invoice_number, vendor_name, po_reference, contract_reference match fixture
      - grand_total > 0
      - at least one line item extracted
    """

    def test_scenario01_returns_success(self, agent: ExtractionAgent):
        fixture = _load_fixture("scenario_01_clean_invoice.json")
        result = _extract_with_retry(agent, fixture["invoice_text"])

        assert isinstance(result, ExtractionSuccess), (
            f"Expected ExtractionSuccess but got ExtractionFailure: "
            f"reason={result.reason if isinstance(result, ExtractionFailure) else ''} — "
            f"detail={result.error_detail if isinstance(result, ExtractionFailure) else ''}"
        )
        assert result.extraction_status == ExtractionStatus.EXTRACTED

    def test_scenario01_invoice_number_correct(self, agent: ExtractionAgent):
        fixture = _load_fixture("scenario_01_clean_invoice.json")
        result = _extract_with_retry(agent, fixture["invoice_text"])
        assert isinstance(result, ExtractionSuccess)
        assert result.invoice.invoice_number == fixture["expected"]["invoice_number"]

    def test_scenario01_vendor_name_correct(self, agent: ExtractionAgent):
        fixture = _load_fixture("scenario_01_clean_invoice.json")
        result = _extract_with_retry(agent, fixture["invoice_text"])
        assert isinstance(result, ExtractionSuccess)
        assert result.invoice.vendor_name == fixture["expected"]["vendor_name"]

    def test_scenario01_po_reference_correct(self, agent: ExtractionAgent):
        fixture = _load_fixture("scenario_01_clean_invoice.json")
        result = _extract_with_retry(agent, fixture["invoice_text"])
        assert isinstance(result, ExtractionSuccess)
        assert result.invoice.po_reference == fixture["expected"]["po_reference"]

    def test_scenario01_grand_total_correct(self, agent: ExtractionAgent):
        fixture = _load_fixture("scenario_01_clean_invoice.json")
        result = _extract_with_retry(agent, fixture["invoice_text"])
        assert isinstance(result, ExtractionSuccess)
        assert result.invoice.grand_total == Decimal(fixture["expected"]["grand_total"])

    def test_scenario01_line_item_count(self, agent: ExtractionAgent):
        fixture = _load_fixture("scenario_01_clean_invoice.json")
        result = _extract_with_retry(agent, fixture["invoice_text"])
        assert isinstance(result, ExtractionSuccess)
        assert len(result.invoice.line_items) == fixture["expected"]["line_item_count"]

    def test_scenario01_raw_payload_stored(self, agent: ExtractionAgent):
        """raw_payload must be present for audit (FR-6.1)."""
        fixture = _load_fixture("scenario_01_clean_invoice.json")
        result = _extract_with_retry(agent, fixture["invoice_text"])
        assert isinstance(result, ExtractionSuccess)
        assert result.raw_payload is not None and len(result.raw_payload) > 0


# ---------------------------------------------------------------------------
# Scenario 4 — Malformed extraction: grand_total absent (§8 scenario 4)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestScenario04MalformedExtraction:
    """
    Given an invoice where grand_total is physically absent from the document,
    the extraction agent must return ExtractionFailure / NEEDS_REEXTRACTION.
    It must NEVER fabricate the grand_total value (FR-1.3).

    Pass criteria (requirements.md §8 scenario 4):
      - outcome == "failure"
      - extraction_status == NEEDS_REEXTRACTION
      - result is ExtractionFailure (not ExtractionSuccess)
    """

    def test_scenario04_returns_failure(self, agent: ExtractionAgent):
        fixture = _load_fixture("scenario_04_malformed_extraction.json")
        result = _extract_with_retry(agent, fixture["invoice_text"])

        assert isinstance(result, ExtractionFailure), (
            f"Expected ExtractionFailure (NEEDS_REEXTRACTION) but agent returned "
            f"ExtractionSuccess with grand_total="
            f"{result.invoice.grand_total if isinstance(result, ExtractionSuccess) else 'N/A'}. "
            f"The agent MUST NOT fabricate a missing required field (FR-1.3)."
        )

    def test_scenario04_status_is_needs_reextraction(self, agent: ExtractionAgent):
        fixture = _load_fixture("scenario_04_malformed_extraction.json")
        result = _extract_with_retry(agent, fixture["invoice_text"])
        assert isinstance(result, ExtractionFailure)
        assert result.extraction_status == ExtractionStatus.NEEDS_REEXTRACTION

    def test_scenario04_never_enters_payment_path(self, agent: ExtractionAgent):
        """
        An ExtractionFailure carries no InvoiceCreate — it cannot be scheduled
        for payment. This is the structural enforcement of FR-5.1.
        """
        fixture = _load_fixture("scenario_04_malformed_extraction.json")
        result = _extract_with_retry(agent, fixture["invoice_text"])
        assert isinstance(result, ExtractionFailure)
        assert not hasattr(result, "invoice"), (
            "ExtractionFailure must not carry an invoice attribute."
        )

    def test_scenario04_raw_payload_stored_for_audit(self, agent: ExtractionAgent):
        """Even on failure, raw_payload field must exist (FR-6.1)."""
        fixture = _load_fixture("scenario_04_malformed_extraction.json")
        result = _extract_with_retry(agent, fixture["invoice_text"])
        assert isinstance(result, ExtractionFailure)
        assert hasattr(result, "raw_payload")
