"""
extraction/__init__.py — Public API of the extraction package.

Import from here rather than individual submodules.
"""

from extraction.agent import (
    ContractExtractionAgent,
    ExtractionAgent,
    InvoiceExtractionAgent,
    PurchaseOrderExtractionAgent,
)
from extraction.contract_schemas import (
    ContractExtractionFailure,
    ContractExtractionResult,
    ContractExtractionSuccess,
)
from extraction.llm_client import LLMCallError, LLMClient, OpenRouterClient
from extraction.parser import ParseError, parse_llm_response
from extraction.po_schemas import (
    POExtractionFailure,
    POExtractionResult,
    POExtractionSuccess,
)
from extraction.schemas import (
    ExtractionFailure,
    ExtractionResult,
    ExtractionSuccess,
    FailureReason,
)

__all__ = [
    # Agents
    "ContractExtractionAgent",
    "ExtractionAgent",          # invoice agent (original name, backward-compat)
    "InvoiceExtractionAgent",   # alias for ExtractionAgent
    "PurchaseOrderExtractionAgent",
    # Invoice result types (original)
    "ExtractionFailure",
    "ExtractionResult",
    "ExtractionSuccess",
    # PO result types
    "POExtractionFailure",
    "POExtractionResult",
    "POExtractionSuccess",
    # Contract result types
    "ContractExtractionFailure",
    "ContractExtractionResult",
    "ContractExtractionSuccess",
    # Shared
    "FailureReason",
    "LLMCallError",
    "LLMClient",
    "OpenRouterClient",
    "ParseError",
    "parse_llm_response",
]
