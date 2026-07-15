"""
app/main.py — FastAPI application entry point for LedgerGate-Agent.

Phase 9 scope (complete API surface):
  - Config is validated at startup via get_settings() (fails fast if env is bad).
  - /health endpoint returns 200 + status payload.
  - Invoice ingestion:   POST /invoices/submit, POST /invoices/upload  (api/invoices.py)
  - Payment schedules:   GET  /payments/{id},   GET  /payments/        (api/payments.py)
  - Exception human-gate: POST /exceptions/{id}/approve,
                           POST /exceptions/{id}/reject,
                           GET  /exceptions/{id}                        (api/exceptions.py)
  - Audit trail:         GET  /audit/invoice/{id},
                          GET  /audit/search,
                          GET  /audit/invoice/{id}/outcome              (api/audit.py)

Run with:
    uvicorn app.main:app --reload

OpenAPI docs:
    http://localhost:8000/docs   (Swagger UI)
    http://localhost:8000/redoc  (ReDoc)
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import get_settings
from api.audit import router as audit_router
from api.exceptions import router as exceptions_router
from api.invoices import router as invoices_router
from api.payments import router as payments_router

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Startup validation — fail-fast gate (spec.md §2).
# ---------------------------------------------------------------------------
_settings = get_settings()

app = FastAPI(
    title="LedgerGate-Agent",
    description=(
        "AP Invoice & Contract Exception Agent — "
        "extracts, matches, routes, and audits invoices against PO and contract terms.\n\n"
        "## Endpoints\n\n"
        "| Group | Path | Method | Description |\n"
        "|---|---|---|---|\n"
        "| Invoices | `/invoices/submit` | POST | Submit pre-extracted invoice |\n"
        "| Invoices | `/invoices/upload` | POST | Upload raw text invoice document |\n"
        "| Payments | `/payments/` | GET | List all payment schedules |\n"
        "| Payments | `/payments/{invoice_id}` | GET | Get schedule for one invoice |\n"
        "| Exceptions | `/exceptions/{invoice_id}` | GET | Get exception record |\n"
        "| Exceptions | `/exceptions/{invoice_id}/approve` | POST | Approve-override |\n"
        "| Exceptions | `/exceptions/{invoice_id}/reject` | POST | Reject to vendor |\n"
        "| Audit | `/audit/invoice/{invoice_id}` | GET | Full audit trail |\n"
        "| Audit | `/audit/search` | GET | Search audit events |\n"
        "| Audit | `/audit/invoice/{invoice_id}/outcome` | GET | Final outcome |\n"
        "| Ops | `/health` | GET | Liveness probe |\n\n"
        "**Authorization:** all endpoints except `/health` require an internal service "
        "token (TBD — Phase 9 deployment).  Pass in `Authorization: Bearer <token>`.\n\n"
        "**Rate limiting:** ingestion endpoints (`/invoices/*`) should be rate-limited "
        "at the reverse-proxy layer.  See endpoint descriptions for recommended limits."
    ),
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ---------------------------------------------------------------------------
# Mount routers
# ---------------------------------------------------------------------------
app.include_router(invoices_router)
app.include_router(payments_router)
app.include_router(exceptions_router)
app.include_router(audit_router)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get(
    "/health",
    summary="Health check",
    description=(
        "Returns HTTP 200 when the service is running and configuration has "
        "been validated successfully.  No authentication required — this "
        "endpoint is intended for load-balancer / liveness probes."
    ),
    tags=["ops"],
)
def health() -> JSONResponse:
    """
    Liveness probe.

    Authorization: none (internal ops endpoint).

    Returns a JSON payload confirming the service is up and which fallback
    chain / database are configured.  Secrets (API keys) are never included.
    """
    payload: dict[str, Any] = {
        "status": "ok",
        "service": "LedgerGate-Agent",
        "version": app.version,
        "config": {
            "openrouter_fallback_chain": _settings.openrouter_fallback_chain,
            "database_url": _settings.database_url,
            "log_level": _settings.log_level,
        },
    }
    logger.info("Health check requested", extra={"status": "ok"})
    return JSONResponse(content=payload, status_code=200)
