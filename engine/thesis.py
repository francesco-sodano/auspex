"""Deterministic E14/E6b per-theme Opportunity Score engine."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
import hashlib
import json
import math
from statistics import fmean, stdev
from typing import Callable, Iterable, Optional


MODEL_VERSION = "opportunity_v1"
WEIGHT_VERSION = "balanced_v1"
MIN_THEME_COHORT = 8
MIN_COMPONENT_WEIGHT = 0.50
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

LEG_COMPONENT_WEIGHTS = {
    "thesis_linkage": {"theme_linkage": 1.0},
    "attention_acceleration": {"attention_change_30d": 1.0},
    "smart_money": {
        "insider_net_buy_ratio_90d": 1 / 6,
        "insider_cluster_buy_30d": 1 / 6,
        "inst_net_flow_qoq": 1 / 6,
        "inst_new_initiations": 1 / 6,
        "contract_award_usd_trailing_90d": 1 / 6,
        "activist_13d_flag": 1 / 6,
    },
    "fundamental_health": {
        "profit_margin": 0.25,
        "rev_growth_yoy": 0.25,
        "fcf_yield": 0.25,
        "net_debt_to_ebitda": 0.25,
    },
    "valuation_brake": {"fundamental_anchor_z": 1.0},
    "crowding_positioning": {"institutional_holder_count_change_qoq": 1.0},
}
LEG_RESULT_FIELDS = {
    "thesis_linkage": "thesis_linkage_z",
    "attention_acceleration": "attention_acceleration_z",
    "smart_money": "smart_money_z",
    "fundamental_health": "fundamental_health_z",
    "valuation_brake": "valuation_brake_z",
    "crowding_positioning": "crowding_positioning_z",
}


@dataclass(frozen=True)
class OpportunityObservation:
    theme_id: str
    security_sk: int
    date_sk: int
    as_of: date
    classification_provenance: str
    classification_id: str
    classification_updated_at: datetime
    theme_proxy_weight: Optional[float]
    broad_market_weight: Optional[float]
    attention_change_30d: Optional[float]
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
    institutional_holder_count_change_qoq: Optional[float]
    max_knowledge_date: date


@dataclass(frozen=True)
class OpportunityResult:
    score_id: str
    cohort_snapshot_hash: str
    theme_id: str
    security_sk: int
    date_sk: int
    as_of: date
    classification_provenance: str
    classification_id: str
    classification_updated_at: datetime
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


def _positive_log_ratio(
    numerator: Optional[float],
    denominator: Optional[float],
) -> Optional[float]:
    left = _finite(numerator)
    right = _finite(denominator)
    if left is None or right is None or left <= 0.0 or right <= 0.0:
        return None
    return math.log(left / right)


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


def _winsorized_z(values: dict[int, Optional[float]]) -> dict[int, Optional[float]]:
    valid = sorted(value for value in values.values() if value is not None)
    if not valid:
        return {security_sk: None for security_sk in values}
    lower = _percentile(valid, WINSOR_LOWER)
    upper = _percentile(valid, WINSOR_UPPER)
    winsorized = {
        security_sk: None if value is None else min(max(value, lower), upper)
        for security_sk, value in values.items()
    }
    observed = [value for value in winsorized.values() if value is not None]
    if len(observed) < 2:
        return {
            security_sk: None if value is None else 0.0
            for security_sk, value in winsorized.items()
        }
    mean = fmean(observed)
    deviation = stdev(observed)
    if deviation <= 0.0:
        return {
            security_sk: None if value is None else 0.0
            for security_sk, value in winsorized.items()
        }
    return {
        security_sk: None if value is None else (value - mean) / deviation
        for security_sk, value in winsorized.items()
    }


def _component_z(
    observations: list[OpportunityObservation],
    field_name: str,
    direction: int = 1,
    transform: Callable[[Optional[float]], Optional[float]] = _identity,
) -> dict[int, Optional[float]]:
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


def _weighted_components(
    observations: list[OpportunityObservation],
    component_z: dict[str, dict[int, Optional[float]]],
    component_weights: dict[str, float],
) -> dict[int, Optional[float]]:
    values = {}
    for observation in observations:
        security_sk = observation.security_sk
        observed = [
            (component_z[name][security_sk], weight)
            for name, weight in component_weights.items()
            if component_z[name][security_sk] is not None
        ]
        available_weight = sum(weight for _, weight in observed)
        values[security_sk] = (
            None
            if available_weight + 1e-12 < MIN_COMPONENT_WEIGHT
            else sum(float(value) * weight for value, weight in observed) / available_weight
        )
    return values


def _coverage_reasons(observation: OpportunityObservation) -> tuple[str, ...]:
    required_fields = (
        "theme_proxy_weight",
        "broad_market_weight",
        "attention_change_30d",
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
        "institutional_holder_count_change_qoq",
    )
    missing_reasons = tuple(
        f"missing:{field_name}"
        for field_name in required_fields
        if getattr(observation, field_name) is None
    )
    return tuple(sorted(missing_reasons))


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
            or not observation.classification_provenance
            or not observation.classification_id
        ):
            raise ValueError(
                "theme_id, classification_provenance, and classification_id are required"
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
                "classification_provenance", "classification_id",
                "classification_updated_at", "max_knowledge_date",
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


def _pearson(pairs: list[tuple[float, float]]) -> Optional[float]:
    if len(pairs) < 2:
        return None
    left_mean = fmean(left for left, _ in pairs)
    right_mean = fmean(right for _, right in pairs)
    numerator = sum(
        (left - left_mean) * (right - right_mean)
        for left, right in pairs
    )
    denominator = math.sqrt(
        sum((left - left_mean) ** 2 for left, _ in pairs)
        * sum((right - right_mean) ** 2 for _, right in pairs)
    )
    return None if denominator <= 0.0 else numerator / denominator


def _pc1_variance_share(rows: list[list[float]]) -> Optional[float]:
    if len(rows) < 2:
        return None
    columns = list(zip(*rows))
    deviations = [stdev(column) for column in columns]
    if any(deviation <= 0.0 for deviation in deviations):
        return None
    standardized = [
        [
            (value - fmean(columns[index])) / deviations[index]
            for index, value in enumerate(row)
        ]
        for row in rows
    ]
    size = len(columns)
    matrix = [
        [
            sum(row[left] * row[right] for row in standardized) / (len(rows) - 1)
            for right in range(size)
        ]
        for left in range(size)
    ]
    vector = [1.0 / math.sqrt(size)] * size
    for _ in range(100):
        product = [sum(matrix[row][column] * vector[column] for column in range(size)) for row in range(size)]
        norm = math.sqrt(sum(value * value for value in product))
        if norm <= 0.0:
            return None
        next_vector = [value / norm for value in product]
        if max(abs(left - right) for left, right in zip(vector, next_vector)) < 1e-12:
            vector = next_vector
            break
        vector = next_vector
    eigenvalue = sum(
        vector[row] * matrix[row][column] * vector[column]
        for row in range(size)
        for column in range(size)
    )
    trace = sum(matrix[index][index] for index in range(size))
    return None if trace <= 0.0 else eigenvalue / trace


def cohort_leg_diagnostics(results: Iterable[OpportunityResult]) -> dict:
    rows = list(results)
    leg_names = tuple(LEG_RESULT_FIELDS)
    correlations = []
    for left in leg_names:
        for right in leg_names:
            pairs = [
                (float(getattr(row, LEG_RESULT_FIELDS[left])), float(getattr(row, LEG_RESULT_FIELDS[right])))
                for row in rows
                if getattr(row, LEG_RESULT_FIELDS[left]) is not None
                and getattr(row, LEG_RESULT_FIELDS[right]) is not None
            ]
            correlations.append({
                "leg_x": left,
                "leg_y": right,
                "pair_count": len(pairs),
                "correlation": _pearson(pairs),
            })
    complete_rows = [
        [float(getattr(row, field_name)) for field_name in LEG_RESULT_FIELDS.values()]
        for row in rows
        if all(getattr(row, field_name) is not None for field_name in LEG_RESULT_FIELDS.values())
    ]
    return {
        "correlations": correlations,
        "complete_case_count": len(complete_rows),
        "pc1_variance_share": _pc1_variance_share(complete_rows),
    }


def _blom_score(value: float, distribution: list[float]) -> float:
    if not distribution or len(set(distribution)) == 1:
        return 50.0
    sorted_values = sorted(distribution)
    equal_positions = [
        index
        for index, candidate in enumerate(sorted_values, start=1)
        if candidate == value
    ]
    rank = (
        fmean(equal_positions)
        if equal_positions
        else 1 + sum(candidate < value for candidate in sorted_values)
    )
    return 100.0 * (rank - 3 / 8) / (len(distribution) + 1 / 4)


def score_movement_attribution(
    previous: Iterable[OpportunityResult],
    current: Iterable[OpportunityResult],
) -> list[dict]:
    previous_rows = {row.security_sk: row for row in previous if row.opportunity_score_raw is not None}
    current_rows = {row.security_sk: row for row in current if row.opportunity_score_raw is not None}
    movements = []
    for security_sk in sorted(previous_rows.keys() & current_rows.keys()):
        prior = previous_rows[security_sk]
        latest = current_rows[security_sk]
        counterfactual_distribution = [
            float(latest.opportunity_score_raw)
            if row.security_sk == security_sk
            else float(row.opportunity_score_raw)
            for row in previous_rows.values()
        ]
        counterfactual = _blom_score(
            float(latest.opportunity_score_raw),
            counterfactual_distribution,
        )
        own_effect = counterfactual - float(prior.opportunity_score)
        cohort_effect = float(latest.opportunity_score) - counterfactual
        movements.append({
            "security_sk": security_sk,
            "previous_score": float(prior.opportunity_score),
            "current_score": float(latest.opportunity_score),
            "counterfactual_score": counterfactual,
            "score_delta": float(latest.opportunity_score) - float(prior.opportunity_score),
            "own_composite_effect": own_effect,
            "cohort_effect": cohort_effect,
        })
    return movements


def _withheld_result(
    observation: OpportunityObservation,
    snapshot_hash: str,
    candidate_count: int,
    reasons: Iterable[str],
) -> OpportunityResult:
    return OpportunityResult(
        score_id=_sha256({"snapshot": snapshot_hash, "security_sk": observation.security_sk}),
        cohort_snapshot_hash=snapshot_hash,
        theme_id=observation.theme_id,
        security_sk=observation.security_sk,
        date_sk=observation.date_sk,
        as_of=observation.as_of,
        classification_provenance=observation.classification_provenance,
        classification_id=observation.classification_id,
        classification_updated_at=observation.classification_updated_at,
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
        coverage_reasons=tuple(sorted(set(reasons))),
        max_knowledge_date=observation.max_knowledge_date,
        model_version=MODEL_VERSION,
        weight_version=WEIGHT_VERSION,
    )


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
            _withheld_result(
                observation,
                snapshot_hash,
                candidate_count,
                (*reasons_by_security[observation.security_sk], "theme_cohort_below_minimum"),
            )
            for observation in ordered
        ]

    component_z = {
        "theme_linkage": _winsorized_z({
            observation.security_sk: _positive_log_ratio(
                observation.theme_proxy_weight,
                observation.broad_market_weight,
            )
            for observation in ordered
        }),
        "attention_change_30d": _component_z(ordered, "attention_change_30d"),
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
        "institutional_holder_count_change_qoq": _component_z(
            ordered,
            "institutional_holder_count_change_qoq",
            direction=-1,
        ),
    }
    leg_raw = {
        leg_name: _weighted_components(ordered, component_z, component_weights)
        for leg_name, component_weights in LEG_COMPONENT_WEIGHTS.items()
    }
    available_legs = {
        observation.security_sk: tuple(
            leg_name
            for leg_name, values in leg_raw.items()
            if values[observation.security_sk] is not None
        )
        for observation in ordered
    }
    scoreable_security_sks = {
        security_sk
        for security_sk, available in available_legs.items()
        if available
    }
    if len(scoreable_security_sks) < MIN_THEME_COHORT:
        return [
            _withheld_result(
                observation,
                snapshot_hash,
                candidate_count,
                (
                    *reasons_by_security[observation.security_sk],
                    "scoreable_cohort_below_minimum",
                    *(
                        ("no_available_legs",)
                        if not available_legs[observation.security_sk]
                        else ()
                    ),
                ),
            )
            for observation in ordered
        ]
    full_variance = sum(weight * weight for weight in active_weights.values())
    variance_scales = {}
    raw_scores = {}
    for observation in ordered:
        security_sk = observation.security_sk
        available = available_legs[security_sk]
        if not available:
            continue
        available_variance = sum(active_weights[leg_name] ** 2 for leg_name in available)
        variance_scales[security_sk] = math.sqrt(full_variance / available_variance)
        raw_scores[security_sk] = variance_scales[security_sk] * sum(
            active_weights[leg_name] * float(leg_raw[leg_name][security_sk])
            for leg_name in available
        )
    sorted_scores = sorted(raw_scores.values())
    if len(set(sorted_scores)) == 1:
        percentile_scores = {observation.security_sk: 50.0 for observation in ordered}
    else:
        rank_positions: dict[float, list[int]] = {}
        for rank, value in enumerate(sorted_scores, start=1):
            rank_positions.setdefault(value, []).append(rank)
        average_rank = {
            value: fmean(positions)
            for value, positions in rank_positions.items()
        }
        percentile_scores = {
            security_sk: round(
                100.0 * (average_rank[value] - 3 / 8) / (len(raw_scores) + 1 / 4),
                4,
            )
            for security_sk, value in raw_scores.items()
        }

    results: list[OpportunityResult] = []
    for observation in ordered:
        security_sk = observation.security_sk
        reasons = reasons_by_security[security_sk]
        if security_sk not in scoreable_security_sks:
            results.append(_withheld_result(
                observation,
                snapshot_hash,
                candidate_count,
                (*reasons, "no_available_legs"),
            ))
            continue
        contributions = {
            leg_name: (
                variance_scales[security_sk]
                * active_weights[leg_name]
                * float(leg_raw[leg_name][security_sk])
                if leg_name in available_legs[security_sk]
                else 0.0
            )
            for leg_name in active_weights
        }
        effective_leg_z = {
            leg_name: (
                float(leg_raw[leg_name][security_sk])
                if leg_name in available_legs[security_sk]
                else None
            )
            for leg_name in active_weights
        }
        results.append(OpportunityResult(
            score_id=_sha256({"snapshot": snapshot_hash, "security_sk": security_sk}),
            cohort_snapshot_hash=snapshot_hash,
            theme_id=observation.theme_id,
            security_sk=security_sk,
            date_sk=observation.date_sk,
            as_of=observation.as_of,
            classification_provenance=observation.classification_provenance,
            classification_id=observation.classification_id,
            classification_updated_at=observation.classification_updated_at,
            candidate_count=candidate_count,
            thesis_linkage_z=effective_leg_z["thesis_linkage"],
            attention_acceleration_z=effective_leg_z["attention_acceleration"],
            smart_money_z=effective_leg_z["smart_money"],
            fundamental_health_z=effective_leg_z["fundamental_health"],
            valuation_brake_z=effective_leg_z["valuation_brake"],
            crowding_positioning_z=effective_leg_z["crowding_positioning"],
            thesis_linkage_contribution=contributions["thesis_linkage"],
            attention_acceleration_contribution=contributions["attention_acceleration"],
            smart_money_contribution=contributions["smart_money"],
            fundamental_health_contribution=contributions["fundamental_health"],
            valuation_brake_contribution=contributions["valuation_brake"],
            crowding_positioning_contribution=contributions["crowding_positioning"],
            opportunity_score_raw=raw_scores[security_sk],
            opportunity_score=percentile_scores[security_sk],
            coverage_status=(
                "PARTIAL"
                if len(available_legs[security_sk]) < len(LEG_WEIGHTS)
                else "READY"
            ),
            coverage_reasons=reasons,
            max_knowledge_date=observation.max_knowledge_date,
            model_version=MODEL_VERSION,
            weight_version=WEIGHT_VERSION,
        ))
    return results