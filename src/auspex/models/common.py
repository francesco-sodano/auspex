"""Shared Pydantic base classes and Decimal-safe helpers.

arc42 TC-06: all monetary values are stored as ``Decimal``, never ``float``.
:class:`AuspexModel` configures Pydantic to serialise ``Decimal`` as strings
(never lossy binary floats) and to reject unknown fields so that stored
documents keep a strict, auditable shape.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


def new_id() -> str:
    return str(uuid.uuid4())


def utc_now() -> datetime:
    return datetime.now(UTC)


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def content_hash(data: bytes | str) -> str:
    return f"sha256:{sha256_hex(data)}"


class AuspexModel(BaseModel):
    """Base model: Decimal-as-string JSON, strict extra field handling."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
    )


def as_decimal(value: str | int | float | Decimal) -> Decimal:
    """Convert to Decimal via ``str`` to avoid binary-float contamination.

    Floats are accepted defensively (e.g. numeric literals from third-party
    JSON APIs) but always routed through ``str()`` first so no float
    precision noise survives into monetary or scoring arithmetic.
    """

    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)
