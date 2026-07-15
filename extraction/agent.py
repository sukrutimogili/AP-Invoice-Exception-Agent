"""
extraction/agent.py — Extraction agent with retry-and-repair loop.

Implements spec.md §1 model-portability rule exactly:
  1. Prompt for JSON matching the explicit schema (prompt describes schema).
  2. Parse defensively (strip markdown fences, attempt JSON repair).
  3. Validate against the Pydantic InvoiceCreate model.
  4. On validation failure, retry ONCE with the validation errors fed back
     ("your last output failed validation for these reasons: ... fix and resend").
  5. On second failure, fail closed → NEEDS_REEXTRACTION.
     Never fabricate a missing field.

The agent depends on LLMClient (Protocol) — inject any implementation.
Use OpenRouterClient in production; use a mock in unit tests.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import ValidationError

from extraction.llm_client import LLMCallError, LLMClient
from extraction.parser import ParseError, parse_llm_response
from extraction.schemas import (
    ExtractionFailure,
    ExtractionResult,
    ExtractionSuccess,
    FailureReason,
)
from models.invoice import InvoiceCreate

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------

_PROMPT_DIR = Path(__file__).parent / "prompts"
_PROMPT_V1_PATH = _PROMPT_DIR / "v1_extract.md"

# Sentinel that separates the system instructions from the invoice-text slot.
_INVOICE_PLACEHOLDER = "{{ invoice_text }}"


def _load_prompt_template(path: Path = _PROMPT_V1_PATH) -> str:
    """Load the versioned prompt template from disk."""
    return path.read_text(encoding="utf-8")


def _build_system_prompt(template: str) -> str:
    """
    Return the portion of the prompt before the invoice-text placeholder.

    This is the system message — it contains all instructions and the schema.
    The invoice text itself is the user message.
    """
    idx = template.find(_INVOICE_PLACEHOLDER)
    if idx == -1:
        # Fallback: use the whole template as the system prompt.
        return template
    return template[:idx].strip()


def _build_retry_user_message(
    original_invoice_text: str,
    raw_llm_output: str,
    validation_errors: str,
) -> str:
    """
    Build the user message for the retry attempt.

    Feeds the validation errors back into the prompt so the model can correct
    its output (spec.md §1, step 4).
    """
    return (
        "Your previous response failed validation. "
        "Here are the specific errors:\n\n"
        f"{validation_errors}\n\n"
        "Your previous (invalid) response was:\n"
        f"{raw_llm_output}\n\n"
        "Please re-read the original invoice text below and return corrected JSON "
        "that satisfies all the required fields. "
        "Return ONLY the JSON object — no explanation, no markdown fences.\n\n"
        f"INVOICE TEXT:\n{original_invoice_text}"
    )


# ---------------------------------------------------------------------------
# Validation helper
# ---------------------------------------------------------------------------


def _validate_invoice(data: dict) -> InvoiceCreate:
    """
    Validate the parsed dict against InvoiceCreate.

    Raises:
        ValidationError: if any field is missing or fails validation.
    """
    return InvoiceCreate(**data)


def _format_validation_errors(exc: ValidationError) -> str:
    """
    Format Pydantic ValidationError into a concise human-readable string
    suitable for feeding back to the LLM.
    """
    lines = []
    for error in exc.errors():
        loc = " → ".join(str(l) for l in error["loc"])
        msg = error["msg"]
        lines.append(f"  Field '{loc}': {msg}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class ExtractionAgent:
    """
    Extracts structured invoice data from raw text using an LLM.

    Implements the full model-portability rule from spec.md §1:
      attempt 1 → parse → validate → success
                                   → failure → attempt 2 (with error feedback)
                                                          → success
                                                          → NEEDS_REEXTRACTION

    Args:
        llm_client: Any object satisfying the LLMClient Protocol.
        prompt_template_path: Optional override for the prompt file path
                               (used in tests to swap templates).
    """

    def __init__(
        self,
        llm_client: LLMClient,
        prompt_template_path: Path | None = None,
    ) -> None:
        self._client = llm_client
        template_path = prompt_template_path or _PROMPT_V1_PATH
        self._template = _load_prompt_template(template_path)
        self._system_prompt = _build_system_prompt(self._template)

    def extract(self, invoice_text: str) -> ExtractionResult:
        """
        Extract structured invoice data from raw text.

        Args:
            invoice_text: The raw invoice document text (untrusted input).

        Returns:
            ExtractionSuccess if a valid InvoiceCreate is produced.
            ExtractionFailure (NEEDS_REEXTRACTION) if both attempts fail.
        """
        logger.info("Extraction started", extra={"text_length": len(invoice_text)})

        # ---------------------------------------------------------------
        # Attempt 1
        # ---------------------------------------------------------------
        raw1, parse_err1, val_err1 = self._attempt(invoice_text)

        if parse_err1 is None and val_err1 is None and raw1 is not None:
            # Attempt 1 succeeded — parse and validate were both clean.
            try:
                parsed = parse_llm_response(raw1)
                invoice = _validate_invoice(parsed)
                logger.info("Extraction succeeded on attempt 1.")
                return ExtractionSuccess(
                    invoice=invoice,
                    raw_payload=raw1,
                    attempt_count=1,
                )
            except (ParseError, ValidationError):
                # Should not reach here (covered by _attempt), but be safe.
                pass

        # If attempt 1 produced a LLM call error, fail immediately.
        if isinstance(parse_err1, str) and parse_err1.startswith("LLM_CALL_FAILED"):
            return ExtractionFailure(
                reason=FailureReason.LLM_CALL_FAILED,
                error_detail=parse_err1,
                raw_payload=None,
                attempt_count=1,
            )

        # ---------------------------------------------------------------
        # Attempt 2 — retry with validation errors fed back
        # ---------------------------------------------------------------
        logger.info(
            "Attempt 1 failed — retrying with error feedback.",
            extra={"parse_err": str(parse_err1), "val_err": str(val_err1)},
        )
        error_summary = str(val_err1 or parse_err1 or "Unknown error on attempt 1")
        retry_user_msg = _build_retry_user_message(
            original_invoice_text=invoice_text,
            raw_llm_output=raw1 or "(no output)",
            validation_errors=error_summary,
        )

        raw2, parse_err2, val_err2 = self._attempt_with_message(retry_user_msg)

        if parse_err2 is None and val_err2 is None and raw2 is not None:
            try:
                parsed2 = parse_llm_response(raw2)
                invoice2 = _validate_invoice(parsed2)
                logger.info("Extraction succeeded on attempt 2 (after retry).")
                return ExtractionSuccess(
                    invoice=invoice2,
                    raw_payload=raw2,
                    attempt_count=2,
                )
            except (ParseError, ValidationError):
                pass

        # ---------------------------------------------------------------
        # Both attempts failed → NEEDS_REEXTRACTION (spec.md §1, step 5)
        # ---------------------------------------------------------------
        logger.warning("Extraction failed after 2 attempts — NEEDS_REEXTRACTION.")

        # Determine the most specific reason code.
        if val_err2 is not None:
            reason = FailureReason.RETRY_VALIDATION_FAILED
            detail = _format_validation_errors(val_err2) if isinstance(val_err2, ValidationError) else str(val_err2)
        elif parse_err2 is not None:
            reason = FailureReason.UNPARSEABLE_JSON
            detail = str(parse_err2)
        elif val_err1 is not None:
            reason = FailureReason.SCHEMA_VALIDATION_FAILED
            detail = _format_validation_errors(val_err1) if isinstance(val_err1, ValidationError) else str(val_err1)
        else:
            reason = FailureReason.UNPARSEABLE_JSON
            detail = str(parse_err1 or "Unknown parse error")

        return ExtractionFailure(
            reason=reason,
            error_detail=detail,
            raw_payload=raw2 or raw1,
            attempt_count=2,
        )

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _attempt(
        self, invoice_text: str
    ) -> tuple[str | None, str | None, ValidationError | None]:
        """
        Run attempt 1: call LLM with system prompt + invoice text as user msg.

        Returns (raw_response, parse_error_str, validation_error).
        raw_response is None on LLM call failure.
        parse_error_str is set on LLM call failure or parse failure.
        validation_error is set on Pydantic validation failure.
        """
        try:
            raw = self._client.complete(
                system_prompt=self._system_prompt,
                user_message=invoice_text,
            )
        except LLMCallError as exc:
            return None, f"LLM_CALL_FAILED: {exc}", None

        return self._parse_and_validate(raw)

    def _attempt_with_message(
        self, user_message: str
    ) -> tuple[str | None, str | None, ValidationError | None]:
        """
        Run attempt 2: call LLM with system prompt + retry user message.
        """
        try:
            raw = self._client.complete(
                system_prompt=self._system_prompt,
                user_message=user_message,
            )
        except LLMCallError as exc:
            return None, f"LLM_CALL_FAILED: {exc}", None

        return self._parse_and_validate(raw)

    def _parse_and_validate(
        self, raw: str
    ) -> tuple[str | None, str | None, ValidationError | None]:
        """
        Parse raw LLM output and validate against InvoiceCreate.

        Returns (raw, parse_error_str, validation_error).
        On success: (raw, None, None).
        On parse failure: (raw, error_str, None).
        On validation failure: (raw, None, ValidationError).
        """
        try:
            data = parse_llm_response(raw)
        except ParseError as exc:
            return raw, str(exc), None

        try:
            _validate_invoice(data)
            # If validate passes, signal clean to the caller who will re-call
            # to get the actual model instance (avoids double-construction).
            return raw, None, None
        except ValidationError as exc:
            return raw, None, exc
