"""
audit/__init__.py — Public API of the audit package.

All write functions and the query helpers are exported from here.
No other module should import from audit.writer directly.
"""

from audit.writer import (
    clear_audit_log,
    get_all_events,
    write_discount_evaluated,
    write_document_conflict_detected,
    write_exception_raised,
    write_extraction_failed,
    write_extraction_succeeded,
    write_human_override_approved,
    write_human_rejected,
    write_invoice_received,
    write_matching_completed,
    write_payment_scheduled,
    write_stp_approved,
    write_vendor_auto_created,
)

__all__ = [
    "clear_audit_log",
    "get_all_events",
    "write_discount_evaluated",
    "write_document_conflict_detected",
    "write_exception_raised",
    "write_extraction_failed",
    "write_extraction_succeeded",
    "write_human_override_approved",
    "write_human_rejected",
    "write_invoice_received",
    "write_matching_completed",
    "write_payment_scheduled",
    "write_stp_approved",
    "write_vendor_auto_created",
]
