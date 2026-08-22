"""Score snapshot and leg-change record (`scores`, `leg_changes` containers).

arc42 §5.11 score document + §5.5 scoring engine outputs.
"""

from __future__ import annotations

from datetime import date

from pydantic import Field

from auspex.models.common import AuspexModel
from auspex.models.enums import CohortConfidence, Direction, FilerProfile, LegName


class LegResult(AuspexModel):
    raw: str | None = Field(default=None, description="Decimal-as-string raw leg value")
    z: str | None = Field(default=None, description="Decimal-as-string winsorised z-score")
    weight: str = Field(description="Decimal-as-string weight actually applied this row")
    contribution: str | None = Field(default=None, description="weight * winsorised z")
    computable: bool
    evidence_ids: list[str] = Field(default_factory=list)
    reason_not_computable: str | None = None


class ScoreSnapshot(AuspexModel):
    """`scores` container row, id = `{security_id}:{as_of_date}`."""

    id: str
    security_id: str
    as_of_date: date
    config_version_id: str
    cohort_used: str
    cohort_confidence: CohortConfidence
    filer_profile: FilerProfile
    coverage: str = Field(description="Decimal-as-string computable_legs / applicable_legs")
    is_backfilled: bool = False
    legs: dict[LegName, LegResult]
    composite: str | None = None
    percentile: int | None = None
    direction: Direction = Direction.STABLE
    package_fingerprint: str
    narrative: str | None = None
    narrative_model_version: str | None = None
    max_knowledge_date: date
    excluded_stale: bool = False

    @property
    def partition_key(self) -> str:
        return self.security_id


class LegChange(AuspexModel):
    """`leg_changes` container row — one per (security, date, leg).

    ``own_evidence_effect`` and ``cohort_distribution_effect`` are an exact
    decomposition of ``delta_z``: they sum to it, or both are ``null`` and
    ``attribution_unavailable_reason`` says why. There is no partial state in
    which one carries the whole move.
    """

    id: str = Field(description="{security_id}:{as_of_date}:{leg}")
    security_id: str
    as_of_date: date
    leg: LegName
    prior_z: str | None = None
    current_z: str | None = None
    delta_z: str | None = None
    own_evidence_effect: str | None = Field(
        default=None,
        description="delta attributable to this security's own raw value moving, peers held at today's distribution",
    )
    cohort_distribution_effect: str | None = Field(
        default=None,
        description="delta attributable to the peer distribution moving, this security's raw value held at its prior",
    )
    attribution_unavailable_reason: str | None = Field(
        default=None,
        description="why the two effects are null (no prior leg value, or prior value not rankable today)",
    )

    @property
    def partition_key(self) -> str:
        return self.security_id
