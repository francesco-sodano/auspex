from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class GateResult:
    ready: bool
    reasons: tuple[str, ...]


def evaluate_recommendation_gate(
    recommendation_response: dict,
    citations: list[dict],
    *,
    today: date,
    max_age_days: int = 5,
) -> GateResult:
    reasons: list[str] = []
    if recommendation_response.get("status") != "ready":
        reasons.append("portfolio_or_signal_status_not_ready")
    if not recommendation_response.get("recommendations"):
        reasons.append("recommendations_unavailable")

    as_of_text = recommendation_response.get("as_of")
    try:
        as_of = date.fromisoformat(as_of_text)
    except (TypeError, ValueError):
        as_of = None
        reasons.append("recommendation_as_of_invalid")
    if as_of is not None:
        age_days = (today - as_of).days
        if age_days < 0:
            reasons.append("recommendation_as_of_in_future")
        elif age_days > max_age_days:
            reasons.append("recommendation_stale")

    if not citations:
        reasons.append("evidence_unavailable")
    elif as_of is not None:
        for citation in citations:
            knowledge_date_text = citation.get("knowledge_date")
            try:
                knowledge_date = date.fromisoformat(str(knowledge_date_text)[:10])
            except (TypeError, ValueError):
                reasons.append("evidence_knowledge_date_invalid")
                continue
            if knowledge_date > as_of:
                reasons.append("evidence_after_recommendation_as_of")

    return GateResult(not reasons, tuple(dict.fromkeys(reasons)))