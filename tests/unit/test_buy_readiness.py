from __future__ import annotations

from datetime import date

from auspex.api.viewmodels import build_recommendation_out
from auspex.models.policy import GateResult, Recommendation


def test_high_score_hold_explains_cash_reserve_blocker() -> None:
    recommendation = Recommendation(
        id="owner:sec:2026-08-12",
        user_id="owner",
        security_id="sec",
        as_of_date=date(2026, 8, 12),
        action="HOLD_NO_ACTION",
        target_weight_pct="15",
        current_weight_pct="0",
        suggested_trade_chf="0",
        gate_trace=[
            GateResult(
                gate="percentile_min",
                passed=True,
                actual_value="100",
                threshold_value="75",
            ),
            GateResult(
                gate="cash_after_trade_min",
                passed=False,
                actual_value="0",
                threshold_value="3000",
            ),
        ],
        config_version_id="cfg",
    )

    result = build_recommendation_out(recommendation, "QRVO", "Qorvo", None)

    assert result.buy_ready is False
    assert result.blocking_reasons == [
        "Cash reserve: projected CHF 0 remaining; minimum CHF 3000"
    ]


def test_blockers_round_percentages_and_z_scores_for_display() -> None:
    recommendation = Recommendation(
        id="owner:sec:2026-08-12",
        user_id="owner",
        security_id="sec",
        as_of_date=date(2026, 8, 12),
        action="HOLD_NO_ACTION",
        gate_trace=[
            GateResult(
                gate="weight_max",
                passed=False,
                actual_value="6.684256269304652412455990056",
                threshold_value="15",
            ),
            GateResult(
                gate="thesis_linkage_z_max",
                passed=False,
                actual_value="1.079474088904541263350055477",
                threshold_value="-1.0",
            ),
        ],
        config_version_id="cfg",
    )

    result = build_recommendation_out(recommendation, "SITM", "SiTime", None)

    assert result.blocking_reasons == [
        "Weight Max: 6.68%; required 15%",
        "Thesis Linkage Z Max: 1.08; required -1",
    ]
