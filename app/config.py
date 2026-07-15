"""
app/config.py — centralised, fail-fast configuration for LedgerGate-Agent.

All application settings are loaded here, once, at import time via
pydantic-settings.  Every other module MUST import `get_settings()` from this
module instead of calling `os.getenv()` directly — see spec.md §2.

Validation is performed by Pydantic at construction time.  If a required
variable (e.g. OPENROUTER_API_KEY) is absent or invalid the process raises a
`ValidationError` immediately on startup rather than failing later at first use.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables / .env file.

    Required variables (no default → startup fails if absent):
      - OPENROUTER_API_KEY

    All other variables have safe defaults matching .env.example.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Extra fields in the env file are silently ignored rather than
        # raising an error, which keeps local overrides flexible.
        extra="ignore",
        # Freeze the instance after construction so settings are never
        # mutated at runtime.
        frozen=True,
    )

    # -----------------------------------------------------------------------
    # LLM Gateway — OpenRouter
    # -----------------------------------------------------------------------

    openrouter_api_key: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "OpenRouter API key.  Required.  "
                "Obtain one at https://openrouter.ai/keys"
            ),
        ),
    ]

    openrouter_fallback_chain: list[str] = Field(
        default=[
            "openrouter/auto",               # 1st: OpenRouter meta-router (picks best free model)
            "google/gemma-3-27b-it:free",    # 2nd: Gemma 3 27B
            "qwen/qwen3-235b-a22b:free",     # 3rd: Qwen3 235B A22B
            "deepseek/deepseek-r1-0528:free",  # 4th: DeepSeek R1 0528
        ],
        description=(
            "Ordered list of OpenRouter model slugs tried in sequence. "
            "Each model gets one automatic retry on 429 (respecting Retry-After) "
            "before the next model in the chain is attempted. "
            "Override via OPENROUTER_FALLBACK_CHAIN as a JSON array in .env."
        ),
    )

    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1",
        description="Base URL for the OpenRouter (OpenAI-compatible) API.",
    )

    # -----------------------------------------------------------------------
    # Database
    # -----------------------------------------------------------------------

    database_url: str = Field(
        default="sqlite:///./app.db",
        description=(
            "SQLAlchemy connection string.  "
            "Switch to a Postgres DSN to upgrade without any code changes."
        ),
    )

    # -----------------------------------------------------------------------
    # Business logic thresholds — spec.md §2 / requirements.md FR-2.4, FR-7
    # -----------------------------------------------------------------------

    approval_threshold_default: Annotated[
        float,
        Field(
            default=10_000.0,
            gt=0,
            description=(
                "Invoices at or above this amount require an approval on file "
                "before they are eligible for STP (FR-2.4)."
            ),
        ),
    ]

    discount_hurdle_rate_default: Annotated[
        float,
        Field(
            default=0.10,
            gt=0,
            lt=1,
            description=(
                "Minimum annualized return (as a decimal fraction) for an "
                "early-payment discount to be recommended (FR-7.2).  "
                "Example: 0.10 = 10 %."
            ),
        ),
    ]

    match_tolerance_percent: Annotated[
        float,
        Field(
            default=0.0,
            ge=0,
            description=(
                "Maximum allowable percentage variance between invoice and "
                "PO/contract values before a field is flagged as a mismatch "
                "(FR-2.2, FR-2.3).  0.0 = exact match required."
            ),
        ),
    ]

    # -----------------------------------------------------------------------
    # Observability
    # -----------------------------------------------------------------------

    log_level: str = Field(
        default="INFO",
        description="Python logging level: DEBUG | INFO | WARNING | ERROR | CRITICAL",
    )

    # -----------------------------------------------------------------------
    # Validators
    # -----------------------------------------------------------------------

    @field_validator("openrouter_api_key")
    @classmethod
    def _api_key_not_blank(cls, v: str) -> str:
        """
        Reject a key that is literally empty or the .env.example placeholder.
        The placeholder value is distinct enough that we fail loudly rather
        than letting an obviously wrong key reach the network.
        """
        stripped = v.strip()
        if not stripped:
            raise ValueError(
                "OPENROUTER_API_KEY must not be empty.  "
                "Set it in your .env file or as an environment variable."
            )
        if stripped == "sk-or-v1-replace-me-with-your-real-openrouter-api-key":
            raise ValueError(
                "OPENROUTER_API_KEY is still set to the placeholder value.  "
                "Replace it with your real OpenRouter API key."
            )
        return stripped

    @field_validator("log_level")
    @classmethod
    def _valid_log_level(cls, v: str) -> str:
        upper = v.upper()
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if upper not in valid:
            raise ValueError(
                f"LOG_LEVEL must be one of {valid!r}, got {v!r}"
            )
        return upper


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return the application settings singleton.

    Uses lru_cache so the .env file is parsed exactly once per process.
    The cache can be cleared in tests via ``get_settings.cache_clear()``.

    Raises:
        pydantic_core.ValidationError: on startup if any required variable is
            missing or fails validation — intentional fail-fast behaviour per
            spec.md §2.
    """
    settings = Settings()  # type: ignore[call-arg]
    # Configure the root logger immediately so every subsequent log call uses
    # the level from config.  This is the single place log level is set.
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logger.info(
        "Configuration loaded",
        extra={
            "openrouter_fallback_chain": settings.openrouter_fallback_chain,
            "database_url": settings.database_url,
            "log_level": settings.log_level,
        },
    )
    return settings
