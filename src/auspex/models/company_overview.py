"""Current provider fundamentals, not point-in-time or reconstructed SEC facts."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from auspex.models.common import AuspexModel


class CompanyOverviewSnapshot(AuspexModel):
    """One current row in ``company_overviews``, partitioned by ``/security_id``.

    Monetary metrics retain the provider's numbers. ``financial_currency`` is
    independently verified for RevenueTTM/GrossProfitTTM, never inferred from
    the quote currency. A numeric metric can have an unavailable-field reason
    when its unit, rather than its value, is unverified.
    """

    id: str = Field(description="security_id; a refresh replaces the current snapshot")
    security_id: str
    ticker: str
    retrieved_at: datetime
    provider: Literal["alpha_vantage"] = "alpha_vantage"
    quote_currency: str | None = None
    financial_currency: str | None = Field(
        default=None,
        description="Statement-verified currency of RevenueTTM and GrossProfitTTM only",
    )
    latest_quarter: date | None = Field(
        default=None,
        description="Provider LatestQuarter period end, not historical availability",
    )
    metrics: dict[str, str | None] = Field(default_factory=dict)
    unavailable_fields: dict[str, str] = Field(default_factory=dict)

    @field_validator("id", "security_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if (
            not value
            or any(character.isspace() or ord(character) < 32 for character in value)
            or any(character in value for character in r"/\?#")
        ):
            raise ValueError("overview identifiers must be nonempty, valid document identifiers")
        return value

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized or any(character.isspace() or ord(character) < 32 for character in normalized):
            raise ValueError("overview ticker must be a nonempty symbol without whitespace")
        return normalized

    @field_validator("quote_currency", "financial_currency")
    @classmethod
    def validate_currency(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"[A-Z]{3}", value) is None:
            raise ValueError("overview currency must be an uppercase three-letter code")
        return value

    @field_validator("metrics")
    @classmethod
    def validate_metrics(cls, values: dict[str, str | None]) -> dict[str, str | None]:
        result: dict[str, str | None] = {}
        for field, value in values.items():
            if value is None:
                result[field] = None
                continue
            try:
                number = Decimal(value)
            except InvalidOperation as exc:
                raise ValueError("overview metrics must contain Decimal strings or null") from exc
            if not number.is_finite():
                raise ValueError("overview metrics must contain only finite Decimal strings")
            result[field] = str(number)
        return result

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if self.id != self.security_id:
            raise ValueError("overview id must equal security_id")
        if self.latest_quarter is not None and self.latest_quarter > self.retrieved_at.date():
            raise ValueError("LatestQuarter cannot be after the retrieval date")
        return self

    @property
    def partition_key(self) -> str:
        return self.security_id
