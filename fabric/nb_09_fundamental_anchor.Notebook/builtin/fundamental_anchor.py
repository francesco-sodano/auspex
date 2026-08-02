"""Pure, deterministic E20 fair-multiple model."""

from dataclasses import dataclass, replace
from datetime import date
from math import exp, isfinite, log, sqrt
from statistics import median, NormalDist


MIN_PEERS = 8
MIN_RESIDUAL_DF = 5
MODEL_VERSION = "e20_v2"
_HUBER_K = 1.345
_MAX_ITERATIONS = 50
_TOLERANCE = 1e-10


@dataclass(frozen=True)
class AnchorObservation:
    security_sk: int
    as_of: date
    sector: str | None
    ev_sales: float | None
    ev_ebitda: float | None
    p_fcf: float | None
    rev_growth_yoy: float | None
    gross_margin: float | None
    profit_margin: float | None
    net_debt_to_ebitda: float | None
    fcf_yield: float | None
    cash_burn_flag: bool | None
    event_date: date
    knowledge_date: date


@dataclass(frozen=True)
class AnchorResult:
    security_sk: int
    as_of: date
    sector: str
    ev_sales: float | None
    ev_ebitda: float | None
    p_fcf: float | None
    expected_ev_sales: float | None
    residual_evs: float | None
    residual_evebitda: float | None
    residual_pfcf: float | None
    anchor_residual: float | None
    fundamental_anchor_z: float | None
    anchor_method: str
    n_peers: int
    r2_sector: float | None
    uses_forward: bool
    imputed_flags: str
    model_version: str
    event_date: date
    knowledge_date: date


@dataclass(frozen=True)
class _Fit:
    expected_logs: dict[int, float]
    residuals: dict[int, float]
    standardized_residuals: dict[int, float]
    method: str
    r2: float | None
    imputed: dict[int, set[str]]


def _finite(value: float | None) -> float | None:
    if value is None:
        return None
    numeric = float(value)
    return numeric if isfinite(numeric) else None


def _percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = probability * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _winsorize(values: list[float]) -> list[float]:
    lower = _percentile(values, 0.01)
    upper = _percentile(values, 0.99)
    return [min(max(value, lower), upper) for value in values]


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    size = len(vector)
    augmented = [matrix[row][:] + [vector[row]] for row in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            return None
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                augmented[row][item] - factor * augmented[column][item]
                for item in range(size + 1)
            ]
    return [augmented[row][-1] for row in range(size)]


def _weighted_least_squares(
    design: list[list[float]],
    target: list[float],
    weights: list[float],
) -> list[float] | None:
    columns = len(design[0])
    normal = [[0.0] * columns for _ in range(columns)]
    rhs = [0.0] * columns
    for features, observed, weight in zip(design, target, weights):
        for left in range(columns):
            rhs[left] += weight * features[left] * observed
            for right in range(columns):
                normal[left][right] += weight * features[left] * features[right]
    return _solve(normal, rhs)


def _huber_fit(design: list[list[float]], target: list[float]) -> tuple[list[float], float] | None:
    coefficients = _weighted_least_squares(design, target, [1.0] * len(target))
    if coefficients is None:
        return None
    weights = [1.0] * len(target)
    for _ in range(_MAX_ITERATIONS):
        residuals = [
            observed - sum(value * coefficient for value, coefficient in zip(features, coefficients))
            for features, observed in zip(design, target)
        ]
        residual_median = median(residuals)
        mad = median([abs(value - residual_median) for value in residuals])
        scale = max(1.4826 * mad, 1e-9)
        weights = [
            1.0 if abs(value) <= _HUBER_K * scale else (_HUBER_K * scale) / abs(value)
            for value in residuals
        ]
        updated = _weighted_least_squares(design, target, weights)
        if updated is None:
            return None
        delta = max(abs(left - right) for left, right in zip(updated, coefficients))
        coefficients = updated
        if delta < _TOLERANCE:
            break
    predictions = [
        sum(value * coefficient for value, coefficient in zip(features, coefficients))
        for features in design
    ]
    weighted_mean = sum(weight * value for weight, value in zip(weights, target)) / sum(weights)
    total = sum(weight * (value - weighted_mean) ** 2 for weight, value in zip(weights, target))
    error = sum(
        weight * (value - prediction) ** 2
        for weight, value, prediction in zip(weights, target, predictions)
    )
    r2 = 1.0 - error / total if total > 1e-12 else 0.0
    return coefficients, max(min(r2, 1.0), -1.0)


def _regressors(row: AnchorObservation) -> dict[str, float | None]:
    return {
        "rev_growth_yoy": _finite(row.rev_growth_yoy),
        "gross_margin": _finite(row.gross_margin),
        "profit_margin": _finite(row.profit_margin),
        "net_debt_to_ebitda": _finite(row.net_debt_to_ebitda),
        "fcf_yield": _finite(row.fcf_yield),
        "cash_burn_flag": float(row.cash_burn_flag) if row.cash_burn_flag is not None else None,
    }


def _studentize(residuals: dict[int, float]) -> dict[int, float]:
    if len(residuals) < 2:
        return {}
    values = list(residuals.values())
    center = median(values)
    mad = median([abs(value - center) for value in values])
    scale = 1.4826 * mad
    if scale <= 1e-12:
        variance = sum((value - center) ** 2 for value in values) / max(len(values) - 1, 1)
        scale = sqrt(variance)
    if scale <= 1e-12:
        return {security_sk: 0.0 for security_sk in residuals}
    return {
        security_sk: (value - center) / scale
        for security_sk, value in residuals.items()
    }


def _normal_scores(ordered: list[tuple[AnchorObservation, float]]) -> dict[int, float]:
    if len(ordered) < 2:
        return {}
    normal = NormalDist()
    scores: dict[int, float] = {}
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and ordered[end][1] == ordered[start][1]:
            end += 1
        average_rank = (start + end - 1) / 2
        percentile = (average_rank + 0.5) / len(ordered)
        score = normal.inv_cdf(percentile)
        for row, _ in ordered[start:end]:
            scores[row.security_sk] = score
        start = end
    return scores


def _fit_multiple(rows: list[AnchorObservation], attribute: str) -> _Fit:
    eligible = sorted(
        [row for row in rows if (_finite(getattr(row, attribute)) or 0) > 0],
        key=lambda row: row.security_sk,
    )
    log_values = [log(float(getattr(row, attribute))) for row in eligible]
    if len(eligible) < MIN_PEERS:
        expected = median(log_values) if log_values else 0.0
        ordered = sorted(
            zip(eligible, log_values),
            key=lambda item: (item[1], item[0].security_sk),
        )
        residuals = {
            row.security_sk: observed - expected
            for row, observed in ordered
        }
        standardized_residuals = _normal_scores(ordered)
        return _Fit(
            expected_logs={row.security_sk: expected for row in eligible},
            residuals=residuals,
            standardized_residuals=standardized_residuals,
            method="percentile",
            r2=None,
            imputed={row.security_sk: set() for row in eligible},
        )

    names = list(_regressors(eligible[0]))
    raw_columns = {name: [_regressors(row)[name] for row in eligible] for name in names}
    medians = {
        name: median([value for value in values if value is not None])
        if any(value is not None for value in values) else 0.0
        for name, values in raw_columns.items()
    }
    imputed: dict[int, set[str]] = {row.security_sk: set() for row in eligible}
    columns: dict[str, list[float]] = {}
    for name, values in raw_columns.items():
        completed = []
        for row, value in zip(eligible, values):
            if value is None:
                value = medians[name]
                imputed[row.security_sk].add(name)
            completed.append(float(value))
        winsorized = _winsorize(completed)
        mean = sum(winsorized) / len(winsorized)
        variance = sum((value - mean) ** 2 for value in winsorized) / max(len(winsorized) - 1, 1)
        deviation = sqrt(variance)
        columns[name] = [
            (value - mean) / deviation if deviation > 1e-12 else 0.0
            for value in winsorized
        ]
    active_names = [
        name for name in names
        if any(abs(value) > 1e-12 for value in columns[name])
    ]
    minimum_regression_peers = max(
        MIN_PEERS,
        1 + len(active_names) + MIN_RESIDUAL_DF,
    )
    if len(eligible) < minimum_regression_peers or not active_names:
        expected = median(log_values)
        ordered = sorted(
            zip(eligible, log_values),
            key=lambda item: (item[1], item[0].security_sk),
        )
        residuals = {
            row.security_sk: observed - expected
            for row, observed in ordered
        }
        return _Fit(
            expected_logs={row.security_sk: expected for row in eligible},
            residuals=residuals,
            standardized_residuals=_normal_scores(ordered),
            method="percentile",
            r2=None,
            imputed=imputed,
        )
    design = [
        [1.0] + [columns[name][index] for name in active_names]
        for index in range(len(eligible))
    ]
    winsorized_target = _winsorize(log_values)
    fitted = _huber_fit(design, winsorized_target)
    if fitted is None:
        expected = median(log_values)
        ordered = sorted(
            zip(eligible, log_values),
            key=lambda item: (item[1], item[0].security_sk),
        )
        residuals = {
            row.security_sk: observed - expected
            for row, observed in ordered
        }
        standardized_residuals = _normal_scores(ordered)
        return _Fit(
            expected_logs={row.security_sk: expected for row in eligible},
            residuals=residuals,
            standardized_residuals=standardized_residuals,
            method="percentile",
            r2=None,
            imputed=imputed,
        )
    coefficients, r2 = fitted
    expected_logs = {
        row.security_sk: sum(value * coefficient for value, coefficient in zip(features, coefficients))
        for row, features in zip(eligible, design)
    }
    residuals = {
        row.security_sk: observed - expected_logs[row.security_sk]
        for row, observed in zip(eligible, log_values)
    }
    return _Fit(
        expected_logs=expected_logs,
        residuals=residuals,
        standardized_residuals=_studentize(residuals),
        method="regression",
        r2=r2,
        imputed=imputed,
    )


def build_anchors(observations: list[AnchorObservation]) -> list[AnchorResult]:
    eligible = [
        row for row in observations
        if row.event_date <= row.as_of and row.knowledge_date <= row.as_of
    ]
    grouped: dict[tuple[date, str], list[AnchorObservation]] = {}
    for row in eligible:
        grouped.setdefault((row.as_of, row.sector or "Unknown"), []).append(row)

    provisional: list[AnchorResult] = []
    for (as_of, sector), rows in sorted(grouped.items()):
        fits = {
            "ev_sales": _fit_multiple(rows, "ev_sales"),
            "ev_ebitda": _fit_multiple(rows, "ev_ebitda"),
            "p_fcf": _fit_multiple(rows, "p_fcf"),
        }
        primary_peers = sum(
            1 for row in rows if (_finite(row.ev_sales) or 0) > 0
        )
        for row in sorted(rows, key=lambda item: item.security_sk):
            primary = _finite(row.ev_sales)
            if primary is None or primary <= 0 or primary_peers < 2:
                flags = []
                if primary is None or primary <= 0:
                    flags.append("missing_positive_sales")
                if primary_peers < 2:
                    flags.append("insufficient_peers")
                provisional.append(AnchorResult(
                    security_sk=row.security_sk,
                    as_of=as_of,
                    sector=sector,
                    ev_sales=primary,
                    ev_ebitda=_finite(row.ev_ebitda),
                    p_fcf=_finite(row.p_fcf),
                    expected_ev_sales=None,
                    residual_evs=None,
                    residual_evebitda=None,
                    residual_pfcf=None,
                    anchor_residual=None,
                    fundamental_anchor_z=None,
                    anchor_method="unanchorable",
                    n_peers=primary_peers,
                    r2_sector=None,
                    uses_forward=False,
                    imputed_flags=",".join(flags),
                    model_version=MODEL_VERSION,
                    event_date=row.event_date,
                    knowledge_date=row.knowledge_date,
                ))
                continue
            residuals = {
                name: fit.residuals.get(row.security_sk)
                for name, fit in fits.items()
            }
            standardized_residuals = {
                name: fit.standardized_residuals.get(row.security_sk)
                for name, fit in fits.items()
            }
            available = [
                value for value in standardized_residuals.values()
                if value is not None
            ]
            imputed = sorted({
                flag
                for name, value in residuals.items() if value is not None
                for flag in fits[name].imputed.get(row.security_sk, set())
            })
            provisional.append(AnchorResult(
                security_sk=row.security_sk,
                as_of=as_of,
                sector=sector,
                ev_sales=primary,
                ev_ebitda=_finite(row.ev_ebitda) if (_finite(row.ev_ebitda) or 0) > 0 else None,
                p_fcf=_finite(row.p_fcf) if (_finite(row.p_fcf) or 0) > 0 else None,
                expected_ev_sales=exp(fits["ev_sales"].expected_logs[row.security_sk]),
                residual_evs=residuals["ev_sales"],
                residual_evebitda=residuals["ev_ebitda"],
                residual_pfcf=residuals["p_fcf"],
                anchor_residual=sum(available) / len(available),
                fundamental_anchor_z=None,
                anchor_method=fits["ev_sales"].method,
                n_peers=primary_peers,
                r2_sector=fits["ev_sales"].r2,
                uses_forward=False,
                imputed_flags=",".join(imputed),
                model_version=MODEL_VERSION,
                event_date=row.event_date,
                knowledge_date=row.knowledge_date,
            ))

    by_date: dict[date, list[AnchorResult]] = {}
    for result in provisional:
        if result.anchor_residual is not None:
            by_date.setdefault(result.as_of, []).append(result)
    standardized: dict[tuple[date, int], float] = {}
    for as_of, results in by_date.items():
        values = [float(result.anchor_residual) for result in results]
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / max(len(values) - 1, 1)
        deviation = sqrt(variance)
        for result in results:
            standardized[(as_of, result.security_sk)] = (
                (float(result.anchor_residual) - mean) / deviation if deviation > 1e-12 else 0.0
            )
    return [
        replace(
            result,
            fundamental_anchor_z=standardized.get((result.as_of, result.security_sk)),
        )
        for result in sorted(provisional, key=lambda item: (item.as_of, item.security_sk))
    ]