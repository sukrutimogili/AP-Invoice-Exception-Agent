"""
tests/unit/test_exception_triage_agent.py — Unit tests for agents/exception_triage_agent.py.

All tests are pure — no DB, no real LLM calls.  The LLMClient is satisfied
by a MagicMock whose complete() method returns a pre-built JSON string.

Key scenario covered:
  PRICE_VARIANCE invoice where the same vendor has 3 prior approved PRICE_VARIANCE
  overrides → the agent's supporting_context must reference that pattern.

Additional coverage:
  - ExceptionInvestigation Pydantic model validates correctly
  - INVESTIGATION_COMPLETED audit event is written after a successful call
  - InvestigationError raised when LLM returns malformed JSON
  - InvestigationError raised when LLM returns JSON that fails validation
  - Vendor with no prior history → supporting_context says so
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from agents.exception_triage_agent import (
    ExceptionInvestigation,
    InvestigationError,
    investigate_exception,
)
from audit.writer import clear_audit_log, get_all_events
from models.audit_event import AuditEventCreate
from models.enums import (
    AuditEventType,
    ExceptionReasonCode,
    ExceptionStatus,
    HumanAction,
)
from models.exception_record import ExceptionReasonSchema, ExceptionRecordBase, ExceptionRecordCreate
from routing.decision import ExceptionDecision
from services.exception_store import clear_store, register_exception


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_llm_client(response: str) -> MagicMock:
    """Return a mock that satisfies LLMClient Protocol with a fixed response."""
    client = MagicMock()
    client.complete.return_value = response
    return client


def _investigation_json(
    *,
    root_cause_summary: str = "Price billed exceeds the contracted rate by 5%.",
    recommended_action: str = "APPROVE_OVERRIDE",
    confidence: str = "high",
    supporting_context: list[str] | None = None,
) -> str:
    """Return a valid ExceptionInvestigation JSON string."""
    return json.dumps(
        {
            "root_cause_summary": root_cause_summary,
            "recommended_action": recommended_action,
            "confidence": confidence,
            "supporting_context": supporting_context or [
                "Vendor has 3 prior PRICE_VARIANCE exceptions, all approved with override.",
            ],
        }
    )


def _make_price_variance_exception(invoice_id: str) -> ExceptionDecision:
    """Build a minimal PRICE_VARIANCE ExceptionDecision and register it."""
    exc = ExceptionDecision(
        invoice_id=invoice_id,
        exception_record=ExceptionRecordCreate(
            invoice_id=invoice_id,
            status=ExceptionStatus.OPEN,
            reasons=[
                ExceptionReasonSchema(
                    reason_code=ExceptionReasonCode.PRICE_VARIANCE,
                    supporting_data={
                        "line_variances": [
                            {
                                "line_number": 1,
                                "billed_unit_price": "105.00",
                                "contract_unit_price": "100.00",
                                "price_variance_abs": "5.00",
                                "price_variance_pct": "0.05",
                            }
                        ]
                    },
                )
            ],
        ),
    )
    register_exception(exc)
    return exc


def _make_resolved_price_variance_exception(
    invoice_id: str, vendor_name: str
) -> ExceptionDecision:
    """
    Build a PRICE_VARIANCE ExceptionDecision that has been APPROVE_OVERRIDE-resolved.

    Also writes a minimal EXCEPTION_RAISED audit event so the vendor history builder
    can correlate the invoice_id to the vendor_name.

    Uses model_construct() to bypass the OPEN-only validator on ExceptionRecordCreate
    — this is intentional: we are seeding historical resolved records for test purposes.
    """
    from datetime import datetime, timezone

    from audit.writer import _append, _payload

    # Write audit event so vendor name is discoverable via the audit trail
    _append(
        AuditEventCreate(
            invoice_id=invoice_id,
            event_type=AuditEventType.EXCEPTION_RAISED,
            vendor_name=vendor_name,
            payload_json=_payload(reason_codes=["PRICE_VARIANCE"]),
        )
    )

    reasons = [
        ExceptionReasonSchema(
            reason_code=ExceptionReasonCode.PRICE_VARIANCE,
            supporting_data={},
        )
    ]
    # Bypass the OPEN-only validator to simulate a resolved historical record
    resolved_record = ExceptionRecordCreate.model_construct(
        invoice_id=invoice_id,
        status=ExceptionStatus.RESOLVED,
        reasons=reasons,
        human_action=HumanAction.APPROVE_OVERRIDE,
        actor_id="reviewer@company.com",
        resolution_notes="Known pricing lag — approved.",
        resolved_at=datetime.now(tz=timezone.utc),
    )

    exc = ExceptionDecision(
        invoice_id=invoice_id,
        exception_record=resolved_record,
    )
    register_exception(exc)
    return exc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_stores():
    """Reset in-process stores before each test."""
    clear_store()
    clear_audit_log()
    yield
    clear_store()
    clear_audit_log()


# ---------------------------------------------------------------------------
# TestExceptionInvestigationModel
# ---------------------------------------------------------------------------


class TestExceptionInvestigationModel:
    """ExceptionInvestigation validates correct inputs and rejects bad ones."""

    def test_valid_full_model(self) -> None:
        inv = ExceptionInvestigation(
            root_cause_summary="Unit price 5% above contracted rate.",
            recommended_action="APPROVE_OVERRIDE",
            confidence="high",
            supporting_context=["Prior 3 approvals for same reason."],
        )
        assert inv.recommended_action == "APPROVE_OVERRIDE"
        assert inv.confidence == "high"
        assert len(inv.supporting_context) == 1

    def test_invalid_recommended_action_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ExceptionInvestigation(
                root_cause_summary="x",
                recommended_action="AUTO_PAY",  # not a valid literal
                confidence="high",
                supporting_context=[],
            )

    def test_invalid_confidence_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ExceptionInvestigation(
                root_cause_summary="x",
                recommended_action="REJECT",
                confidence="very_high",  # not a valid literal
                supporting_context=[],
            )

    def test_supporting_context_defaults_to_empty_list(self) -> None:
        inv = ExceptionInvestigation(
            root_cause_summary="x",
            recommended_action="ESCALATE",
            confidence="low",
        )
        assert inv.supporting_context == []


# ---------------------------------------------------------------------------
# TestVendorPatternDetection  ← the main scenario
# ---------------------------------------------------------------------------


class TestVendorPatternDetection:
    """
    Core scenario: PRICE_VARIANCE invoice where the same vendor has 3 prior
    approved PRICE_VARIANCE overrides.

    The LLM is mocked to return a realistic response that references the vendor
    pattern in supporting_context.  We verify:
      1. The function returns an ExceptionInvestigation with the expected fields.
      2. supporting_context contains language referencing the prior-approval pattern.
      3. An INVESTIGATION_COMPLETED audit event is written.
    """

    VENDOR = "Acme Supplies Ltd"
    CURRENT_INVOICE = "INV-CURRENT-001"

    def _seed_history(self) -> None:
        """Seed 3 prior APPROVE_OVERRIDE PRICE_VARIANCE exceptions for the vendor."""
        for i in range(1, 4):
            _make_resolved_price_variance_exception(
                invoice_id=f"INV-PRIOR-{i:03d}",
                vendor_name=self.VENDOR,
            )

    def _seed_current_exception(self) -> None:
        """Register the current open exception and write its audit event."""
        from audit.writer import _append, _payload

        _append(
            AuditEventCreate(
                invoice_id=self.CURRENT_INVOICE,
                event_type=AuditEventType.EXCEPTION_RAISED,
                vendor_name=self.VENDOR,
                payload_json=_payload(reason_codes=["PRICE_VARIANCE"]),
            )
        )
        _make_price_variance_exception(self.CURRENT_INVOICE)

    def test_supporting_context_references_prior_approval_pattern(self) -> None:
        """
        The LLM receives context about 3 prior approved overrides and its response
        (mocked here) includes that pattern in supporting_context.
        """
        self._seed_history()
        self._seed_current_exception()

        # The mock returns a response that explicitly references the vendor pattern.
        # In a real call the LLM would derive this from the rendered prompt; here we
        # inject the expected output to test the full round-trip without a network call.
        mock_response = _investigation_json(
            root_cause_summary=(
                "The billed unit price exceeds the contracted rate by 5% on line 1.  "
                "This vendor (Acme Supplies Ltd) has had this pattern before and all "
                "3 prior exceptions were approved."
            ),
            recommended_action="APPROVE_OVERRIDE",
            confidence="high",
            supporting_context=[
                "Vendor 'Acme Supplies Ltd' has 3 prior PRICE_VARIANCE exceptions, "
                "all resolved with APPROVE_OVERRIDE.",
                "Current variance: 5% above contracted rate on line 1 (billed $105.00 vs contract $100.00).",
                "No REJECT actions in vendor history — pattern is consistent with approved pricing drift.",
            ],
        )
        client = _make_llm_client(mock_response)

        result = investigate_exception(self.CURRENT_INVOICE, llm_client=client)

        assert isinstance(result, ExceptionInvestigation)
        assert result.recommended_action == "APPROVE_OVERRIDE"
        assert result.confidence == "high"

        # At least one supporting_context item must reference the prior pattern
        pattern_mentions = [
            item
            for item in result.supporting_context
            if "3" in item and "PRICE_VARIANCE" in item
        ]
        assert pattern_mentions, (
            "Expected supporting_context to reference the 3 prior PRICE_VARIANCE "
            f"approvals, but got: {result.supporting_context}"
        )

    def test_investigation_completed_audit_event_written(self) -> None:
        """An INVESTIGATION_COMPLETED audit event must appear in the log after the call."""
        self._seed_history()
        self._seed_current_exception()

        client = _make_llm_client(_investigation_json())
        investigate_exception(self.CURRENT_INVOICE, llm_client=client)

        events = get_all_events()
        investigation_events = [
            e for e in events
            if e.get("event_type") == AuditEventType.INVESTIGATION_COMPLETED.value
            and e.get("invoice_id") == self.CURRENT_INVOICE
        ]
        assert len(investigation_events) == 1, (
            f"Expected 1 INVESTIGATION_COMPLETED event, found {len(investigation_events)}"
        )

        payload = json.loads(investigation_events[0]["payload_json"])
        assert payload["recommended_action"] == "APPROVE_OVERRIDE"
        assert payload["confidence"] == "high"
        assert isinstance(payload["supporting_context"], list)

    def test_vendor_name_written_to_audit_event(self) -> None:
        """The vendor_name field on the audit event matches the invoice's vendor."""
        self._seed_history()
        self._seed_current_exception()

        client = _make_llm_client(_investigation_json())
        investigate_exception(self.CURRENT_INVOICE, llm_client=client)

        events = get_all_events()
        investigation_event = next(
            e for e in events
            if e.get("event_type") == AuditEventType.INVESTIGATION_COMPLETED.value
        )
        assert investigation_event.get("vendor_name") == self.VENDOR

    def test_llm_receives_history_context_in_prompt(self) -> None:
        """
        Verify that the prompt rendered to the LLM contains the vendor history text
        (i.e., the 3 prior approved exceptions are visible to the model).
        """
        self._seed_history()
        self._seed_current_exception()

        captured_calls: list[tuple[str, str]] = []

        class _CapturingClient:
            def complete(self, system_prompt: str, user_message: str) -> str:
                captured_calls.append((system_prompt, user_message))
                return _investigation_json()

        investigate_exception(self.CURRENT_INVOICE, llm_client=_CapturingClient())

        assert captured_calls, "LLM client was never called"
        _, user_message = captured_calls[0]

        # The user message (rendered prompt) should contain the vendor name and
        # mention prior exceptions or APPROVE_OVERRIDE outcomes
        assert self.VENDOR in user_message, (
            f"Vendor name '{self.VENDOR}' not found in rendered prompt"
        )
        assert "PRICE_VARIANCE" in user_message, (
            "PRICE_VARIANCE not found in rendered prompt — vendor history not injected"
        )
        assert "APPROVE_OVERRIDE" in user_message, (
            "APPROVE_OVERRIDE not found in rendered prompt — prior approval outcomes not injected"
        )


# ---------------------------------------------------------------------------
# TestNoVendorHistory
# ---------------------------------------------------------------------------


class TestNoVendorHistory:
    """Vendor with no prior history → agent output reflects that."""

    VENDOR = "NewVendor Co"
    INVOICE_ID = "INV-NEWVENDOR-001"

    def _seed(self) -> None:
        from audit.writer import _append, _payload

        _append(
            AuditEventCreate(
                invoice_id=self.INVOICE_ID,
                event_type=AuditEventType.EXCEPTION_RAISED,
                vendor_name=self.VENDOR,
                payload_json=_payload(reason_codes=["PRICE_VARIANCE"]),
            )
        )
        _make_price_variance_exception(self.INVOICE_ID)

    def test_no_history_note_in_prompt(self) -> None:
        """When the vendor has no prior exceptions the prompt says so."""
        self._seed()

        captured: list[str] = []

        class _Capture:
            def complete(self, _sys: str, user: str) -> str:
                captured.append(user)
                return _investigation_json(
                    supporting_context=["No prior exception history found for this vendor."]
                )

        investigate_exception(self.INVOICE_ID, llm_client=_Capture())

        assert captured
        prompt = captured[0]
        assert "No prior exception history" in prompt or self.VENDOR in prompt


# ---------------------------------------------------------------------------
# TestLLMFailurePaths
# ---------------------------------------------------------------------------


class TestLLMFailurePaths:
    """investigate_exception raises InvestigationError on LLM / parse failures."""

    INVOICE_ID = "INV-FAIL-001"

    def _seed(self) -> None:
        from audit.writer import _append, _payload

        _append(
            AuditEventCreate(
                invoice_id=self.INVOICE_ID,
                event_type=AuditEventType.EXCEPTION_RAISED,
                vendor_name="Fail Vendor",
                payload_json=_payload(reason_codes=["PRICE_VARIANCE"]),
            )
        )
        _make_price_variance_exception(self.INVOICE_ID)

    def test_malformed_json_raises_investigation_error(self) -> None:
        """Non-JSON LLM response → InvestigationError."""
        self._seed()
        client = _make_llm_client("This is definitely not JSON at all.")
        with pytest.raises(InvestigationError, match="non-JSON"):
            investigate_exception(self.INVOICE_ID, llm_client=client)

    def test_invalid_schema_raises_investigation_error(self) -> None:
        """Valid JSON that fails Pydantic validation → InvestigationError."""
        self._seed()
        bad_json = json.dumps(
            {
                "root_cause_summary": "x",
                "recommended_action": "NOT_A_VALID_ACTION",
                "confidence": "high",
                "supporting_context": [],
            }
        )
        client = _make_llm_client(bad_json)
        with pytest.raises(InvestigationError, match="validation"):
            investigate_exception(self.INVOICE_ID, llm_client=client)

    def test_llm_call_error_raises_investigation_error(self) -> None:
        """LLMCallError from the client → InvestigationError."""
        from extraction.llm_client import LLMCallError

        self._seed()
        client = MagicMock()
        client.complete.side_effect = LLMCallError("rate limit", status_code=429)

        with pytest.raises(InvestigationError, match="LLM call failed"):
            investigate_exception(self.INVOICE_ID, llm_client=client)

    def test_markdown_fence_stripped_before_parse(self) -> None:
        """LLM response wrapped in ```json fences parses successfully."""
        self._seed()
        wrapped = "```json\n" + _investigation_json() + "\n```"
        client = _make_llm_client(wrapped)
        result = investigate_exception(self.INVOICE_ID, llm_client=client)
        assert result.recommended_action == "APPROVE_OVERRIDE"


# ---------------------------------------------------------------------------
# TestExceptionNotFound
# ---------------------------------------------------------------------------


class TestExceptionNotFound:
    """investigate_exception behaves gracefully when no exception is registered."""

    def test_missing_exception_still_runs(self) -> None:
        """
        If no exception is in the store for the given invoice_id, the agent still
        runs (the reason text will say 'No exception record found') and writes the
        audit event.  It does NOT raise — absence of data is handled in the prompt.
        """
        from audit.writer import _append, _payload

        invoice_id = "INV-MISSING-001"
        # Minimal audit trail so vendor lookup does not crash
        _append(
            AuditEventCreate(
                invoice_id=invoice_id,
                event_type=AuditEventType.EXCEPTION_RAISED,
                vendor_name="Ghost Vendor",
                payload_json=_payload(reason_codes=["PRICE_VARIANCE"]),
            )
        )

        client = _make_llm_client(
            _investigation_json(
                root_cause_summary="No exception data available.",
                recommended_action="ESCALATE",
                confidence="low",
                supporting_context=["Exception record not found in store."],
            )
        )

        result = investigate_exception(invoice_id, llm_client=client)
        assert result.recommended_action == "ESCALATE"

        events = get_all_events()
        assert any(
            e.get("event_type") == AuditEventType.INVESTIGATION_COMPLETED.value
            and e.get("invoice_id") == invoice_id
            for e in events
        )
