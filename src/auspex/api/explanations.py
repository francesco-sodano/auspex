"""Plain-language explanations for score and mover read models.

These helpers translate already-computed deterministic results. They never
change a score, infer evidence, or create an action.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from auspex.models.enums import Action, LegName
from auspex.models.policy import Recommendation
from auspex.models.scoring import LegExplanation, LegResult, ScoreSnapshot

LEG_LABELS: dict[LegName, str] = {
    LegName.THESIS_LINKAGE: "documented connection to the tracked themes",
    LegName.ATTENTION_ACCELERATION: "the pace of important company updates",
    LegName.NARRATIVE_PREMIUM: "the company story compared with its business progress",
    LegName.SMART_MONEY: "recent insider buying and selling",
    LegName.FUNDAMENTAL_HEALTH: "business performance and financial strength",
    LegName.VALUATION_BRAKE: "valuation compared with similar companies",
}


def leg_availability_explanation(leg: LegName, result: LegResult) -> str | None:
    if result.computable:
        return None
    if result.explanation is not None:
        return result.explanation.summary
    if result.reason_not_computable == "not_applicable":
        return "This area is not used because the required comparable information is not applicable to this issuer."
    if result.raw is not None:
        return (
            "There is evidence for this area, but not enough variation among comparable companies "
            "to distinguish it. It contributes neutrally rather than being treated as a weakness."
        )
    messages = {
        LegName.THESIS_LINKAGE: (
            "No usable, verified links to the tracked investment themes were available for this score date. "
            "That is missing evidence, not proof that the company has no relevant business activity."
        ),
        LegName.ATTENTION_ACCELERATION: (
            "There are not enough assessed company updates in the comparison window to tell whether "
            "important reporting is becoming more or less frequent."
        ),
        LegName.NARRATIVE_PREMIUM: (
            "The available financial history does not support a reliable comparison between "
            "the company story and its business progress."
        ),
        LegName.SMART_MONEY: (
            "Auspex could not reliably relate the insider records to the company's market value. "
            "Missing share-price or share-count data does not imply that insiders were selling."
        ),
        LegName.FUNDAMENTAL_HEALTH: (
            "The stored financial reports do not provide enough comparable inputs to assess "
            "growth, cash generation and financial strength together."
        ),
        LegName.VALUATION_BRAKE: (
            "The necessary market value, financial figures or positive comparable valuation measures "
            "are unavailable. Auspex does not treat missing information as a cheap share price."
        ),
    }
    return messages[leg]


def _score_position(score: int) -> str:
    if score >= 75:
        return "among the stronger companies in its comparison group"
    if score >= 60:
        return "above the middle of its comparison group"
    if score >= 40:
        return "around the middle of its comparison group"
    if score >= 25:
        return "below the middle of its comparison group"
    return "among the weaker companies in its comparison group"


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _contribution(result: LegResult | None) -> Decimal | None:
    if result is None:
        return None
    contribution = _decimal(result.contribution)
    if contribution is not None:
        return contribution
    z_value = _decimal(result.z)
    weight = _decimal(result.weight)
    if z_value is None or weight is None:
        return None
    return z_value * weight


def _selected_explanations(
    score: ScoreSnapshot, *, per_effect: int = 2
) -> list[tuple[LegName, LegExplanation]]:
    explained = [
        (name, leg, leg.explanation) for name, leg in score.legs.items()
        if leg.explanation is not None
    ]
    supported = sorted(
        (item for item in explained if item[2].effect == "supports"),
        key=lambda item: _contribution(item[1]) or Decimal(0), reverse=True,
    )
    weighed = sorted(
        (item for item in explained if item[2].effect == "weighs"),
        key=lambda item: _contribution(item[1]) or Decimal(0),
    )
    selected = [*supported[:per_effect], *weighed[:per_effect]] or explained[:per_effect]
    return [(name, detail) for name, _leg, detail in selected]


def _largest_leg_change(
    current: ScoreSnapshot,
    prior: ScoreSnapshot,
) -> tuple[LegName, Decimal] | None:
    changes: list[tuple[LegName, Decimal]] = []
    for leg in LegName:
        current_value = _contribution(current.legs.get(leg))
        prior_value = _contribution(prior.legs.get(leg))
        if current_value is None or prior_value is None:
            continue
        changes.append((leg, current_value - prior_value))
    return max(changes, key=lambda item: abs(item[1]), default=None)


def _action_sentence(recommendation: Recommendation | None) -> str:
    if recommendation is None:
        return "There is no portfolio action attached to this update."
    if recommendation.action is Action.BUY:
        return "It also passed today's portfolio checks for a possible new purchase."
    if recommendation.action is Action.ADD:
        return "It also passed today's portfolio checks for a possible addition."
    if recommendation.action is Action.TRIM:
        return "Auspex currently suggests reducing the existing position."
    if recommendation.action is Action.SELL:
        return "Auspex currently suggests exiting the existing position."
    if recommendation.action is Action.HOLD_INSUFFICIENT_DATA:
        return "No action is suggested because there is not enough reliable information."
    return "No portfolio change is suggested today."


def mover_summary(
    current: ScoreSnapshot,
    prior: ScoreSnapshot,
    recommendation: Recommendation | None,
) -> str:
    """Explain a one-session score move without quant terminology."""

    if current.percentile is None or prior.percentile is None:
        return "A comparable score movement is not available for this company."
    score_change = current.percentile - prior.percentile
    direction = "rose" if score_change > 0 else "fell"
    opening = (
        f"The Auspex Score {direction} {abs(score_change)} "
        f"{'point' if abs(score_change) == 1 else 'points'} to "
        f"{current.percentile}/100, placing the company "
        f"{_score_position(current.percentile)}."
    )
    largest = _largest_leg_change(current, prior)
    if largest is None or largest[1] == 0:
        reason = (
            "The company's own signals changed little, so movement among comparable "
            "companies explains most of the new rank."
        )
    elif (largest[1] > 0) == (score_change > 0):
        change_direction = "improved" if largest[1] > 0 else "weakened"
        reason = (
            f"The area that {change_direction} most relative to similar companies "
            f"was {LEG_LABELS[largest[0]]}. That relative movement can reflect "
            "company updates, changes among its peers, or both."
        )
    else:
        reason = (
            "The largest area moved in the opposite direction to the overall score. "
            "Changes in the other research areas or among comparable companies may "
            "explain the difference."
        )
    if largest is not None:
        leg = current.legs.get(largest[0])
        if leg is not None and leg.explanation is not None:
            opening = f"The score {direction} compared with the previous scored session."
            reason += f" Current evidence in this area: {leg.explanation.summary}"
    return " ".join((opening, reason, _action_sentence(recommendation)))


def top_score_summary(
    current: ScoreSnapshot,
    prior: ScoreSnapshot | None,
    recommendation: Recommendation | None,
) -> str:
    """Explain a high current score without implying a price forecast."""

    if current.percentile is None:
        return "A reliable current score is not available for this company."
    facts = _selected_explanations(current, per_effect=1)
    if facts:
        return " ".join([*(detail.summary for _name, detail in facts), _action_sentence(recommendation)])
    opening = (
        f"The Auspex Score is {current.percentile}/100, placing the company "
        f"{_score_position(current.percentile)}."
    )
    movement = ""
    if prior is not None and prior.percentile is not None:
        score_change = current.percentile - prior.percentile
        if score_change == 0:
            movement = " The score is unchanged from the previous scored session."
        else:
            movement = (
                f" It {'rose' if score_change > 0 else 'fell'} {abs(score_change)} "
                f"{'point' if abs(score_change) == 1 else 'points'} since the previous "
                "scored session."
            )
    return (
        f"{opening}{movement} {_action_sentence(recommendation)} "
        "The score is a comparison with similar companies, not a share-price forecast."
    )


def score_reasoning(
    score: ScoreSnapshot,
    prior: ScoreSnapshot | None,
    leg_scores: Mapping[str, int | None],
) -> str:
    """Explain actual stored drivers, without narrating calculation numbers."""

    if score.excluded_stale:
        return (
            "Auspex has withheld this assessment because the available share price is too old. "
            "The company is not being assigned a weak score; a reliable current assessment needs fresh market data."
        )
    if score.percentile is None:
        return (
            "Auspex cannot calculate a reliable score for this company yet. "
            "More current, comparable information is needed."
        )

    selected = _selected_explanations(score)
    if selected:
        parts = [
            f"{LEG_LABELS[name].capitalize()}: {detail.summary}"
            for name, detail in selected
        ]
        missing = [
            name for name, leg in score.legs.items()
            if leg.explanation is not None and leg.explanation.effect == "unavailable"
        ]
        if missing:
            parts.append(
                "The assessment is less complete where reliable evidence is unavailable: "
                + ", ".join(LEG_LABELS[name] for name in missing)
                + ". Missing information is not a negative finding."
            )
        parts.append("This is an evidence-based comparison with similar companies, not a share-price forecast.")
        return "\n\n".join(parts)

    ranked = sorted(
        (
            (LegName(name), value)
            for name, value in leg_scores.items()
            if value is not None
        ),
        key=lambda item: item[1],
    )
    if len(ranked) >= 4:
        weakest = ranked[:2]
        strongest = list(reversed(ranked[-2:]))
    elif len(ranked) == 3:
        weakest = ranked[:1]
        strongest = list(reversed(ranked[-1:]))
    elif len(ranked) == 2:
        weakest = ranked[:1]
        strongest = list(reversed(ranked[-1:]))
    else:
        weakest = []
        strongest = ranked
    opening = "Detailed source reasons have not yet been attached to this saved assessment."

    def names(items: list[tuple[LegName, int]]) -> str:
        return " and ".join(LEG_LABELS[leg] for leg, _value in items)

    comparisons: list[str] = []
    if strongest:
        comparisons.append(f"The strongest areas are {names(strongest)}")
    if weakest:
        comparisons.append(f"the weakest are {names(weakest)}")
    comparison_text = "; ".join(comparisons)
    if comparison_text:
        comparison_text = f" {comparison_text[0].upper()}{comparison_text[1:]}."

    coverage_text = (
        " All applicable areas had enough information to assess."
        if Decimal(score.coverage) == Decimal(1)
        else " Some applicable areas did not have enough reliable information to assess."
    )
    return (
        f"{opening}{comparison_text}{coverage_text} "
        "This score is a comparison with similar companies, not a forecast of the share price."
    )
