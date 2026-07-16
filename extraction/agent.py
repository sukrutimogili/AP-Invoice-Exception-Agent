"""
extraction/agent.py — Schema-agnostic extraction agent with retry-and-repair loop.

Implements spec.md §1 model-portability rule exactly:
  1. Prompt for JSON matching the explicit schema (prompt describes schema).
  2. Parse defensively (strip markdown fences, attempt JSON repair).
  3. Validate against the target Pydantic model via the injected validator fn.
  4. On validation failure, retry ONCE with the validation errors fed back
     ("your last output failed validation for these reasons: ... fix and resend").
  5. On second failure, fail closed → NEEDS_REEXTRACTION.
     Never fabricate a missing field.

The agent is parameterized by:
  - SchemaT: the Pydantic model type produced on success.
  - ResultT: the ExtractionResult-compatible union returned to callers.
  - A validator callable: (dict) -> SchemaT, which raises ValidationError on failure.
  - A success factory: (model, raw_payload, attempt_count) -> ResultT.
  - A failure factory: (reason, detail, raw_payload, attempt_count) -> ResultT.
  - A prompt path and a document-text placeholder string.

This parameterization means InvoiceCreate, PurchaseOrderCreate, and ContractCreate
all share the identical retry/fail-closed machinery; only the schema, prompt, and
result types differ.

Thin subclasses / factory functions (InvoiceExtractionAgent, PurchaseOrderExtractionAgent,
ContractExtractionAgent) are defined at the bottom for convenient import.

The agent depends on LLMClient (Protocol) — inject any implementation.
Use OpenRouterClient in production; use a mock in unit tests.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Generic, TypeVar

from pydantic import BaseModel, ValidationError

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
# Type parameters
# ---------------------------------------------------------------------------

SchemaT = TypeVar("SchemaT", bound=BaseModel)
ResultT = TypeVar("ResultT")

# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------

_PROMPT_DIR = Path(__file__).parent / "prompts"

# Default paths for each document type.
_INVOICE_PROMPT_PATH = _PROMPT_DIR / "v1_extract.md"
_PO_PROMPT_PATH = _PROMPT_DIR / "v1_extract_po.md"
_CONTRACT_PROMPT_PATH = _PROMPT_DIR / "v1_extract_contract.md"

# Default invoice prompt placeholder (kept for backward compat in tests).
_INVOICE_PLACEHOLDER = "{{ invoice_text }}"


def _load_prompt_template(path: Path) -> str:
    """Load the versioned prompt template from disk."""
    return path.read_text(encoding="utf-8")


def _build_system_prompt(template: str, placeholder: str) -> str:
    """
    Return the portion of the prompt before the document-text placeholder.

    This is the system message — it contains all instructions and the schema.
    The document text itself is the user message.
    """
    idx = template.find(placeholder)
    if idx == -1:
        # Fallback: use the whole template as the system prompt.
        return template
    return template[:idx].strip()


def _build_retry_user_message(
    original_document_text: str,
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
        "Please re-read the original document text below and return corrected JSON "
        "that satisfies all the required fields. "
        "Return ONLY the JSON object — no explanation, no markdown fences.\n\n"
        f"DOCUMENT TEXT:\n{original_document_text}"
    )


# ---------------------------------------------------------------------------
# Validation helpers
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
# Generic base agent
# ---------------------------------------------------------------------------


class BaseExtractionAgent(Generic[SchemaT, ResultT]):
    """
    Generic extraction agent — schema-agnostic retry-and-repair loop.

    Subclasses (or factory-constructed instances) specify:
      - prompt_path: Path to the versioned prompt template.
      - doc_placeholder: The template placeholder for the document text
            (e.g. "{{ invoice_text }}", "{{ po_text }}").
      - validator: Callable[(dict) -> SchemaT] that raises ValidationError.
      - success_factory: Callable[(SchemaT, str, int) -> ResultT].
      - failure_factory: Callable[(FailureReason, str, str | None, int) -> ResultT].

    The retry-and-repair machinery is identical for every document type.

    Args:
        llm_client: Any object satisfying the LLMClient Protocol.
        prompt_path: Path to the prompt file (overrides subclass default when supplied).
    """

    # Subclasses set these as class-level defaults.
    _default_prompt_path: Path
    _default_doc_placeholder: str

    def __init__(
        self,
        llm_client: LLMClient,
        prompt_path: Path | None = None,
    ) -> None:
        self._client = llm_client
        effective_path = prompt_path or self._default_prompt_path
        self._template = _load_prompt_template(effective_path)
        self._system_prompt = _build_system_prompt(
            self._template, self._default_doc_placeholder
        )

    # ------------------------------------------------------------------
    # Abstract interface — subclasses implement these three methods.
    # ------------------------------------------------------------------

    def _validate(self, data: dict) -> SchemaT:
        """Validate a parsed dict against the target schema. Raise ValidationError on failure."""
        raise NotImplementedError

    def _make_success(self, model: SchemaT, raw: str, attempt: int) -> ResultT:
        """Construct the typed success result."""
        raise NotImplementedError

    def _make_failure(
        self,
        reason: FailureReason,
        detail: str,
        raw: str | None,
        attempt: int,
    ) -> ResultT:
        """Construct the typed failure result."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Public extract entry point
    # ------------------------------------------------------------------

    def extract(self, document_text: str) -> ResultT:
        """
        Extract structured data from raw document text.

        Args:
            document_text: The raw document text (untrusted input).

        Returns:
            A success result carrying the validated model, or a failure result
            with reason NEEDS_REEXTRACTION if both attempts fail.
        """
        logger.info(
            "Extraction started",
            extra={"text_length": len(document_text), "agent": type(self).__name__},
        )

        # -------------------------------------------------------------------
        # Attempt 1
        # -------------------------------------------------------------------
        raw1, parse_err1, val_err1 = self._attempt(document_text)

        if parse_err1 is None and val_err1 is None and raw1 is not None:
            # Attempt 1 succeeded — parse and validate were both clean.
            try:
                parsed = parse_llm_response(raw1)
                model = self._validate(parsed)
                logger.info("Extraction succeeded on attempt 1.")
                return self._make_success(model, raw1, 1)
            except (ParseError, ValidationError):
                # Should not reach here (covered by _attempt), but be safe.
                pass

        # If attempt 1 produced a LLM call error, fail immediately.
        if isinstance(parse_err1, str) and parse_err1.startswith("LLM_CALL_FAILED"):
            return self._make_failure(
                FailureReason.LLM_CALL_FAILED,
                parse_err1,
                None,
                1,
            )

        # -------------------------------------------------------------------
        # Attempt 2 — retry with validation errors fed back
        # -------------------------------------------------------------------
        logger.info(
            "Attempt 1 failed — retrying with error feedback.",
            extra={"parse_err": str(parse_err1), "val_err": str(val_err1)},
        )
        error_summary = str(val_err1 or parse_err1 or "Unknown error on attempt 1")
        retry_user_msg = _build_retry_user_message(
            original_document_text=document_text,
            raw_llm_output=raw1 or "(no output)",
            validation_errors=error_summary,
        )

        raw2, parse_err2, val_err2 = self._attempt_with_message(retry_user_msg)

        if parse_err2 is None and val_err2 is None and raw2 is not None:
            try:
                parsed2 = parse_llm_response(raw2)
                model2 = self._validate(parsed2)
                logger.info("Extraction succeeded on attempt 2 (after retry).")
                return self._make_success(model2, raw2, 2)
            except (ParseError, ValidationError):
                pass

        # -------------------------------------------------------------------
        # Both attempts failed → NEEDS_REEXTRACTION (spec.md §1, step 5)
        # -------------------------------------------------------------------
        logger.warning("Extraction failed after 2 attempts — NEEDS_REEXTRACTION.")

        # Determine the most specific reason code.
        if val_err2 is not None:
            reason = FailureReason.RETRY_VALIDATION_FAILED
            detail = (
                _format_validation_errors(val_err2)
                if isinstance(val_err2, ValidationError)
                else str(val_err2)
            )
        elif parse_err2 is not None:
            reason = FailureReason.UNPARSEABLE_JSON
            detail = str(parse_err2)
        elif val_err1 is not None:
            reason = FailureReason.SCHEMA_VALIDATION_FAILED
            detail = (
                _format_validation_errors(val_err1)
                if isinstance(val_err1, ValidationError)
                else str(val_err1)
            )
        else:
            reason = FailureReason.UNPARSEABLE_JSON
            detail = str(parse_err1 or "Unknown parse error")

        return self._make_failure(reason, detail, raw2 or raw1, 2)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _attempt(
        self, document_text: str
    ) -> tuple[str | None, str | None, ValidationError | None]:
        """
        Run attempt 1: call LLM with system prompt + document text as user msg.

        Returns (raw_response, parse_error_str, validation_error).
        raw_response is None on LLM call failure.
        parse_error_str is set on LLM call failure or parse failure.
        validation_error is set on Pydantic validation failure.
        """
        try:
            raw = self._client.complete(
                system_prompt=self._system_prompt,
                user_message=document_text,
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
        Parse raw LLM output and validate against the target schema.

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
            self._validate(data)
            # If validate passes, signal clean to the caller who will re-call
            # to get the actual model instance (avoids double-construction).
            return raw, None, None
        except ValidationError as exc:
            return raw, None, exc


# ---------------------------------------------------------------------------
# Invoice extraction agent (preserves full backward-compat API)
# ---------------------------------------------------------------------------


class ExtractionAgent(BaseExtractionAgent[InvoiceCreate, ExtractionResult]):
    """
    Extracts structured invoice data from raw text using an LLM.

    This is the original agent, now implemented as a thin subclass of
    BaseExtractionAgent.  Its public API is identical to the pre-refactor
    version — all callers and tests that used ExtractionAgent directly
    continue to work unchanged.

    Args:
        llm_client: Any object satisfying the LLMClient Protocol.
        prompt_template_path: Optional override for the prompt file path
                               (used in tests to swap templates).
    """

    _default_prompt_path = _INVOICE_PROMPT_PATH
    _default_doc_placeholder = _INVOICE_PLACEHOLDER

    def __init__(
        self,
        llm_client: LLMClient,
        prompt_template_path: Path | None = None,
    ) -> None:
        # Accept the old kwarg name for full backward compat.
        super().__init__(llm_client=llm_client, prompt_path=prompt_template_path)

    def _validate(self, data: dict) -> InvoiceCreate:
        return _validate_invoice(data)

    def _make_success(
        self, model: InvoiceCreate, raw: str, attempt: int
    ) -> ExtractionSuccess:
        return ExtractionSuccess(invoice=model, raw_payload=raw, attempt_count=attempt)

    def _make_failure(
        self,
        reason: FailureReason,
        detail: str,
        raw: str | None,
        attempt: int,
    ) -> ExtractionFailure:
        return ExtractionFailure(
            reason=reason,
            error_detail=detail,
            raw_payload=raw,
            attempt_count=attempt,
        )


# Alias kept for any code that imports ExtractionAgent explicitly.
InvoiceExtractionAgent = ExtractionAgent


# ---------------------------------------------------------------------------
# Purchase Order extraction agent
# ---------------------------------------------------------------------------


class PurchaseOrderExtractionAgent(
    BaseExtractionAgent["_POModel", "_POResult"]  # type: ignore[type-arg]
):
    """
    Extracts structured Purchase Order data from raw PO document text.

    Returns POExtractionResult (POExtractionSuccess | POExtractionFailure).

    The extracted vendor_code is surfaced in POExtractionSuccess.vendor_code_extracted.
    Callers must replace po.vendor_id with the real DB UUID after resolving the vendor.

    Args:
        llm_client: Any object satisfying the LLMClient Protocol.
        prompt_path: Optional override for the prompt file path.
    """

    _default_prompt_path = _PO_PROMPT_PATH
    _default_doc_placeholder = "{{ po_text }}"

    def _validate(self, data: dict) -> "PurchaseOrderCreate":
        from models.purchase_order import PurchaseOrderCreate

        # The PO prompt extracts vendor_code; we pass it as vendor_id so
        # PurchaseOrderCreate validation passes (it only requires non-empty str).
        vendor_code = data.get("vendor_code") or ""
        po_data = {**data, "vendor_id": vendor_code}
        # Remove vendor_code from the dict — it's not a PurchaseOrderCreate field.
        po_data.pop("vendor_code", None)
        return PurchaseOrderCreate(**po_data)

    def _make_success(
        self, model: "PurchaseOrderCreate", raw: str, attempt: int
    ) -> "POExtractionSuccess":
        from extraction.po_schemas import POExtractionSuccess

        # Re-read vendor_code_extracted from the raw payload.
        import json as _json

        try:
            vendor_code = _json.loads(raw).get("vendor_code", "")
        except Exception:
            vendor_code = model.vendor_id  # fallback

        return POExtractionSuccess(
            po=model,
            vendor_code_extracted=vendor_code or model.vendor_id,
            raw_payload=raw,
            attempt_count=attempt,
        )

    def _make_failure(
        self,
        reason: FailureReason,
        detail: str,
        raw: str | None,
        attempt: int,
    ) -> "POExtractionFailure":
        from extraction.po_schemas import POExtractionFailure

        return POExtractionFailure(
            reason=reason,
            error_detail=detail,
            raw_payload=raw,
            attempt_count=attempt,
        )


# ---------------------------------------------------------------------------
# Contract extraction agent
# ---------------------------------------------------------------------------


class ContractExtractionAgent(
    BaseExtractionAgent["_ContractModel", "_ContractResult"]  # type: ignore[type-arg]
):
    """
    Extracts structured Contract data from raw contract document text.

    Returns ContractExtractionResult (ContractExtractionSuccess | ContractExtractionFailure).

    Design notes:
    - vendor_code is extracted by the LLM and surfaced in
      ContractExtractionSuccess.vendor_code_extracted.  Callers must replace
      contract.vendor_id with the real DB UUID after resolving the vendor.
    - discount_term_raw is extracted verbatim and surfaced in
      ContractExtractionSuccess.discount_term_raw.  Callers must invoke
      discount.parser.parse_discount_term() to obtain the parsed DiscountTermSchema.
      contract.discount_term is always None at extraction time.

    Args:
        llm_client: Any object satisfying the LLMClient Protocol.
        prompt_path: Optional override for the prompt file path.
    """

    _default_prompt_path = _CONTRACT_PROMPT_PATH
    _default_doc_placeholder = "{{ contract_text }}"

    def _validate(self, data: dict) -> "ContractCreate":
        from models.contract import ContractCreate

        vendor_code = data.get("vendor_code") or ""
        contract_data = {**data, "vendor_id": vendor_code}
        # Remove LLM-specific fields that are not ContractCreate fields.
        contract_data.pop("vendor_code", None)
        contract_data.pop("discount_term_raw", None)
        # discount_term is always None at extraction time (see module docstring).
        contract_data["discount_term"] = None
        return ContractCreate(**contract_data)

    def _make_success(
        self, model: "ContractCreate", raw: str, attempt: int
    ) -> "ContractExtractionSuccess":
        from extraction.contract_schemas import ContractExtractionSuccess
        import json as _json

        try:
            raw_dict = _json.loads(raw)
            vendor_code = raw_dict.get("vendor_code", "")
            discount_term_raw = raw_dict.get("discount_term_raw")
        except Exception:
            vendor_code = model.vendor_id
            discount_term_raw = None

        return ContractExtractionSuccess(
            contract=model,
            vendor_code_extracted=vendor_code or model.vendor_id,
            discount_term_raw=discount_term_raw,
            raw_payload=raw,
            attempt_count=attempt,
        )

    def _make_failure(
        self,
        reason: FailureReason,
        detail: str,
        raw: str | None,
        attempt: int,
    ) -> "ContractExtractionFailure":
        from extraction.contract_schemas import ContractExtractionFailure

        return ContractExtractionFailure(
            reason=reason,
            error_detail=detail,
            raw_payload=raw,
            attempt_count=attempt,
        )
