"""Deterministic E14/E6b per-theme Opportunity Score engine."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
import hashlib
import json
import math
from statistics import fmean, stdev
from typing import Callable, Iterable, Optional


MODEL_VERSION = "e6b_v1"
WEIGHT_VERSION = "e6b_balanced_v1"
MIN_THEME_COHORT = 8
WINSOR_LOWER = 0.01
WINSOR_UPPER = 0.99

LEG_WEIGHTS = {
    "thesis_linkage": 0.20,
    "attention_acceleration": 0.15,
    "smart_money": 0.20,
    "fundamental_health": 0.20,
    "valuation_brake": 0.15,
    "crowding_positioning": 0.10,
}


@dataclass(frozen=True)
class OpportunityObservation:
    theme_id: str
    security_sk: int
    date_sk: int
    as_of: date
    candidate_source: str
    candidate_snapshot_id: str
    candidate_snapshot_ingest_ts: datetime
    membership_weight: Optional[float]
    news_volume_z_30d: Optional[float]
    insider_net_buy_ratio_90d: Optional[float]
    insider_cluster_buy_30d: Optional[float]
    inst_net_flow_qoq: Optional[float]
    inst_new_initiations: Optional[float]
    contract_award_usd_trailing_90d: Optional[float]
    activist_13d_flag: Optional[bool]
    profit_margin: Optional[float]
    rev_growth_yoy: Optional[float]
    fcf_yield: Optional[float]
    net_debt_to_ebitda: Optional[float]
    fundamental_anchor_z: Optional[float]
    news_count_30d: Optional[float]
    institutional_holder_count_120d: Optional[float]
    max_knowledge_date: date


@dataclass(frozen=True)
class OpportunityResult:
    score_id: str
    cohort_snapshot_hash: str
    theme_id: str
    security_sk: int
    date_sk: int
    as_of: date
    candidate_source: str
    candidate_snapshot_id: str
    candidate_snapshot_ingest_ts: datetime
    candidate_count: int
    thesis_linkage_z: Optional[float]
    attention_acceleration_z: Optional[float]
    smart_money_z: Optional[float]
    fundamental_health_z: Optional[float]
    valuation_brake_z: Optional[float]
    crowding_positioning_z: Optional[float]
    thesis_linkage_contribution: Optional[float]
    attention_acceleration_contribution: Optional[float]
    smart_money_contribution: Optional[float]
    fundamental_health_contribution: Optional[float]
    valuation_brake_contribution: Optional[float]
    crowding_positioning_contribution: Optional[float]
    opportunity_score_raw: Optional[float]
    opportunity_score: Optional[float]
    coverage_status: str
    coverage_reasons: tuple[str, ...]
    max_knowledge_date: date
    model_version: str
    weight_version: str


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _finite(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError("Opportunity Score inputs must be finite")
    return numeric


def _identity(value: Optional[float]) -> Optional[float]:
    return _finite(value)


def _nonnegative_log1p(value: Optional[float]) -> Optional[float]:
    numeric = _finite(value)
    if numeric is None:
        return None
    return math.log1p(max(numeric, 0.0))


def _percentile(sorted_values: list[float], quantile: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    fraction = position - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction


def _winsorized_z(values: dict[int, Optional[float]]) -> dict[int, float]:
    valid = sorted(value for value in values.values() if value is not None)
    if not valid:
        return {security_sk: 0.0 for security_sk in values}
    lower = _percentile(valid, WINSOR_LOWER)
    upper = _percentile(valid, WINSOR_UPPER)
    winsorized = {
        security_sk: None if value is None else min(max(value, lower), upper)
        for security_sk, value in values.items()
    }
    observed = [value for value in winsorized.values() if value is not None]
    if len(observed) < 2:
        return {security_sk: 0.0 for security_sk in values}
    mean = fmean(observed)
    deviation = stdev(observed)
    if deviation <= 0.0:
        return {security_sk: 0.0 for security_sk in values}
    return {
        security_sk: 0.0 if value is None else (value - mean) / deviation
        for security_sk, value in winsorized.items()
    }


def _component_z(
    observations: list[OpportunityObservation],
    field_name: str,
    direction: int = 1,
    transform: Callable[[Optional[float]], Optional[float]] = _identity,
) -> dict[int, float]:
    values: dict[int, Optional[float]] = {}
    for observation in observations:
        raw_value = getattr(observation, field_name)
        if isinstance(raw_value, bool):
            raw_value = float(raw_value)
        transformed = transform(raw_value)
        values[observation.security_sk] = (
            None if transformed is None else transformed * direction
        )
    return _winsorized_z(values)


def _average_components(
    observations: list[OpportunityObservation],
    components: Iterable[dict[int, float]],
) -> dict[int, float]:
    component_list = list(components)
    return {
        observation.security_sk: fmean(
            component[observation.security_sk] for component in component_list
        )
        for observation in observations
    }


def _coverage_reasons(observation: OpportunityObservation) -> tuple[str, ...]:
    required_fields = (
        "membership_weight",
        "news_volume_z_30d",
        "insider_net_buy_ratio_90d",
        "insider_cluster_buy_30d",
        "inst_net_flow_qoq",
        "inst_new_initiations",
        "contract_award_usd_trailing_90d",
        "activist_13d_flag",
        "profit_margin",
        "rev_growth_yoy",
        "fcf_yield",
        "net_debt_to_ebitda",
        "fundamental_anchor_z",
        "news_count_30d",
        "institutional_holder_count_120d",
    )
    return tuple(
        f"missing:{field_name}"
        for field_name in required_fields
        if getattr(observation, field_name) is None
    )


def _validate(observations: list[OpportunityObservation]) -> None:
    if not observations:
        return
    expected = (
        observations[0].theme_id,
        observations[0].date_sk,
        observations[0].as_of,
    )
    security_keys: set[int] = set()
    for observation in observations:
        if (
            not observation.theme_id
            or not observation.candidate_source
            or not observation.candidate_snapshot_id
        ):
            raise ValueError(
                "theme_id, candidate_source, and candidate_snapshot_id are required"
            )
        if (observation.theme_id, observation.date_sk, observation.as_of) != expected:
            raise ValueError("score_theme requires one theme and as_of cohort")
        if observation.security_sk in security_keys:
            raise ValueError("duplicate security in theme cohort")
        security_keys.add(observation.security_sk)
        if observation.max_knowledge_date > observation.as_of:
            raise ValueError("knowledge_date exceeds as_of")
        for field_name, value in asdict(observation).items():
            if field_name in {
                "theme_id", "security_sk", "date_sk", "as_of",
                "candidate_source", "candidate_snapshot_id",
                "candidate_snapshot_ingest_ts", "max_knowledge_date",
            }:
                continue
            if value is not None and not isinstance(value, bool):
                _finite(value)


def _validate_leg_weights(leg_weights: dict[str, float]) -> dict[str, float]:
    if set(leg_weights) != set(LEG_WEIGHTS):
        raise ValueError("Opportunity Score leg weights do not match the six-leg contract")
    validated = {leg_name: float(weight) for leg_name, weight in leg_weights.items()}
    if any(not math.isfinite(weight) or weight < 0.0 for weight in validated.values()):
        raise ValueError("Opportunity Score leg weights must be finite and nonnegative")
    if not math.isclose(sum(validated.values()), 1.0, abs_tol=1e-9):
        raise ValueError("Opportunity Score leg weights must sum to 1.0")
    return validated


def _cohort_snapshot_hash(
    observations: list[OpportunityObservation],
    leg_weights: dict[str, float],
) -> str:
    return _sha256({
        "model_version": MODEL_VERSION,
        "weight_version": WEIGHT_VERSION,
        "weights": leg_weights,
        "minimum_cohort": MIN_THEME_COHORT,
        "winsor": [WINSOR_LOWER, WINSOR_UPPER],
        "observations": [asdict(observation) for observation in observations],
    })


def score_theme(
    observations: Iterable[OpportunityObservation],
    leg_weights: dict[str, float],
) -> list[OpportunityResult]:
    ordered = sorted(observations, key=lambda observation: observation.security_sk)
    active_weights = _validate_leg_weights(leg_weights)
    _validate(ordered)
    if not ordered:
        return []

    snapshot_hash = _cohort_snapshot_hash(ordered, active_weights)
    candidate_count = len(ordered)
    reasons_by_security = {
        observation.security_sk: _coverage_reasons(observation)
        for observation in ordered
    }
    if candidate_count < MIN_THEME_COHORT:
        return [
            OpportunityResult(
                score_id=_sha256({"snapshot": snapshot_hash, "security_sk": observation.security_sk}),
                cohort_snapshot_hash=snapshot_hash,
                theme_id=observation.theme_id,
                security_sk=observation.security_sk,
                date_sk=observation.date_sk,
                as_of=observation.as_of,
                candidate_source=observation.candidate_source,
                candidate_snapshot_id=observation.candidate_snapshot_id,
                candidate_snapshot_ingest_ts=observation.candidate_snapshot_ingest_ts,
                candidate_count=candidate_count,
                thesis_linkage_z=None,
                attention_acceleration_z=None,
                smart_money_z=None,
                fundamental_health_z=None,
                valuation_brake_z=None,
                crowding_positioning_z=None,
                thesis_linkage_contribution=None,
                attention_acceleration_contribution=None,
                smart_money_contribution=None,
                fundamental_health_contribution=None,
                valuation_brake_contribution=None,
                crowding_positioning_contribution=None,
                opportunity_score_raw=None,
                opportunity_score=None,
                coverage_status="WITHHELD",
                coverage_reasons=tuple(sorted((*reasons_by_security[observation.security_sk], "theme_cohort_below_minimum"))),
                max_knowledge_date=observation.max_knowledge_date,
                model_version=MODEL_VERSION,
                weight_version=WEIGHT_VERSION,
            )
            for observation in ordered
        ]

    component_z = {
        "membership_weight": _component_z(ordered, "membership_weight"),
        "news_volume_z_30d": _component_z(ordered, "news_volume_z_30d"),
        "insider_net_buy_ratio_90d": _component_z(ordered, "insider_net_buy_ratio_90d"),
        "insider_cluster_buy_30d": _component_z(ordered, "insider_cluster_buy_30d"),
        "inst_net_flow_qoq": _component_z(ordered, "inst_net_flow_qoq"),
        "inst_new_initiations": _component_z(ordered, "inst_new_initiations"),
        "contract_award_usd_trailing_90d": _component_z(
            ordered,
            "contract_award_usd_trailing_90d",
            transform=_nonnegative_log1p,
        ),
        "activist_13d_flag": _component_z(ordered, "activist_13d_flag"),
        "profit_margin": _component_z(ordered, "profit_margin"),
        "rev_growth_yoy": _component_z(ordered, "rev_growth_yoy"),
        "fcf_yield": _component_z(ordered, "fcf_yield"),
        "net_debt_to_ebitda": _component_z(ordered, "net_debt_to_ebitda", direction=-1),
        "fundamental_anchor_z": _component_z(ordered, "fundamental_anchor_z", direction=-1),
        "news_count_30d": _component_z(ordered, "news_count_30d", direction=-1),
        "institutional_holder_count_120d": _component_z(
            ordered,
            "institutional_holder_count_120d",
            direction=-1,
        ),
    }
    leg_raw = {
        "thesis_linkage": component_z["membership_weight"],
        "attention_acceleration": component_z["news_volume_z_30d"],
        "smart_money": _average_components(ordered, (
            component_z["insider_net_buy_ratio_90d"],
            component_z["insider_cluster_buy_30d"],
            component_z["inst_net_flow_qoq"],
            component_z["inst_new_initiations"],
            component_z["contract_award_usd_trailing_90d"],
            component_z["activist_13d_flag"],
        )),
        "fundamental_health": _average_components(ordered, (
            component_z["profit_margin"],
            component_z["rev_growth_yoy"],
            component_z["fcf_yield"],
            component_z["net_debt_to_ebitda"],
        )),
        "valuation_brake": component_z["fundamental_anchor_z"],
        "crowding_positioning": _average_components(ordered, (
            component_z["news_count_30d"],
            component_z["institutional_holder_count_120d"],
        )),
    }
    leg_z = {
        leg_name: _winsorized_z(values)
        for leg_name, values in leg_raw.items()
    }
    raw_scores = {
        observation.security_sk: sum(
            active_weights[leg_name] * leg_z[leg_name][observation.security_sk]
            for leg_name in active_weights
        )
        for observation in ordered
    }
    unique_scores = sorted(set(raw_scores.values()))
    if len(unique_scores) == 1:
        percentile_scores = {observation.security_sk: 50.0 for observation in ordered}
    else:
        first_rank: dict[float, int] = {}
        for index, value in enumerate(sorted(raw_scores.values())):
            first_rank.setdefault(value, index)
        percentile_scores = {
            security_sk: round(first_rank[value] / (candidate_count - 1) * 100.0, 4)
            for security_sk, value in raw_scores.items()
        }

    results: list[OpportunityResult] = []
    for observation in ordered:
        security_sk = observation.security_sk
        reasons = reasons_by_security[security_sk]
        contributions = {
            leg_name: active_weights[leg_name] * leg_z[leg_name][security_sk]
            for leg_name in active_weights
        }
        results.append(OpportunityResult(
            score_id=_sha256({"snapshot": snapshot_hash, "security_sk": security_sk}),
            cohort_snapshot_hash=snapshot_hash,
            theme_id=observation.theme_id,
            security_sk=security_sk,
            date_sk=observation.date_sk,
            as_of=observation.as_of,
            candidate_source=observation.candidate_source,
            candidate_snapshot_id=observation.candidate_snapshot_id,
            candidate_snapshot_ingest_ts=observation.candidate_snapshot_ingest_ts,
            candidate_count=candidate_count,
            thesis_linkage_z=leg_z["thesis_linkage"][security_sk],
            attention_acceleration_z=leg_z["attention_acceleration"][security_sk],
            smart_money_z=leg_z["smart_money"][security_sk],
            fundamental_health_z=leg_z["fundamental_health"][security_sk],
            valuation_brake_z=leg_z["valuation_brake"][security_sk],
            crowding_positioning_z=leg_z["crowding_positioning"][security_sk],
            thesis_linkage_contribution=contributions["thesis_linkage"],
            attention_acceleration_contribution=contributions["attention_acceleration"],
            smart_money_contribution=contributions["smart_money"],
            fundamental_health_contribution=contributions["fundamental_health"],
            valuation_brake_contribution=contributions["valuation_brake"],
            crowding_positioning_contribution=contributions["crowding_positioning"],
            opportunity_score_raw=raw_scores[security_sk],
            opportunity_score=percentile_scores[security_sk],
            coverage_status="PARTIAL" if reasons else "READY",
            coverage_reasons=reasons,
            max_knowledge_date=observation.max_knowledge_date,
            model_version=MODEL_VERSION,
            weight_version=WEIGHT_VERSION,
        ))
    return results