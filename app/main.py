"""
app/main.py — FastAPI application entry point for LedgerGate-Agent.

Phase 0 scope: skeleton only.
  - Config is validated at startup via get_settings() (fails fast if env is bad).
  - /health endpoint returns 200 + status payload.
  - No business logic, no domain models, no additional endpoints.

Run with:
    uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Startup validation — this is the fail-fast gate spec.md §2 requires.
# get_settings() will raise pydantic_core.ValidationError immediately if any
# required environment variable (e.g. OPENROUTER_API_KEY) is absent or invalid.
# The exception propagates before the server accepts any requests.
# ---------------------------------------------------------------------------
_settings = get_settings()

app = FastAPI(
    title="LedgerGate-Agent",
    description=(
        "AP Invoice & Contract Exception Agent — "
        "extracts, matches, routes, and audits invoices."
    ),
    version="0.1.0",
    # Disable the default /redoc and /docs in production via config later;
    # leave them on for Phase 0 development convenience.
    docs_url="/docs",
    redoc_url="/redoc",
)


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

    Returns a JSON payload confirming the service is up and which model /
    database are configured.  Secrets (API keys) are never included.
    """
    payload: dict[str, Any] = {
        "status": "ok",
        "service": "LedgerGate-Agent",
        "version": app.version,
        "config": {
            "openrouter_model": _settings.openrouter_model,
            "database_url": _settings.database_url,
            "log_level": _settings.log_level,
        },
    }
    logger.info("Health check requested", extra={"status": "ok"})
    return JSONResponse(content=payload, status_code=200)
