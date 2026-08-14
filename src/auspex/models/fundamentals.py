"""Point-in-time XBRL fundamentals (`fundamentals` container, arc42 §5.3).

Every fact carries ``accn``, ``fy``, ``fp``, ``form``, ``end``, and ``filed``.
All fundamental queries filter ``filed <= as_of_date`` — this is what makes
historical reconstruction genuinely point-in-time.
"""

from __future__ import annotations

from datetime import date

from pydantic import Field

from auspex.models.common import AuspexModel


class XbrlFact(AuspexModel):
    taxonomy: str = Field(
        default="us-gaap",
        description="SEC companyfacts namespace: us-gaap, ifrs-full, or dei",
    )
    concept: str = Field(description="taxonomy concept name, e.g. Revenues or Revenue")
    unit: str = "USD"
    value: str = Field(description="Decimal-as-string")
    accn: str
    fy: int
    fp: str
    form: str
    start: date | None = None
    end: date
    filed: date


class FundamentalSnapshot(AuspexModel):
    """`fundamentals` container row — one per (security, accession)."""

    id: str = Field(description="{security_id}:{accn}")
    security_id: str
    accn: str
    form: str
    fy: int
    fp: str
    filed: date
    facts: list[XbrlFact] = Field(default_factory=list)

    @property
    def partition_key(self) -> str:
        return self.security_id

    def fact_values(self, concept_aliases: list[str], as_of_date: date) -> list[XbrlFact]:
        """Return facts for the first alias with data, filtered by point-in-time ``filed``."""

        for alias in concept_aliases:
            matches = [f for f in self.facts if f.concept == alias and f.filed <= as_of_date]
            if matches:
                return matches
        return []
