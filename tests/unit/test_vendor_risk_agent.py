"""
tests/unit/test_vendor_risk_agent.py — Unit tests for agents/vendor_risk_agent.py.

All tests are pure — no real DB queries (list_vendors is patched), no real
LLM calls (llm_client is injected via the parameter).  The SQLAlchemy session
is mocked.

Key scenario:
  An auto-created vendor with 3 approved PRICE_VARIANCE overrides in a row
  → risk_level="high", reasons mention auto-creation and repeated override pattern.

Additional coverage:
  - VendorRiskFlag model validates correct inputs and rejects bad ones
  - {"skip": true} response → vendor not included in returned list
  - VENDOR_RISK_FLAGGED audit event written per flagged vendor
  - Vendor with no exceptions and no auto-creation flag → skipped (not sent to LLM)
  - LLM call error → vendor skipped (no crash), rest of assessment continues
  - Malformed JSON → VendorRiskError raised from _parse_llm_response
  - Markdown fences stripped before parse
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from agents.vendor_risk_agent import (
    VendorRiskError,
    VendorRiskFlag,
    _parse_llm_response,
    assess_vendor_risk,
)
from audit.writer import _append, _payload, clear_audit_log, get_all_events
from models.audit_event import AuditEventCreate
from models.enums import (
    AuditEventType,
    ExceptionReasonCode,
    ExceptionStatus,
    HumanAction,
)
from models.exception_record import ExceptionReasonSchema, ExceptionRecordCreate
from models.vendor import VendorCreate
from routing.decision import ExceptionDecision
from services.exception_store import clear_store, register_exception


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_llm_client(response: str) -> MagicMock:
    client = MagicMock()
    client.complete.return_value = response
    return client


def _flag_json(
    vendor_code: str = "ACME-001",
    risk_level: str = "high",
    reasons: list[str] | None = None,
    recommended_action: str = "Review vendor history with procurement.",
) -> str:
    return json.dumps({
        "vendor_code": vendor_code,
        "risk_level": risk_level,
        "reasons": reasons or [
            "Vendor was auto-created 3 days ago — not manually onboarded.",
            "3 consecutive PRICE_VARIANCE exceptions, all approved with override.",
        ],
        "recommended_action": recommended_action,
    })


def _skip_json() -> str:
    return json.dumps({"skip": True})


def _make_vendor(vendor_code: str = "ACME-001", name: str = "Acme Corp") -> VendorCreate:
    return VendorCreate(vendor_code=vendor_code, name=name, is_active=True)


def _mock_session(vendors: list[VendorCreate]) -> MagicMock:
    """Return a mock Session whose list_vendors patch will return `vendors`."""
    return MagicMock()


def _write_vendor_auto_created_event(vendor_code: str, vendor_name: str) -> None:
    """Write a VENDOR_AUTO_CREATED audit event into the in-process log."""
    _append(
        AuditEventCreate(
            invoice_id=f"vendor:{vendor_code}",
            event_type=AuditEventType.VENDOR_AUTO_CREATED,
            payload_json=_payload(
                vendor_code=vendor_code,
                vendor_name=vendor_name,
                source_document="PO",
                created_vendor_id="uuid-123",
                reason="Vendor code not previously known",
            ),
        )
    )


def _write_exception_audit_event(invoice_id: str, vendor_name: str) -> None:
    """Write a minimal EXCEPTION_RAISED audit event so vendor name is discoverable."""
    _append(
        AuditEventCreate(
            invoice_id=invoice_id,
            event_type=AuditEventType.EXCEPTION_RAISED,
            vendor_name=vendor_name,
            payload_json=_payload(reason_codes=["PRICE_VARIANCE"]),
        )
    )


def _register_approved_price_variance(invoice_id: str, variance_pct: str = "0.05") -> None:
    """Register a RESOLVED APPROVE_OVERRIDE PRICE_VARIANCE exception."""
    record = ExceptionRecordCreate.model_construct(
        invoice_id=invoice_id,
        status=ExceptionStatus.RESOLVED,
        reasons=[
            ExceptionReasonSchema(
                reason_code=ExceptionReasonCode.PRICE_VARIANCE,
                supporting_data={
                    "line_variances": [
                        {
                            "line_number": 1,
                            "billed_unit_price": "105.00",
                            "contract_unit_price": "100.00",
                            "price_variance_pct": variance_pct,
                            "price_variance_abs": "5.00",
                        }
                    ]
                },
            )
        ],
        human_action=HumanAction.APPROVE_OVERRIDE,
        actor_id="reviewer@company.com",
        resolution_notes="Approved — known pricing issue.",
        resolved_at=datetime.now(tz=timezone.utc),
    )
    register_exception(ExceptionDecision(invoice_id=invoice_id, exception_record=record))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean():
    clear_store()
    clear_audit_log()
    yield
    clear_store()
    clear_audit_log()


# ---------------------------------------------------------------------------
# TestVendorRiskFlagModel
# ---------------------------------------------------------------------------


class TestVendorRiskFlagModel:
    def test_valid_model(self) -> None:
        flag = VendorRiskFlag(
            vendor_code="V001",
            risk_level="high",
            reasons=["Auto-created and 3 overrides."],
            recommended_action="Review with procurement.",
        )
        assert flag.risk_level == "high"
        assert len(flag.reasons) == 1

    def test_invalid_risk_level(self) -> None:
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            VendorRiskFlag(
                vendor_code="V001",
                risk_level="critical",   # not a valid literal
                reasons=["x"],
                recommended_action="y",
            )

    def test_reasons_must_have_at_least_one(self) -> None:
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            VendorRiskFlag(
                vendor_code="V001",
                risk_level="low",
                reasons=[],             # empty list not allowed
                recommended_action="y",
            )


# ---------------------------------------------------------------------------
# TestParseResponse
# ---------------------------------------------------------------------------


class TestParseResponse:
    def test_valid_flag_parsed(self) -> None:
        raw = _flag_json("V001")
        result = _parse_llm_response(raw, "V001")
        assert result is not None
        assert result.vendor_code == "V001"
        assert result.risk_level == "high"

    def test_skip_returns_none(self) -> None:
        assert _parse_llm_response(_skip_json(), "V001") is None

    def test_markdown_fences_stripped(self) -> None:
        raw = "```json\n" + _flag_json("V001") + "\n```"
        result = _parse_llm_response(raw, "V001")
        assert result is not None
        assert result.vendor_code == "V001"

    def test_non_json_raises(self) -> None:
        with pytest.raises(VendorRiskError, match="non-JSON"):
            _parse_llm_response("not json at all", "V001")

    def test_invalid_schema_raises(self) -> None:
        bad = json.dumps({"vendor_code": "V001", "risk_level": "extreme", "reasons": [], "recommended_action": "x"})
        with pytest.raises(VendorRiskError, match="validation"):
            _parse_llm_response(bad, "V001")


# ---------------------------------------------------------------------------
# TestAutoCreatedWithRepeatedOverrides  ← key scenario
# ---------------------------------------------------------------------------


class TestAutoCreatedWithRepeatedOverrides:
    """
    Core scenario: vendor auto-created + 3 consecutive approved PRICE_VARIANCE
    overrides → risk_level="high", reasons mention both signals.
    """

    VENDOR_CODE = "NEWV-001"
    VENDOR_NAME = "New Vendor Corp"

    def _seed(self) -> None:
        # Auto-creation event in audit log
        _write_vendor_auto_created_event(self.VENDOR_CODE, self.VENDOR_NAME)

        # 3 resolved PRICE_VARIANCE exceptions, all APPROVE_OVERRIDE
        for i in range(1, 4):
            inv_id = f"INV-{i:04d}"
            _write_exception_audit_event(inv_id, self.VENDOR_NAME)
            _register_approved_price_variance(inv_id, variance_pct=f"0.0{i}")

    def test_risk_level_is_high(self) -> None:
        self._seed()
        vendor = _make_vendor(self.VENDOR_CODE, self.VENDOR_NAME)

        mock_response = _flag_json(
            vendor_code=self.VENDOR_CODE,
            risk_level="high",
            reasons=[
                f"Vendor '{self.VENDOR_NAME}' was auto-created from a PO upload — "
                "not manually onboarded.",
                "3 consecutive PRICE_VARIANCE exceptions, all resolved with "
                "APPROVE_OVERRIDE — possible rubber-stamp pattern.",
            ],
        )

        with patch("agents.vendor_risk_agent.list_vendors", return_value=[vendor]):
            flags = assess_vendor_risk(
                session=_mock_session([vendor]),
                llm_client=_make_llm_client(mock_response),
            )

        assert len(flags) == 1
        assert flags[0].risk_level == "high"

    def test_reasons_mention_auto_creation(self) -> None:
        self._seed()
        vendor = _make_vendor(self.VENDOR_CODE, self.VENDOR_NAME)

        mock_response = _flag_json(
            vendor_code=self.VENDOR_CODE,
            risk_level="high",
            reasons=[
                "Vendor was auto-created from a PO document — not manually onboarded.",
                "3 PRICE_VARIANCE exceptions all approved with override.",
            ],
        )

        with patch("agents.vendor_risk_agent.list_vendors", return_value=[vendor]):
            flags = assess_vendor_risk(
                session=_mock_session([vendor]),
                llm_client=_make_llm_client(mock_response),
            )

        assert flags
        auto_create_mentions = [
            r for r in flags[0].reasons
            if "auto-created" in r.lower() or "auto_created" in r.lower()
            or "not manually" in r.lower()
        ]
        assert auto_create_mentions, (
            f"Expected a reason mentioning auto-creation, got: {flags[0].reasons}"
        )

    def test_reasons_mention_override_pattern(self) -> None:
        self._seed()
        vendor = _make_vendor(self.VENDOR_CODE, self.VENDOR_NAME)

        mock_response = _flag_json(
            vendor_code=self.VENDOR_CODE,
            risk_level="high",
            reasons=[
                "Auto-created vendor.",
                "3 PRICE_VARIANCE exceptions, all approved with APPROVE_OVERRIDE — "
                "no rejections on record.",
            ],
        )

        with patch("agents.vendor_risk_agent.list_vendors", return_value=[vendor]):
            flags = assess_vendor_risk(
                session=_mock_session([vendor]),
                llm_client=_make_llm_client(mock_response),
            )

        assert flags
        override_mentions = [
            r for r in flags[0].reasons
            if "override" in r.lower() or "approve" in r.lower() or "price_variance" in r.lower()
        ]
        assert override_mentions, (
            f"Expected a reason mentioning override pattern, got: {flags[0].reasons}"
        )

    def test_vendor_risk_flagged_audit_event_written(self) -> None:
        self._seed()
        vendor = _make_vendor(self.VENDOR_CODE, self.VENDOR_NAME)

        with patch("agents.vendor_risk_agent.list_vendors", return_value=[vendor]):
            assess_vendor_risk(
                session=_mock_session([vendor]),
                llm_client=_make_llm_client(_flag_json(self.VENDOR_CODE)),
            )

        events = get_all_events()
        risk_events = [
            e for e in events
            if e.get("event_type") == AuditEventType.VENDOR_RISK_FLAGGED.value
        ]
        assert len(risk_events) == 1, (
            f"Expected 1 VENDOR_RISK_FLAGGED event, found {len(risk_events)}"
        )
        payload = json.loads(risk_events[0]["payload_json"])
        assert payload["vendor_code"] == self.VENDOR_CODE
        assert payload["risk_level"] == "high"

    def test_prompt_contains_auto_creation_signal(self) -> None:
        """The rendered prompt sent to the LLM must contain the auto-creation info."""
        self._seed()
        vendor = _make_vendor(self.VENDOR_CODE, self.VENDOR_NAME)

        captured: list[str] = []

        class _Capture:
            def complete(self, _sys: str, user: str) -> str:
                captured.append(user)
                return _flag_json(vendor_code=self.VENDOR_CODE if False else "NEWV-001")

        with patch("agents.vendor_risk_agent.list_vendors", return_value=[vendor]):
            assess_vendor_risk(session=_mock_session([vendor]), llm_client=_Capture())

        assert captured, "LLM client was never called"
        prompt = captured[0]
        assert "auto-created" in prompt.lower() or "WAS auto-created" in prompt, (
            "Auto-creation info not found in rendered prompt"
        )
        assert "PRICE_VARIANCE" in prompt, (
            "PRICE_VARIANCE not found in rendered prompt"
        )
        assert "APPROVE_OVERRIDE" in prompt, (
            "APPROVE_OVERRIDE not found in rendered prompt — resolution pattern missing"
        )


# ---------------------------------------------------------------------------
# TestSkipLogic
# ---------------------------------------------------------------------------


class TestSkipLogic:
    """Vendors with no exceptions and no auto-creation are skipped without LLM call."""

    def test_clean_vendor_not_sent_to_llm(self) -> None:
        vendor = _make_vendor("CLEAN-001", "Clean Corp")
        # No audit events, no exceptions seeded

        client = _make_llm_client(_flag_json("CLEAN-001"))

        with patch("agents.vendor_risk_agent.list_vendors", return_value=[vendor]):
            flags = assess_vendor_risk(
                session=_mock_session([vendor]),
                llm_client=client,
            )

        assert flags == [], "Expected no flags for a clean vendor"
        client.complete.assert_not_called()

    def test_llm_skip_response_produces_no_flag(self) -> None:
        vendor = _make_vendor("SKIP-001", "Skip Vendor")
        _write_vendor_auto_created_event("SKIP-001", "Skip Vendor")

        with patch("agents.vendor_risk_agent.list_vendors", return_value=[vendor]):
            flags = assess_vendor_risk(
                session=_mock_session([vendor]),
                llm_client=_make_llm_client(_skip_json()),
            )

        assert flags == []

    def test_no_vendor_risk_event_when_skipped(self) -> None:
        vendor = _make_vendor("SKIP-002", "Skip Corp")
        _write_vendor_auto_created_event("SKIP-002", "Skip Corp")

        with patch("agents.vendor_risk_agent.list_vendors", return_value=[vendor]):
            assess_vendor_risk(
                session=_mock_session([vendor]),
                llm_client=_make_llm_client(_skip_json()),
            )

        events = get_all_events()
        assert not any(
            e.get("event_type") == AuditEventType.VENDOR_RISK_FLAGGED.value
            for e in events
        )


# ---------------------------------------------------------------------------
# TestMultipleVendors
# ---------------------------------------------------------------------------


class TestMultipleVendors:
    """With multiple vendors, only flagged ones appear in results."""

    def test_only_risky_vendor_flagged(self) -> None:
        clean = _make_vendor("CLEAN-A", "Clean Corp A")
        risky = _make_vendor("RISKY-A", "Risky Corp A")

        _write_vendor_auto_created_event("RISKY-A", "Risky Corp A")

        responses = [_skip_json(), _flag_json("RISKY-A")]
        client = MagicMock()
        # First call (clean vendor with auto-creation) skips;
        # but clean vendor has no auto-creation so it's not called at all.
        # Only risky vendor gets a call.
        client.complete.return_value = _flag_json("RISKY-A")

        with patch("agents.vendor_risk_agent.list_vendors", return_value=[clean, risky]):
            flags = assess_vendor_risk(
                session=_mock_session([clean, risky]),
                llm_client=client,
            )

        assert len(flags) == 1
        assert flags[0].vendor_code == "RISKY-A"
        # clean vendor has no exceptions and no auto-creation, so LLM called once
        client.complete.assert_called_once()

    def test_multiple_flags_produce_multiple_audit_events(self) -> None:
        v1 = _make_vendor("V-001", "Vendor One")
        v2 = _make_vendor("V-002", "Vendor Two")

        _write_vendor_auto_created_event("V-001", "Vendor One")
        _write_vendor_auto_created_event("V-002", "Vendor Two")

        responses = [_flag_json("V-001", risk_level="high"), _flag_json("V-002", risk_level="medium")]
        call_count = [0]

        class _MultiClient:
            def complete(self, _s: str, _u: str) -> str:
                r = responses[call_count[0]]
                call_count[0] += 1
                return r

        with patch("agents.vendor_risk_agent.list_vendors", return_value=[v1, v2]):
            flags = assess_vendor_risk(
                session=_mock_session([v1, v2]),
                llm_client=_MultiClient(),
            )

        assert len(flags) == 2
        events = get_all_events()
        risk_events = [
            e for e in events
            if e.get("event_type") == AuditEventType.VENDOR_RISK_FLAGGED.value
        ]
        assert len(risk_events) == 2


# ---------------------------------------------------------------------------
# TestLLMFailureHandling
# ---------------------------------------------------------------------------


class TestLLMFailureHandling:
    """LLM errors on individual vendors should not crash the whole assessment."""

    def test_llm_error_skips_vendor_continues(self) -> None:
        from extraction.llm_client import LLMCallError

        v_fail = _make_vendor("FAIL-001", "Fail Corp")
        v_ok = _make_vendor("OK-001", "OK Corp")

        _write_vendor_auto_created_event("FAIL-001", "Fail Corp")
        _write_vendor_auto_created_event("OK-001", "OK Corp")

        client = MagicMock()
        client.complete.side_effect = [
            LLMCallError("rate limit", status_code=429),
            _flag_json("OK-001"),
        ]

        with patch("agents.vendor_risk_agent.list_vendors", return_value=[v_fail, v_ok]):
            flags = assess_vendor_risk(
                session=_mock_session([v_fail, v_ok]),
                llm_client=client,
            )

        # Only the OK vendor should be flagged; FAIL vendor silently skipped
        assert len(flags) == 1
        assert flags[0].vendor_code == "OK-001"

    def test_parse_error_skips_vendor(self) -> None:
        vendor = _make_vendor("BAD-001", "Bad Response Corp")
        _write_vendor_auto_created_event("BAD-001", "Bad Response Corp")

        with patch("agents.vendor_risk_agent.list_vendors", return_value=[vendor]):
            flags = assess_vendor_risk(
                session=_mock_session([vendor]),
                llm_client=_make_llm_client("not valid json {{{{"),
            )

        assert flags == []
