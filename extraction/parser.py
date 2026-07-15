"""
extraction/parser.py — Defensive JSON parser for LLM output.

spec.md §1 model-portability rule steps 2–3:
  2. Parse defensively (strip markdown fences, attempt JSON repair).
  3. Validate against the Pydantic model.

Strategy (in order):
  1. Strip leading/trailing whitespace.
  2. Strip markdown code fences (```json ... ``` or ``` ... ```).
  3. Attempt json.loads() directly.
  4. If that fails, attempt lightweight repair:
       - Remove trailing commas before } or ]
       - Strip non-JSON prefix/suffix text (find first { and last })
  5. Return the parsed dict or raise ParseError.

No external JSON-repair library is used — keeping it dependency-free and
predictable on weak models. If repair also fails, we propagate a typed error
so the agent can decide whether to retry.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


class ParseError(Exception):
    """Raised when the LLM response cannot be parsed into a JSON object."""

    def __init__(self, message: str, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


# Matches ```json ... ``` or ``` ... ``` fences (non-greedy, DOTALL).
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)

# Trailing comma before closing brace/bracket — common LLM mistake.
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _strip_fences(text: str) -> str:
    """
    Remove markdown code fences if present.

    Handles:
      ```json { ... } ```
      ``` { ... } ```
      { ... }  (no fences — returned as-is)
    """
    match = _FENCE_RE.search(text)
    if match:
        logger.debug("Stripped markdown fence from LLM response.")
        return match.group(1).strip()
    return text.strip()


def _extract_json_object(text: str) -> str:
    """
    Find the first `{` and the last `}` in the text and return that slice.

    This handles cases where the LLM prefixes the JSON with explanation text
    even after being told not to (common on weak models).
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ParseError(
            "No JSON object found in LLM response (no matching { ... } pair).",
            raw=text,
        )
    return text[start : end + 1]


def _repair(text: str) -> str:
    """
    Apply lightweight, safe repairs to near-valid JSON.

    Only removes trailing commas — a very common LLM error that is safe to fix
    without changing semantics.
    """
    return _TRAILING_COMMA_RE.sub(r"\1", text)


def parse_llm_response(raw: str) -> dict[str, Any]:
    """
    Parse the raw LLM response string into a Python dict.

    Attempts (in order):
      1. Strip fences → direct json.loads.
      2. Strip fences → repair → json.loads.
      3. Extract { ... } slice → repair → json.loads.

    Raises:
        ParseError: if all attempts fail, carrying the original raw string.
    """
    if not raw or not raw.strip():
        raise ParseError("LLM response is empty.", raw=raw or "")

    # Step 1: strip fences, try direct parse.
    stripped = _strip_fences(raw)
    try:
        result = json.loads(stripped)
        if not isinstance(result, dict):
            raise ParseError(
                f"Expected a JSON object (dict), got {type(result).__name__}.",
                raw=raw,
            )
        logger.debug("JSON parsed on first attempt (stripped fences).")
        return result
    except json.JSONDecodeError:
        pass

    # Step 2: repair trailing commas, try again.
    repaired = _repair(stripped)
    try:
        result = json.loads(repaired)
        if not isinstance(result, dict):
            raise ParseError(
                f"Expected a JSON object (dict), got {type(result).__name__}.",
                raw=raw,
            )
        logger.debug("JSON parsed after trailing-comma repair.")
        return result
    except json.JSONDecodeError:
        pass

    # Step 3: extract first { ... } slice (handles extra prose) then repair.
    try:
        sliced = _extract_json_object(stripped)
        repaired_sliced = _repair(sliced)
        result = json.loads(repaired_sliced)
        if not isinstance(result, dict):
            raise ParseError(
                f"Expected a JSON object (dict), got {type(result).__name__}.",
                raw=raw,
            )
        logger.debug("JSON parsed after slice extraction + repair.")
        return result
    except (json.JSONDecodeError, ParseError) as exc:
        raise ParseError(
            f"All JSON parse attempts failed. Last error: {exc}",
            raw=raw,
        ) from exc
