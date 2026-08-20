"""Decision signatures and disposition-driven suppression (arc42 §5.6, §5.7).

The behaviour being pinned: answering "no" (or "not now") to a recommendation
must stop Auspex asking the *same* question every night, without ever hiding
a genuinely different one.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from auspex.models.enums import Action, CohortConfidence, Direction, DispositionStatus
from auspex.models.policy import GateResult, RecommendationDisposition
from auspex.policy.signature import (
    SIGNATURE_VERSION,
    compute_decision_signature,
    evidence_fingerprint,
    gate_fingerprint,
    is_ready,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def signature(**overrides) -> str:
    payload = {
        "action": Action.BUY,
        "security_id": "sec-nvda",
        "suggested_quantity": "10",
        "suggested_trade_chf": Decimal("1200"),
        "target_weight_pct": Decimal("8.0"),
        "gate_trace": [
            GateResult(gate="percentile_min", passed=True),
            GateResult(gate="cash_after_trade_min", passed=True),
        ],
        "evidence": evidence_fingerprint(
            percentile=82,
            cohort_confidence=CohortConfidence.HIGH,
            direction=Direction.STRENGTHENING,
            coverage=Decimal("0.9"),
        ),
    }
    payload.update(overrides)
    return compute_decision_signature(**payload)


class TestSignatureShape:
    def test_signature_is_versioned(self):
        assert signature().startswith(f"{SIGNATURE_VERSION}:")

    def test_signature_is_deterministic(self):
        assert signature() == signature()

    def test_readiness_reflects_actionability(self):
        assert is_ready(Action.BUY) is True
        assert is_ready(Action.SELL) is True
        assert is_ready(Action.HOLD_NO_ACTION) is False
        assert is_ready(Action.HOLD_INSUFFICIENT_DATA) is False

    def test_gate_fingerprint_is_order_independent(self):
        forward = gate_fingerprint(
            [GateResult(gate="a", passed=True), GateResult(gate="b", passed=False)]
        )
        reversed_order = gate_fingerprint(
            [GateResult(gate="b", passed=False), GateResult(gate="a", passed=True)]
        )
        assert forward == reversed_order


class TestMaterialChangeDetection:
    def test_action_change_produces_a_new_signature(self):
        assert signature() != signature(action=Action.SELL)

    def test_quantity_change_produces_a_new_signature(self):
        assert signature() != signature(suggested_quantity="25")

    def test_gate_outcome_change_produces_a_new_signature(self):
        flipped = [
            GateResult(gate="percentile_min", passed=True),
            GateResult(gate="cash_after_trade_min", passed=False),
        ]
        assert signature() != signature(gate_trace=flipped)

    def test_material_evidence_change_produces_a_new_signature(self):
        weaker = evidence_fingerprint(
            percentile=41,
            cohort_confidence=CohortConfidence.HIGH,
            direction=Direction.STRENGTHENING,
            coverage=Decimal("0.9"),
        )
        assert signature() != signature(evidence=weaker)

    def test_target_weight_change_produces_a_new_signature(self):
        assert signature() != signature(target_weight_pct=Decimal("12.0"))


class TestImmaterialNoiseIsIgnored:
    def test_sub_bucket_notional_drift_keeps_the_same_signature(self):
        """A few francs of price noise is not a new question."""

        assert signature(suggested_trade_chf=Decimal("1200")) == signature(
            suggested_trade_chf=Decimal("1210")
        )

    def test_share_count_drift_from_price_noise_keeps_the_same_signature(self):
        """`suggested_quantity` is `floor(notional / price)`.

        Both inputs move nightly, so an exactly-hashed count would cross an
        integer boundary on ordinary noise and resurface a decision the user
        already rejected. Larger counts are therefore banded.
        """

        assert signature(suggested_quantity="100") == signature(suggested_quantity="101")
        assert signature(suggested_quantity="100") == signature(suggested_quantity="104")

    def test_small_share_counts_still_compare_exactly(self):
        """At single-digit size one share is a material fraction of the trade."""

        assert signature(suggested_quantity="3") != signature(suggested_quantity="4")

    def test_a_materially_different_share_count_still_changes_the_signature(self):
        assert signature(suggested_quantity="100") != signature(suggested_quantity="140")

    def test_single_percentile_point_move_keeps_the_same_signature(self):
        same_decile = evidence_fingerprint(
            percentile=84,
            cohort_confidence=CohortConfidence.HIGH,
            direction=Direction.STRENGTHENING,
            coverage=Decimal("0.9"),
        )
        assert signature() == signature(evidence=same_decile)

    def test_crossing_a_decile_boundary_is_material(self):
        next_decile = evidence_fingerprint(
            percentile=91,
            cohort_confidence=CohortConfidence.HIGH,
            direction=Direction.STRENGTHENING,
            coverage=Decimal("0.9"),
        )
        assert signature() != signature(evidence=next_decile)


def disposition(
    status: DispositionStatus, *, sig: str | None = None, expires_at: datetime | None = None
) -> RecommendationDisposition:
    return RecommendationDisposition(
        id="user-1:sec-nvda",
        user_id="user-1",
        security_id="sec-nvda",
        disposition=status,
        decision_signature=sig or signature(),
        as_of_date=date(2026, 8, 20),
        recorded_at=NOW,
        expires_at=expires_at,
    )


class TestSuppressionSemantics:
    def test_rejected_suppresses_the_same_signature_indefinitely(self):
        record = disposition(DispositionStatus.REJECTED)

        assert record.suppresses(signature(), now=NOW) is True
        assert record.suppresses(signature(), now=NOW + timedelta(days=365)) is True

    def test_rejected_does_not_suppress_a_changed_signature(self):
        record = disposition(DispositionStatus.REJECTED)

        assert record.suppresses(signature(action=Action.SELL), now=NOW) is False

    def test_deferred_suppresses_until_it_expires(self):
        record = disposition(DispositionStatus.DEFERRED, expires_at=NOW + timedelta(days=7))

        assert record.suppresses(signature(), now=NOW) is True
        assert record.suppresses(signature(), now=NOW + timedelta(days=6)) is True
        assert record.suppresses(signature(), now=NOW + timedelta(days=7)) is False
        assert record.suppresses(signature(), now=NOW + timedelta(days=8)) is False

    def test_deferred_does_not_suppress_a_changed_signature_even_before_expiry(self):
        record = disposition(DispositionStatus.DEFERRED, expires_at=NOW + timedelta(days=7))

        assert record.suppresses(signature(suggested_quantity="25"), now=NOW) is False

    def test_accepted_suppresses_nothing(self):
        record = disposition(DispositionStatus.ACCEPTED)

        assert record.suppresses(signature(), now=NOW) is False


@pytest.mark.asyncio
async def test_deferred_default_window_is_seven_days():
    from auspex.settings import Settings

    assert Settings().deferred_disposition_days == 7
