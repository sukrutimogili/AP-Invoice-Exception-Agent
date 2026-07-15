"""
tests/unit/test_models.py — Phase 1 unit tests.

spec.md Phase 1 testing requirement:
  "unit tests instantiating each model with valid and invalid data, asserting
   validation errors on the invalid cases (missing required field, wrong type)."

One TestClass per model.  Each class has:
  - test_valid_*   : happy-path construction
  - test_invalid_* : ValidationError on bad / missing fields
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from models.contract import (
    ContractCreate,
    ContractLineItemCreate,
    DiscountTermSchema,
)
from models.discount_recommendation import DiscountRecommendationCreate
from models.enums import (
    AuditEventType,
    DiscountRecommendation,
    ExceptionReasonCode,
    ExceptionStatus,
    ExtractionStatus,
    HumanAction,
    InvoiceStatus,
)
from models.exception_record import (
    ExceptionRecordCreate,
    ExceptionReasonSchema,
    HumanResolutionUpdate,
)
from models.invoice import InvoiceCreate, InvoiceLineItemCreate, InvoiceReceived
from models.match_result import LineItemMatchDetail, MatchResultCreate
from models.payment_schedule import PaymentScheduleCreate
from models.purchase_order import POLineItemCreate, PurchaseOrderCreate
from models.vendor import VendorCreate
from models.audit_event import AuditEventCreate


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

def _po_line(n: int = 1) -> dict:
    return {"line_number": n, "description": "Widget A", "qty": "10", "unit_price": "38.00"}


def _contract_line(n: int = 1) -> dict:
    return {"line_number": n, "description": "Widget A", "unit_price": "38.00"}


def _invoice_line(n: int = 1) -> dict:
    return {
        "line_number": n,
        "description": "Widget A",
        "qty": "10",
        "unit_price": "38.00",
        "amount": "380.00",
    }


def _valid_invoice_payload(**overrides) -> dict:
    base = {
        "invoice_number": "INV-001",
        "vendor_name": "Acme Corp",
        "invoice_date": date(2026, 1, 15),
        "po_reference": "PO-100",
        "contract_reference": "CTR-001",
        "subtotal": "380.00",
        "tax": "38.00",
        "grand_total": "418.00",
        "due_date": date(2026, 2, 14),
        "payment_terms": "Net 30",
        "line_items": [_invoice_line()],
    }
    base.update(overrides)
    return base


# ===========================================================================
# Vendor
# ===========================================================================


class TestVendorCreate:

    def test_valid_minimal(self):
        v = VendorCreate(vendor_code="V001", name="Acme Corp")
        assert v.vendor_code == "V001"
        assert v.is_active is True

    def test_valid_full(self):
        v = VendorCreate(
            vendor_code="V002",
            name="Beta Ltd",
            contact_email="ap@beta.com",
            is_active=False,
            notes="On probation",
        )
        assert v.contact_email == "ap@beta.com"
        assert v.is_active is False

    def test_invalid_missing_vendor_code(self):
        with pytest.raises(ValidationError) as exc:
            VendorCreate(name="Acme Corp")
        assert "vendor_code" in str(exc.value)

    def test_invalid_missing_name(self):
        with pytest.raises(ValidationError) as exc:
            VendorCreate(vendor_code="V001")
        assert "name" in str(exc.value)

    def test_invalid_blank_vendor_code(self):
        with pytest.raises(ValidationError):
            VendorCreate(vendor_code="   ", name="Acme")

    def test_invalid_blank_name(self):
        with pytest.raises(ValidationError):
            VendorCreate(vendor_code="V001", name="")

    def test_invalid_bad_email(self):
        with pytest.raises(ValidationError):
            VendorCreate(vendor_code="V001", name="Acme", contact_email="not-an-email")

    def test_vendor_code_stripped(self):
        v = VendorCreate(vendor_code="  V001  ", name="Acme")
        assert v.vendor_code == "V001"


# ===========================================================================
# POLineItem + PurchaseOrder
# ===========================================================================


class TestPOLineItemCreate:

    def test_valid(self):
        item = POLineItemCreate(**_po_line())
        assert item.qty == Decimal("10")
        assert item.unit_price == Decimal("38.00")

    def test_invalid_zero_qty(self):
        with pytest.raises(ValidationError):
            POLineItemCreate(line_number=1, description="X", qty="0", unit_price="10")

    def test_invalid_negative_qty(self):
        with pytest.raises(ValidationError):
            POLineItemCreate(line_number=1, description="X", qty="-1", unit_price="10")

    def test_invalid_negative_price(self):
        with pytest.raises(ValidationError):
            POLineItemCreate(line_number=1, description="X", qty="1", unit_price="-5")

    def test_invalid_missing_description(self):
        with pytest.raises(ValidationError):
            POLineItemCreate(line_number=1, qty="1", unit_price="10")

    def test_invalid_blank_description(self):
        with pytest.raises(ValidationError):
            POLineItemCreate(line_number=1, description="", qty="1", unit_price="10")

    def test_invalid_line_number_zero(self):
        with pytest.raises(ValidationError):
            POLineItemCreate(line_number=0, description="X", qty="1", unit_price="10")


class TestPurchaseOrderCreate:

    def test_valid(self):
        po = PurchaseOrderCreate(
            po_number="PO-100",
            vendor_id="vendor-uuid",
            po_total="380.00",
            approval_threshold="10000",
            line_items=[_po_line()],
        )
        assert po.po_number == "PO-100"
        assert len(po.line_items) == 1

    def test_invalid_missing_po_number(self):
        with pytest.raises(ValidationError):
            PurchaseOrderCreate(
                vendor_id="v", po_total="100", approval_threshold="10000",
                line_items=[_po_line()],
            )

    def test_invalid_empty_line_items(self):
        with pytest.raises(ValidationError):
            PurchaseOrderCreate(
                po_number="PO-1", vendor_id="v", po_total="100",
                approval_threshold="10000", line_items=[],
            )

    def test_invalid_zero_approval_threshold(self):
        with pytest.raises(ValidationError):
            PurchaseOrderCreate(
                po_number="PO-1", vendor_id="v", po_total="100",
                approval_threshold="0", line_items=[_po_line()],
            )

    def test_invalid_negative_po_total(self):
        with pytest.raises(ValidationError):
            PurchaseOrderCreate(
                po_number="PO-1", vendor_id="v", po_total="-1",
                approval_threshold="10000", line_items=[_po_line()],
            )

    def test_invalid_blank_po_number(self):
        with pytest.raises(ValidationError):
            PurchaseOrderCreate(
                po_number="  ", vendor_id="v", po_total="100",
                approval_threshold="10000", line_items=[_po_line()],
            )



# ===========================================================================
# DiscountTerm + Contract
# ===========================================================================


class TestDiscountTermSchema:

    def test_valid(self):
        dt = DiscountTermSchema(
            discount_term_raw="2/10 net 30",
            discount_pct="0.02",
            discount_days=10,
            net_days=30,
        )
        assert dt.discount_pct == Decimal("0.02")

    def test_invalid_discount_days_gte_net_days(self):
        with pytest.raises(ValidationError):
            DiscountTermSchema(
                discount_term_raw="2/10 net 10",
                discount_pct="0.02",
                discount_days=10,
                net_days=10,
            )

    def test_invalid_discount_pct_zero(self):
        with pytest.raises(ValidationError):
            DiscountTermSchema(
                discount_term_raw="0/10 net 30",
                discount_pct="0",
                discount_days=10,
                net_days=30,
            )

    def test_invalid_discount_pct_gte_one(self):
        with pytest.raises(ValidationError):
            DiscountTermSchema(
                discount_term_raw="100/10 net 30",
                discount_pct="1.0",
                discount_days=10,
                net_days=30,
            )

    def test_invalid_missing_net_days(self):
        with pytest.raises(ValidationError):
            DiscountTermSchema(
                discount_term_raw="2/10 net 30",
                discount_pct="0.02",
                discount_days=10,
            )


class TestContractCreate:

    def _line(self, n=1):
        return ContractLineItemCreate(line_number=n, description="Widget A", unit_price="38.00")

    def test_valid_no_discount(self):
        c = ContractCreate(
            contract_reference="CTR-001",
            vendor_id="v-uuid",
            line_items=[self._line()],
        )
        assert c.discount_term is None

    def test_valid_with_discount(self):
        c = ContractCreate(
            contract_reference="CTR-002",
            vendor_id="v-uuid",
            discount_term=DiscountTermSchema(
                discount_term_raw="2/10 net 30",
                discount_pct="0.02",
                discount_days=10,
                net_days=30,
            ),
            line_items=[self._line()],
        )
        assert c.discount_term is not None
        assert c.discount_term.net_days == 30

    def test_invalid_missing_contract_reference(self):
        with pytest.raises(ValidationError):
            ContractCreate(vendor_id="v", line_items=[self._line()])

    def test_invalid_empty_line_items(self):
        with pytest.raises(ValidationError):
            ContractCreate(
                contract_reference="CTR-1", vendor_id="v", line_items=[]
            )

    def test_invalid_blank_reference(self):
        with pytest.raises(ValidationError):
            ContractCreate(contract_reference="  ", vendor_id="v", line_items=[self._line()])

    def test_invalid_negative_line_price(self):
        with pytest.raises(ValidationError):
            ContractCreate(
                contract_reference="CTR-1",
                vendor_id="v",
                line_items=[
                    ContractLineItemCreate(line_number=1, description="X", unit_price="-1")
                ],
            )


# ===========================================================================
# InvoiceLineItem + Invoice
# ===========================================================================


class TestInvoiceLineItemCreate:

    def test_valid(self):
        item = InvoiceLineItemCreate(**_invoice_line())
        assert item.amount == Decimal("380.00")

    def test_invalid_zero_qty(self):
        with pytest.raises(ValidationError):
            InvoiceLineItemCreate(
                line_number=1, description="X", qty="0", unit_price="10", amount="0"
            )

    def test_invalid_amount_mismatch(self):
        """amount must equal qty × unit_price within ±2 cents."""
        with pytest.raises(ValidationError):
            InvoiceLineItemCreate(
                line_number=1, description="X", qty="10", unit_price="38.00", amount="999.00"
            )

    def test_invalid_missing_description(self):
        with pytest.raises(ValidationError):
            InvoiceLineItemCreate(line_number=1, qty="1", unit_price="10", amount="10")

    def test_invalid_negative_amount(self):
        with pytest.raises(ValidationError):
            InvoiceLineItemCreate(
                line_number=1, description="X", qty="1", unit_price="10", amount="-1"
            )


class TestInvoiceCreate:

    def test_valid_full(self):
        inv = InvoiceCreate(**_valid_invoice_payload())
        assert inv.invoice_number == "INV-001"
        assert inv.extraction_status == ExtractionStatus.EXTRACTED

    def test_invalid_missing_vendor_name(self):
        payload = _valid_invoice_payload()
        del payload["vendor_name"]
        with pytest.raises(ValidationError) as exc:
            InvoiceCreate(**payload)
        assert "vendor_name" in str(exc.value)

    def test_invalid_missing_grand_total(self):
        payload = _valid_invoice_payload()
        del payload["grand_total"]
        with pytest.raises(ValidationError) as exc:
            InvoiceCreate(**payload)
        assert "grand_total" in str(exc.value)

    def test_invalid_missing_po_reference(self):
        payload = _valid_invoice_payload()
        del payload["po_reference"]
        with pytest.raises(ValidationError) as exc:
            InvoiceCreate(**payload)
        assert "po_reference" in str(exc.value)

    def test_invalid_due_date_before_invoice_date(self):
        with pytest.raises(ValidationError):
            InvoiceCreate(**_valid_invoice_payload(
                invoice_date=date(2026, 3, 1),
                due_date=date(2026, 2, 1),
            ))

    def test_invalid_grand_total_less_than_subtotal(self):
        with pytest.raises(ValidationError):
            InvoiceCreate(**_valid_invoice_payload(subtotal="500.00", grand_total="100.00"))

    def test_invalid_zero_grand_total(self):
        with pytest.raises(ValidationError):
            InvoiceCreate(**_valid_invoice_payload(grand_total="0"))

    def test_invalid_empty_line_items(self):
        with pytest.raises(ValidationError):
            InvoiceCreate(**_valid_invoice_payload(line_items=[]))

    def test_invalid_blank_invoice_number(self):
        with pytest.raises(ValidationError):
            InvoiceCreate(**_valid_invoice_payload(invoice_number="   "))

    def test_valid_invoice_received(self):
        rec = InvoiceReceived(invoice_number="INV-002")
        assert rec.extraction_status == ExtractionStatus.PENDING
        assert rec.invoice_status == InvoiceStatus.RECEIVED



# ===========================================================================
# MatchResult + LineItemMatchDetail
# ===========================================================================


class TestLineItemMatchDetail:

    def _valid(self) -> dict:
        return {
            "line_number": 1,
            "billed_qty": "10", "ordered_qty": "10", "qty_variance_abs": "0",
            "qty_match": True,
            "billed_unit_price": "42", "contract_unit_price": "38",
            "price_variance_abs": "4", "price_variance_pct": "0.1053",
            "price_match": False,
        }

    def test_valid(self):
        d = LineItemMatchDetail(**self._valid())
        assert d.price_match is False
        assert d.qty_match is True

    def test_invalid_missing_line_number(self):
        data = self._valid()
        del data["line_number"]
        with pytest.raises(ValidationError) as exc:
            LineItemMatchDetail(**data)
        assert "line_number" in str(exc.value)

    def test_invalid_non_numeric_variance(self):
        data = self._valid()
        data["price_variance_abs"] = "not-a-number"
        with pytest.raises(ValidationError):
            LineItemMatchDetail(**data)

    def test_invalid_line_number_zero(self):
        data = self._valid()
        data["line_number"] = 0
        with pytest.raises(ValidationError):
            LineItemMatchDetail(**data)


class TestMatchResultCreate:

    def _valid(self) -> dict:
        return {
            "invoice_id": "inv-uuid-1",
            "vendor_known": True,
            "po_resolved": True,
            "contract_resolved": True,
            "quantities_match": True,
            "prices_match": False,
            "total_matches": True,
            "approval_satisfied": True,
            "overall_passed": False,
        }

    def test_valid_all_pass(self):
        data = self._valid()
        data.update({"prices_match": True, "overall_passed": True})
        m = MatchResultCreate(**data)
        assert m.overall_passed is True

    def test_valid_price_fail(self):
        m = MatchResultCreate(**self._valid())
        assert m.prices_match is False
        assert m.overall_passed is False

    def test_valid_with_variance_fields(self):
        data = self._valid()
        data["total_variance_abs"] = "5.00"
        data["total_variance_pct"] = "0.013"
        m = MatchResultCreate(**data)
        assert m.total_variance_abs == Decimal("5.00")

    def test_invalid_missing_invoice_id(self):
        data = self._valid()
        del data["invoice_id"]
        with pytest.raises(ValidationError) as exc:
            MatchResultCreate(**data)
        assert "invoice_id" in str(exc.value)

    def test_invalid_missing_overall_passed(self):
        data = self._valid()
        del data["overall_passed"]
        with pytest.raises(ValidationError):
            MatchResultCreate(**data)

    def test_invalid_non_numeric_variance(self):
        data = self._valid()
        data["total_variance_abs"] = "abc"
        with pytest.raises(ValidationError):
            MatchResultCreate(**data)


# ===========================================================================
# ExceptionRecord
# ===========================================================================


class TestExceptionReasonSchema:

    def test_valid(self):
        r = ExceptionReasonSchema(
            reason_code=ExceptionReasonCode.PRICE_VARIANCE,
            supporting_data={"billed": 42, "contract": 38, "delta": 4},
        )
        assert r.reason_code == ExceptionReasonCode.PRICE_VARIANCE

    def test_invalid_bad_reason_code(self):
        with pytest.raises(ValidationError):
            ExceptionReasonSchema(reason_code="MADE_UP_CODE")

    def test_valid_empty_supporting_data(self):
        r = ExceptionReasonSchema(reason_code=ExceptionReasonCode.UNKNOWN_VENDOR)
        assert r.supporting_data == {}


class TestExceptionRecordCreate:

    def _reason(self, code=ExceptionReasonCode.PRICE_VARIANCE):
        return ExceptionReasonSchema(
            reason_code=code,
            supporting_data={"billed": 42, "contract": 38},
        )

    def test_valid_open(self):
        rec = ExceptionRecordCreate(
            invoice_id="inv-1",
            reasons=[self._reason()],
        )
        assert rec.status == ExceptionStatus.OPEN

    def test_valid_multiple_reasons(self):
        rec = ExceptionRecordCreate(
            invoice_id="inv-1",
            reasons=[
                self._reason(ExceptionReasonCode.PRICE_VARIANCE),
                self._reason(ExceptionReasonCode.MISSING_APPROVAL),
            ],
        )
        assert len(rec.reasons) == 2

    def test_invalid_missing_invoice_id(self):
        with pytest.raises(ValidationError) as exc:
            ExceptionRecordCreate(reasons=[self._reason()])
        assert "invoice_id" in str(exc.value)

    def test_invalid_empty_reasons(self):
        with pytest.raises(ValidationError):
            ExceptionRecordCreate(invoice_id="inv-1", reasons=[])

    def test_invalid_non_open_status_on_create(self):
        with pytest.raises(ValidationError):
            ExceptionRecordCreate(
                invoice_id="inv-1",
                reasons=[self._reason()],
                status=ExceptionStatus.RESOLVED,
            )

    def test_invalid_resolved_without_actor(self):
        """RESOLVED status requires actor_id and human_action."""
        from models.exception_record import ExceptionRecordBase
        with pytest.raises(ValidationError):
            ExceptionRecordBase(
                invoice_id="inv-1",
                reasons=[self._reason()],
                status=ExceptionStatus.RESOLVED,
                human_action=HumanAction.APPROVE_OVERRIDE,
                actor_id=None,   # missing
            )

    def test_invalid_resolved_without_human_action(self):
        from models.exception_record import ExceptionRecordBase
        with pytest.raises(ValidationError):
            ExceptionRecordBase(
                invoice_id="inv-1",
                reasons=[self._reason()],
                status=ExceptionStatus.RESOLVED,
                human_action=None,  # missing
                actor_id="reviewer@company.com",
            )


class TestHumanResolutionUpdate:

    def test_valid_approve_override(self):
        h = HumanResolutionUpdate(
            human_action=HumanAction.APPROVE_OVERRIDE,
            actor_id="ap-clerk@company.com",
            resolution_notes="Approved per controller email.",
        )
        assert h.human_action == HumanAction.APPROVE_OVERRIDE

    def test_valid_reject(self):
        h = HumanResolutionUpdate(
            human_action=HumanAction.REJECT,
            actor_id="ap-clerk@company.com",
        )
        assert h.resolution_notes is None

    def test_invalid_missing_actor_id(self):
        with pytest.raises(ValidationError) as exc:
            HumanResolutionUpdate(human_action=HumanAction.REJECT)
        assert "actor_id" in str(exc.value)

    def test_invalid_blank_actor_id(self):
        with pytest.raises(ValidationError):
            HumanResolutionUpdate(
                human_action=HumanAction.REJECT, actor_id="   "
            )

    def test_invalid_bad_action(self):
        with pytest.raises(ValidationError):
            HumanResolutionUpdate(human_action="MAYBE", actor_id="user@x.com")


# ===========================================================================
# PaymentSchedule
# ===========================================================================


class TestPaymentScheduleCreate:

    def test_valid_no_discount(self):
        ps = PaymentScheduleCreate(
            invoice_id="inv-1",
            scheduled_date=date(2026, 2, 14),
            amount="418.00",
            discount_taken=False,
        )
        assert ps.discount_taken is False
        assert ps.discount_amount is None

    def test_valid_with_discount(self):
        ps = PaymentScheduleCreate(
            invoice_id="inv-1",
            scheduled_date=date(2026, 1, 25),
            amount="409.64",
            discount_taken=True,
            discount_amount="8.36",
        )
        assert ps.discount_taken is True
        assert ps.discount_amount == Decimal("8.36")

    def test_invalid_missing_invoice_id(self):
        with pytest.raises(ValidationError) as exc:
            PaymentScheduleCreate(
                scheduled_date=date(2026, 2, 14), amount="418.00"
            )
        assert "invoice_id" in str(exc.value)

    def test_invalid_zero_amount(self):
        with pytest.raises(ValidationError):
            PaymentScheduleCreate(
                invoice_id="inv-1", scheduled_date=date(2026, 2, 14), amount="0"
            )

    def test_invalid_negative_amount(self):
        with pytest.raises(ValidationError):
            PaymentScheduleCreate(
                invoice_id="inv-1", scheduled_date=date(2026, 2, 14), amount="-100"
            )

    def test_invalid_discount_taken_without_amount(self):
        """discount_taken=True requires discount_amount > 0."""
        with pytest.raises(ValidationError):
            PaymentScheduleCreate(
                invoice_id="inv-1",
                scheduled_date=date(2026, 1, 25),
                amount="418.00",
                discount_taken=True,
                discount_amount=None,
            )

    def test_invalid_missing_scheduled_date(self):
        with pytest.raises(ValidationError):
            PaymentScheduleCreate(invoice_id="inv-1", amount="418.00")


# ===========================================================================
# DiscountRecommendation
# ===========================================================================


class TestDiscountRecommendationCreate:

    def _valid_take(self) -> dict:
        return {
            "invoice_id": "inv-1",
            "invoice_amount": "418.00",
            "discount_pct": "0.02",
            "discount_days": 10,
            "net_days": 30,
            "discount_amount": "8.36",
            "annualized_return": "0.3735",
            "hurdle_rate": "0.10",
            "recommendation": DiscountRecommendation.TAKE_DISCOUNT,
            "discount_date": date(2026, 1, 25),
        }

    def test_valid_take_discount(self):
        dr = DiscountRecommendationCreate(**self._valid_take())
        assert dr.recommendation == DiscountRecommendation.TAKE_DISCOUNT
        assert dr.annualized_return == Decimal("0.3735")

    def test_valid_hold_to_net(self):
        data = self._valid_take()
        data.update({
            "discount_pct": "0.005",
            "annualized_return": "0.0917",
            "hurdle_rate": "0.15",
            "recommendation": DiscountRecommendation.HOLD_TO_NET,
            "discount_days": 10,
            "net_days": 15,
        })
        dr = DiscountRecommendationCreate(**data)
        assert dr.recommendation == DiscountRecommendation.HOLD_TO_NET

    def test_valid_window_missed(self):
        dr = DiscountRecommendationCreate(
            invoice_id="inv-1",
            invoice_amount="418.00",
            hurdle_rate="0.10",
            recommendation=DiscountRecommendation.WINDOW_MISSED,
            window_missed=True,
        )
        assert dr.window_missed is True

    def test_valid_no_discount_term(self):
        dr = DiscountRecommendationCreate(
            invoice_id="inv-1",
            invoice_amount="418.00",
            hurdle_rate="0.10",
            recommendation=DiscountRecommendation.NO_DISCOUNT,
        )
        assert dr.discount_pct is None

    def test_invalid_missing_invoice_id(self):
        data = self._valid_take()
        del data["invoice_id"]
        with pytest.raises(ValidationError) as exc:
            DiscountRecommendationCreate(**data)
        assert "invoice_id" in str(exc.value)

    def test_invalid_missing_invoice_amount(self):
        data = self._valid_take()
        del data["invoice_amount"]
        with pytest.raises(ValidationError) as exc:
            DiscountRecommendationCreate(**data)
        assert "invoice_amount" in str(exc.value)

    def test_invalid_partial_discount_term(self):
        """All three discount term fields must be set together or all None."""
        data = self._valid_take()
        del data["net_days"]   # only discount_pct + discount_days set → invalid
        with pytest.raises(ValidationError):
            DiscountRecommendationCreate(**data)

    def test_invalid_window_missed_wrong_recommendation(self):
        with pytest.raises(ValidationError):
            DiscountRecommendationCreate(
                invoice_id="inv-1",
                invoice_amount="418.00",
                hurdle_rate="0.10",
                recommendation=DiscountRecommendation.TAKE_DISCOUNT,  # wrong
                window_missed=True,
            )

    def test_invalid_hurdle_rate_zero(self):
        with pytest.raises(ValidationError):
            DiscountRecommendationCreate(
                invoice_id="inv-1",
                invoice_amount="418.00",
                hurdle_rate="0",
                recommendation=DiscountRecommendation.NO_DISCOUNT,
            )


# ===========================================================================
# AuditEvent
# ===========================================================================


class TestAuditEventCreate:

    def test_valid_system_event(self):
        ev = AuditEventCreate(
            invoice_id="inv-1",
            event_type=AuditEventType.EXTRACTION_SUCCEEDED,
            invoice_number="INV-001",
            vendor_name="Acme Corp",
            po_reference="PO-100",
        )
        assert ev.event_type == AuditEventType.EXTRACTION_SUCCEEDED
        assert ev.actor_id is None

    def test_valid_human_event(self):
        ev = AuditEventCreate(
            invoice_id="inv-1",
            event_type=AuditEventType.HUMAN_OVERRIDE_APPROVED,
            actor_id="ap-clerk@company.com",
            payload_json='{"reason": "approved per email"}',
        )
        assert ev.actor_id == "ap-clerk@company.com"

    def test_invalid_missing_invoice_id(self):
        with pytest.raises(ValidationError) as exc:
            AuditEventCreate(event_type=AuditEventType.INVOICE_RECEIVED)
        assert "invoice_id" in str(exc.value)

    def test_invalid_missing_event_type(self):
        with pytest.raises(ValidationError) as exc:
            AuditEventCreate(invoice_id="inv-1")
        assert "event_type" in str(exc.value)

    def test_invalid_bad_event_type(self):
        with pytest.raises(ValidationError):
            AuditEventCreate(invoice_id="inv-1", event_type="INVENTED_EVENT")

    def test_invalid_blank_actor_id(self):
        with pytest.raises(ValidationError):
            AuditEventCreate(
                invoice_id="inv-1",
                event_type=AuditEventType.HUMAN_REJECTED,
                actor_id="   ",
            )

    def test_no_update_schema_exists(self):
        """
        Confirm there is no AuditEventUpdate class — append-only enforcement
        (spec.md §4, FR-6.3).
        """
        import models.audit_event as ae_module
        assert not hasattr(ae_module, "AuditEventUpdate"), (
            "AuditEventUpdate must not exist — audit log is append-only."
        )
