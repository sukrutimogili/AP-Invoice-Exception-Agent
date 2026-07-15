"""
app/main.py — FastAPI application entry point for LedgerGate-Agent.

Current scope (Phase 4):
  - Config is validated at startup via get_settings() (fails fast if env is bad).
  - /health endpoint returns 200 + status payload.
  - Exception human-gate endpoints mounted at /exceptions (FR-4.3).

Run with:
    uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import get_settings
from api.audit import router as audit_router
from api.exceptions import router as exceptions_router

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Startup validation — fail-fast gate (spec.md §2).
# ---------------------------------------------------------------------------
_settings = get_settings()

app = FastAPI(
    title="LedgerGate-Agent",
    description=(
        "AP Invoice & Contract Exception Agent — "
        "extracts, matches, routes, and audits invoices."
    ),
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ---------------------------------------------------------------------------
# Mount routers
# ---------------------------------------------------------------------------
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
