"""
extraction/__init__.py — Public API of the extraction package.

Import from here rather than individual submodules.
"""

from extraction.agent import ExtractionAgent
from extraction.llm_client import LLMCallError, LLMClient, OpenRouterClient
from extraction.parser import ParseError, parse_llm_response
from extraction.schemas import (
    ExtractionFailure,
    ExtractionResult,
    ExtractionSuccess,
    FailureReason,
)

__all__ = [
    "ExtractionAgent",
    "ExtractionFailure",
    "ExtractionResult",
    "ExtractionSuccess",
    "FailureReason",
    "LLMCallError",
    "LLMClient",
    "OpenRouterClient",
    "ParseError",
    "parse_llm_response",
]
