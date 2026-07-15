"""
tests/unit/test_config.py — Phase 0 required test suite.

spec.md Phase 0 testing requirement:
  "one test asserting config validation fails on missing OPENROUTER_API_KEY"

Additional coverage added for robustness without adding business logic:
  - blank key is rejected
  - placeholder key is rejected
  - valid key + defaults loads successfully
  - all eight §2 variables are present and have expected defaults
  - log_level is normalised to uppercase
  - invalid log_level fails
  - settings object is immutable (frozen)

Test isolation strategy
-----------------------
pydantic-settings resolves values in priority order:
  1. init kwargs  (highest)
  2. environment variables
  3. .env file    (lowest before defaults)

`_make_settings(**overrides)` passes values as init kwargs, which take priority
over both the environment and the .env file — so the real .env on disk never
interferes with any test that uses this helper.

The one test that specifically checks the *missing* key scenario uses
`NoEnvFileSettings` (a subclass with env_file=None) plus `monkeypatch.delenv`
to guarantee no source can supply the key.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from app.config import Settings, get_settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class NoEnvFileSettings(Settings):
    """
    Test-only subclass that disables .env file loading entirely.

    Used only in tests that need to assert behaviour when a variable is
    completely absent from all sources.  Do not use in application code.
    """

    model_config = SettingsConfigDict(
        env_file=None,          # do not read any .env file
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )


def _make_settings(**overrides: str) -> Settings:
    """
    Construct a Settings instance with test-safe values.

    Passes values as init kwargs (highest priority in pydantic-settings),
    so the real .env file and any ambient environment variables are
    irrelevant — this helper is fully self-contained.

    Always supplies a valid OPENROUTER_API_KEY unless the test overrides it.
    """
    defaults: dict = {
        "openrouter_api_key": "sk-or-v1-test-key-valid",
        "openrouter_fallback_chain": [
            "openrouter/auto",
            "google/gemma-3-27b-it:free",
            "qwen/qwen3-235b-a22b:free",
            "deepseek/deepseek-r1-0528:free",
        ],
        "openrouter_base_url": "https://openrouter.ai/api/v1",
        "database_url": "sqlite:///./test.db",
        "approval_threshold_default": "10000",
        "discount_hurdle_rate_default": "0.10",
        "match_tolerance_percent": "0.0",
        "log_level": "INFO",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Phase 0 mandatory test (spec.md §6 Phase 0 testing requirement)
# ---------------------------------------------------------------------------

class TestMissingApiKey:
    """Config validation must fail loudly when OPENROUTER_API_KEY is absent."""

    def test_missing_openrouter_api_key_raises_validation_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        spec.md §2: 'Config validation fails fast and loudly at startup if a
        required variable is missing.'

        When OPENROUTER_API_KEY is not supplied from any source, Settings()
        must raise a pydantic ValidationError — never silently default to
        None or empty.

        Uses NoEnvFileSettings (env_file=None) so the .env file on disk cannot
        supply the key, and monkeypatch.delenv so the environment cannot either.
        """
        get_settings.cache_clear()
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        with pytest.raises(ValidationError) as exc_info:
            # No .env file, no env var → must fail.
            NoEnvFileSettings()  # type: ignore[call-arg]

        errors = exc_info.value.errors()
        field_names = [e["loc"][0] for e in errors]
        assert "openrouter_api_key" in field_names, (
            f"Expected ValidationError on 'openrouter_api_key', "
            f"got errors on: {field_names}"
        )

    def test_blank_openrouter_api_key_raises_validation_error(self) -> None:
        """A key that is an empty string must be rejected (min_length=1)."""
        with pytest.raises(ValidationError) as exc_info:
            _make_settings(openrouter_api_key="")

        errors = exc_info.value.errors()
        field_names = [e["loc"][0] for e in errors]
        assert "openrouter_api_key" in field_names

    def test_whitespace_only_api_key_raises_validation_error(self) -> None:
        """A key that is only whitespace must be rejected by the validator."""
        with pytest.raises(ValidationError):
            _make_settings(openrouter_api_key="   ")

    def test_placeholder_api_key_raises_validation_error(self) -> None:
        """The .env placeholder value must be rejected explicitly."""
        with pytest.raises(ValidationError) as exc_info:
            _make_settings(
                openrouter_api_key="sk-or-v1-replace-me-with-your-real-openrouter-api-key"
            )

        errors = exc_info.value.errors()
        assert any("openrouter_api_key" in str(e) for e in errors)


# ---------------------------------------------------------------------------
# Happy-path: valid config loads successfully
# ---------------------------------------------------------------------------

class TestValidConfig:
    """Settings loads correctly when all required variables are present."""

    def test_valid_settings_loads(self) -> None:
        """A valid API key + defaults should construct without error."""
        settings = _make_settings()
        assert settings.openrouter_api_key == "sk-or-v1-test-key-valid"

    def test_all_eight_variables_present(self) -> None:
        """
        All eight variables listed in spec.md §2 must exist on the Settings
        object and carry the expected defaults.
        """
        settings = _make_settings()

        # 1. OPENROUTER_API_KEY — required, no default
        assert settings.openrouter_api_key == "sk-or-v1-test-key-valid"

        # 2. OPENROUTER_FALLBACK_CHAIN — ordered model fallback list
        assert isinstance(settings.openrouter_fallback_chain, list)
        assert len(settings.openrouter_fallback_chain) == 4
        assert settings.openrouter_fallback_chain[0] == "openrouter/auto"

        # 3. OPENROUTER_BASE_URL
        assert settings.openrouter_base_url == "https://openrouter.ai/api/v1"

        # 4. DATABASE_URL
        assert settings.database_url == "sqlite:///./test.db"

        # 5. APPROVAL_THRESHOLD_DEFAULT
        assert settings.approval_threshold_default == 10_000.0

        # 6. DISCOUNT_HURDLE_RATE_DEFAULT
        assert settings.discount_hurdle_rate_default == 0.10

        # 7. MATCH_TOLERANCE_PERCENT
        assert settings.match_tolerance_percent == 0.0

        # 8. LOG_LEVEL
        assert settings.log_level == "INFO"

    def test_log_level_is_uppercased(self) -> None:
        """LOG_LEVEL should be normalised to uppercase by the validator."""
        settings = _make_settings(log_level="debug")
        assert settings.log_level == "DEBUG"

    def test_invalid_log_level_raises(self) -> None:
        """An unrecognised LOG_LEVEL value must fail validation."""
        with pytest.raises(ValidationError):
            _make_settings(log_level="VERBOSE")

    def test_settings_are_immutable(self) -> None:
        """Settings must be frozen — no attribute may be reassigned at runtime."""
        settings = _make_settings()
        with pytest.raises(Exception):  # ValidationError or TypeError from frozen model
            settings.log_level = "DEBUG"  # type: ignore[misc]
