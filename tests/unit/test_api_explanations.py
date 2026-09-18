from datetime import date

from auspex.api.explanations import mover_summary, score_reasoning
from auspex.models.enums import CohortConfidence, FilerProfile, LegName
from auspex.models.policy import Recommendation
from auspex.models.scoring import LegExplanation, LegResult, ScoreSnapshot


def _score(
    percentile: int,
    thesis_contribution: str,
    attention_contribution: str,
) -> ScoreSnapshot:
    return ScoreSnapshot(
        id=f"sec-a:{date(2026, 8, 8).isoformat()}",
        security_id="sec-a",
        as_of_date=date(2026, 8, 8),
        config_version_id="cfg",
        cohort_used="software",
        cohort_confidence=CohortConfidence.HIGH,
        filer_profile=FilerProfile.DOMESTIC,
        coverage="1",
        legs={
            LegName.THESIS_LINKAGE: LegResult(
                raw="0.5",
                z="1",
                weight="0.2",
                contribution=thesis_contribution,
                computable=True,
            ),
            LegName.ATTENTION_ACCELERATION: LegResult(
                raw="0.2",
                z="-1",
                weight="0.15",
                contribution=attention_contribution,
                computable=True,
            ),
        },
        composite="0.2",
        percentile=percentile,
        package_fingerprint="fp",
        max_knowledge_date=date(2026, 8, 8),
    )


def test_mover_summary_uses_plain_language_and_action_meaning():
    prior = _score(40, "0.05", "-0.15")
    current = _score(58, "0.20", "-0.15")
    recommendation = Recommendation(
        id="owner:sec-a:2026-08-08",
        user_id="owner",
        security_id="sec-a",
        as_of_date=date(2026, 8, 8),
        action="HOLD_NO_ACTION",
        config_version_id="cfg",
    )

    summary = mover_summary(current, prior, recommendation)

    assert "rose 18 points to 58/100" in summary
    assert "documented connection to the tracked themes" in summary
    assert "relative to similar companies" in summary
    assert "No portfolio change is suggested today" in summary
    assert "z-score" not in summary
    assert "cohort" not in summary
    assert "gate" not in summary


def test_score_reasoning_explains_relative_score_without_quant_jargon():
    prior = _score(70, "0.05", "-0.15")
    current = _score(80, "0.20", "-0.15")

    explanation = score_reasoning(
        current,
        prior,
        {
            LegName.THESIS_LINKAGE.value: 88,
            LegName.ATTENTION_ACCELERATION.value: 20,
        },
    )

    assert "80/100" not in explanation
    assert "rose 10 points" not in explanation
    assert "Detailed source reasons have not yet been attached" in explanation
    assert "documented connection to the tracked themes" in explanation
    assert "pace of important company updates" in explanation
    assert "not a forecast of the share price" in explanation
    assert "z-score" not in explanation
    assert explanation.count("documented connection to the tracked themes") == 1
    assert explanation.count("pace of important company updates") == 1


def test_score_reasoning_uses_actual_leg_facts_not_score_numbers():
    current = _score(80, "0.20", "-0.15")
    current.legs[LegName.THESIS_LINKAGE].explanation = LegExplanation(
        summary="The company reports customer demand for data-center cooling equipment.",
        effect="supports",
    )
    current.legs[LegName.ATTENTION_ACCELERATION].explanation = LegExplanation(
        summary="Important company updates have become less frequent in the recent reporting window.",
        effect="weighs",
    )
    explanation = score_reasoning(current, None, {})
    assert "customer demand for data-center cooling" in explanation
    assert "become less frequent" in explanation
    assert "80" not in explanation
    assert "percentile" not in explanation
    assert "z-score" not in explanation


def test_opposing_leg_move_does_not_claim_peer_movement_as_fact():
    prior = _score(60, "0.00", "0.00")
    current = _score(68, "0.10", "-0.20")

    summary = mover_summary(current, prior, None)

    assert "may explain the difference" in summary
    assert "movement among comparable companies outweighed" not in summary
