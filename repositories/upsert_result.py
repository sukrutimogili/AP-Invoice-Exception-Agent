"""
repositories/upsert_result.py — Typed return type for upsert operations.

UpsertResult is a discriminated union of three outcomes:

  UpsertCreated   — no row existed; the incoming document was written and the
                    new record is returned.

  UpsertUnchanged — a row with the same natural key already exists AND every
                    compared field matches the incoming document exactly.  No
                    write is performed.  The existing record is returned.

  UpsertConflict  — a row with the same natural key already exists BUT one or
                    more compared fields differ.  No write is performed — the
                    caller decides what to do (raise an exception, flag for
                    review, etc.).  The existing record and a field-level diff
                    are both returned.

The diff is a dict[str, FieldDiff] mapping field name → (existing, incoming)
pair.  Only fields that actually differ are included; identical fields are
omitted.  Field names are the canonical Pydantic model attribute names, e.g.
"po_total", "approval_threshold", "line_items[1].unit_price".

Design decisions
----------------
- Literal outcome tags ("created", "unchanged", "conflict") allow exhaustive
  isinstance / match dispatch without importing the union type itself.
- The record field on all three variants is typed as the caller's Pydantic
  *Create schema (PurchaseOrderCreate or ContractCreate), not the ORM row,
  so callers never need to touch SQLAlchemy objects.
- UpsertResult is a Union alias, not a base class, so mypy can narrow it with
  isinstance checks without a common parent.
- FieldDiff is a simple dataclass rather than a Pydantic model; it carries
  only two Python values and needs no JSON serialisation at this layer.

Public API
----------
FieldDiff        — (existing_value, incoming_value) pair for one differing field
UpsertCreated    — outcome="created", record=<newly written Pydantic model>
UpsertUnchanged  — outcome="unchanged", record=<existing Pydantic model>
UpsertConflict   — outcome="conflict", record=<existing Pydantic model>,
                   diff=dict[str, FieldDiff]
UpsertResult     — Union[UpsertCreated, UpsertUnchanged, UpsertConflict]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar, Union

from pydantic import BaseModel

# T is the Pydantic *Create schema type (PurchaseOrderCreate or ContractCreate).
T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class FieldDiff:
    """
    Records the disagreement between the existing stored value and the
    incoming value for a single field.

    Attributes:
        existing: The value currently stored in the database.
        incoming: The value from the upload being processed.

    Both values are plain Python objects (Decimal, str, int, list, None).
    They are stored as-is for caller inspection; no normalisation is applied.
    """

    existing: Any
    incoming: Any

    def __repr__(self) -> str:
        return f"FieldDiff(existing={self.existing!r}, incoming={self.incoming!r})"


class UpsertCreated(BaseModel, Generic[T]):
    """
    The upsert wrote a new row — no prior record existed for this natural key.

    Attributes:
        record: The newly created Pydantic model, populated from the ORM row
                immediately after the INSERT.
    """

    outcome: Literal["created"] = "created"
    record: T

    model_config = {"arbitrary_types_allowed": True}


class UpsertUnchanged(BaseModel, Generic[T]):
    """
    The upsert found an existing row whose field values are identical to the
    incoming document.  No write was performed.

    Attributes:
        record: The existing Pydantic model (unchanged from the database).
    """

    outcome: Literal["unchanged"] = "unchanged"
    record: T

    model_config = {"arbitrary_types_allowed": True}


class UpsertConflict(BaseModel, Generic[T]):
    """
    The upsert found an existing row with the same natural key, but one or
    more compared fields differ from the incoming document.  No write was
    performed — the caller decides how to handle the conflict.

    Attributes:
        record: The existing Pydantic model (the stored version, not the
                incoming upload).  Callers that need the incoming values can
                read them from diff[field_name].incoming.

        diff:   Mapping of field name → FieldDiff for every field that
                differs.  Field names use dot-notation for nested values, e.g.
                "line_items[1].unit_price".  Only differing fields are
                included; fields that match are omitted.
    """

    outcome: Literal["conflict"] = "conflict"
    record: T
    diff: dict[str, FieldDiff]

    model_config = {"arbitrary_types_allowed": True}


# Convenience alias — callers annotate return types as UpsertResult[T].
UpsertResult = Union[UpsertCreated[T], UpsertUnchanged[T], UpsertConflict[T]]
