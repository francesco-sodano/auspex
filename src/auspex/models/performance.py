"""Self-measurement metrics (`performance` container, arc42 §5.8)."""

from __future__ import annotations

from datetime import date

from pydantic import Field

from auspex.models.common import AuspexModel


class PerformanceMetric(AuspexModel):
    """`performance` container row, partitioned by `/metric_type`."""

    id: str = Field(description="{metric_type}:{as_of_date}:{scope}")
    metric_type: str = Field(
        description="composite_ic | leg_ic | leg_correlation | suggestion_hit_rate | "
        "disposition_outcome | cohort_quality"
    )
    as_of_date: date
    horizon_days: int | None = Field(default=None, description="21 | 63 | 126, when applicable")
    scope: str = Field(default="universe", description="universe | cohort:<name> | leg:<name>")
    value: str = Field(description="Decimal-as-string metric value")
    sample_size: int
    detail: dict[str, str] = Field(default_factory=dict)
    user_id: str | None = Field(
        default=None,
        description="set only for private recommendation/outcome metrics",
    )

    @property
    def partition_key(self) -> str:
        return self.metric_type
