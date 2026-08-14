"""Policy gate trace and recommendation (`recommendations` container, arc42 §5.6)."""

from __future__ import annotations

from datetime import date

from pydantic import Field

from auspex.models.common import AuspexModel
from auspex.models.enums import Action, DispositionStatus


class GateResult(AuspexModel):
    gate: str
    passed: bool
    actual_value: str | None = None
    threshold_value: str | None = None
    detail: str | None = None


class CostOutcomeOverlay(AuspexModel):
    """Displayed context on SELL/TRIM, never gates (arc42 §5.6)."""

    realised_gain_usd: str | None = None
    realised_gain_chf: str | None = None
    fx_effect_chf: str | None = None
    holding_period_days: int | None = None
    estimated_cost_chf: str | None = None
    cost_as_pct_of_position: str | None = None


class Recommendation(AuspexModel):
    """`recommendations` container row, partitioned by `/user_id`."""

    id: str = Field(description="{user_id}:{security_id}:{as_of_date}")
    user_id: str
    security_id: str
    as_of_date: date
    action: Action
    target_weight_pct: str | None = None
    current_weight_pct: str | None = None
    suggested_trade_chf: str | None = None
    suggested_quantity: str | None = None
    gate_trace: list[GateResult] = Field(default_factory=list)
    cost_overlay: CostOutcomeOverlay | None = None
    config_version_id: str
    disposition: DispositionStatus | None = Field(
        default=None, description="owner's response, recorded here — never written to the external ledger"
    )

    @property
    def partition_key(self) -> str:
        return self.user_id
