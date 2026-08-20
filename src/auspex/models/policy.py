"""Policy gate trace and recommendation (`recommendations` container, arc42 §5.6)."""

from __future__ import annotations

from datetime import date, datetime

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
    decision_signature: str | None = Field(
        default=None,
        description=(
            "versioned fingerprint of the material decision "
            "(:mod:`auspex.policy.signature`). Two recommendations sharing a signature "
            "are the same ask; a user's REJECTED/DEFERRED disposition suppresses that "
            "signature until it materially changes."
        ),
    )
    suppressed: bool = Field(
        default=False,
        description="true when an active disposition suppresses this decision from the user's feed",
    )
    suppression_reason: str | None = None

    @property
    def partition_key(self) -> str:
        return self.user_id


class RecommendationDisposition(AuspexModel):
    """`recommendation_dispositions` container row, partitioned by `/user_id`.

    One durable row per ``(user, security)``: the user's most recent answer to
    the most recent ask about that security, together with the decision
    signature it applied to.

    * ``REJECTED`` suppresses that exact signature indefinitely.
    * ``DEFERRED`` suppresses it until ``expires_at``
      (``Settings.deferred_disposition_days``, default 7 days), then the same
      ask legitimately reappears.
    * ``ACCEPTED`` suppresses nothing — the user acted, and tomorrow's
      evaluation of the resulting position should be free to say anything.

    A signature change means a materially different ask, so suppression stops
    applying without the user having to clear anything.
    """

    id: str = Field(description="{user_id}:{security_id}")
    user_id: str
    security_id: str
    disposition: DispositionStatus
    decision_signature: str
    recommendation_id: str | None = None
    as_of_date: date | None = None
    recorded_at: datetime
    expires_at: datetime | None = Field(
        default=None, description="DEFERRED only — when suppression lapses"
    )

    @property
    def partition_key(self) -> str:
        return self.user_id

    def suppresses(self, decision_signature: str, *, now: datetime) -> bool:
        """Whether this disposition hides ``decision_signature`` right now."""

        if self.decision_signature != decision_signature:
            return False
        if self.disposition is DispositionStatus.REJECTED:
            return True
        if self.disposition is DispositionStatus.DEFERRED:
            return self.expires_at is None or now < self.expires_at
        return False
