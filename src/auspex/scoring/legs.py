"""The six deterministic legs — raw value computations (arc42 §5.5 "Leg detail").

Every function here is pure: Decimal in, Decimal (or None for non-computable)
out, no I/O. Cross-sectional statistics needed internally (valuation_brake)
are supplied by the caller via :mod:`auspex.scoring.normalize`, which the
orchestrator (:mod:`auspex.scoring.engine`) computes once per cohort per day.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from auspex.models.enums import Form4TransactionCode
from auspex.scoring.normalize import clip, exponential_decay, mean_std, zscore

# ---------------------------------------------------------------------------
# 1. Thesis linkage
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThemeClaimEvent:
    theme_strength_value: Decimal  # STRONG=1.0 / MODERATE=0.6 / WEAK=0.25
    document_authority: Decimal
    age_days: int


def thesis_linkage(events: list[ThemeClaimEvent], half_life_days: Decimal = Decimal(90)) -> Decimal | None:
    """Sum of theme_strength * document_authority * decay(age), clipped [0, 1].

    Events must already be pre-filtered to trailing 180 days and to approved
    theme claims by the caller (arc42 §5.5 leg 1).
    """

    total = sum(
        (e.theme_strength_value * e.document_authority * exponential_decay(e.age_days, half_life_days) for e in events),
        Decimal(0),
    )
    return clip(total, Decimal(0), Decimal(1))


# ---------------------------------------------------------------------------
# 2. Attention acceleration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttentionEvent:
    materiality_weight: Decimal
    document_authority: Decimal
    days_ago: int  # 0 = today


def attention_acceleration(events: list[AttentionEvent]) -> Decimal | None:
    """ln((weighted events last 30d + 1) / (weighted events prior 30d + 1)), clipped +-1.5."""

    recent = sum(
        (e.materiality_weight * e.document_authority for e in events if 0 <= e.days_ago < 30),
        Decimal(0),
    )
    prior = sum(
        (e.materiality_weight * e.document_authority for e in events if 30 <= e.days_ago < 60),
        Decimal(0),
    )
    import math

    ratio = (float(recent) + 1.0) / (float(prior) + 1.0)
    value = Decimal(str(math.log(ratio)))
    return clip(value, Decimal("-1.5"), Decimal("1.5"))


# ---------------------------------------------------------------------------
# 3. Narrative premium
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NarrativeClaimEvent:
    strength_value: Decimal
    age_days: int


def narrative_claim_aggregate(events: list[NarrativeClaimEvent], half_life_days: Decimal = Decimal(90)) -> Decimal:
    total = sum((e.strength_value * exponential_decay(e.age_days, half_life_days) for e in events), Decimal(0))
    return clip(total, Decimal(0), Decimal(1))


def narrative_premium(
    narrative_events: list[NarrativeClaimEvent],
    revenue_growth_percentile: int | None,
) -> Decimal | None:
    """narrative_claim_aggregate - fundamental_implied_expectation, bounded +-1.

    ``fundamental_implied_expectation`` is the percentile rank (0-100) of
    trailing revenue growth within cohort, scaled to [0, 1].
    """

    if revenue_growth_percentile is None:
        return None
    aggregate = narrative_claim_aggregate(narrative_events)
    implied_expectation = Decimal(revenue_growth_percentile) / Decimal(100)
    return clip(aggregate - implied_expectation, Decimal(-1), Decimal(1))


# ---------------------------------------------------------------------------
# 4. Smart money
# ---------------------------------------------------------------------------

_INCLUDED_CODES = {Form4TransactionCode.P, Form4TransactionCode.S}


@dataclass(frozen=True)
class InsiderTxnEvent:
    code: Form4TransactionCode
    shares: Decimal
    price_per_share: Decimal
    is_officer_or_director: bool
    is_ten_percent_owner: bool
    days_ago: int  # within trailing 90d filtered by caller


def smart_money(events: list[InsiderTxnEvent], market_cap_usd: Decimal | None) -> Decimal | None:
    """Net open-market insider value (P - S) over 90d / market cap. Not computed for FPI."""

    if market_cap_usd is None or market_cap_usd <= 0:
        return None
    relevant = [e for e in events if e.code in _INCLUDED_CODES and 0 <= e.days_ago < 90]
    if not relevant:
        return Decimal(0)
    net = Decimal(0)
    for e in relevant:
        weight = (
            Decimal("1.0") if e.is_officer_or_director else (Decimal("0.5") if e.is_ten_percent_owner else Decimal(0))
        )
        if weight == 0:
            continue
        value = e.shares * e.price_per_share * weight
        net += value if e.code == Form4TransactionCode.P else -value
    return net / market_cap_usd


# ---------------------------------------------------------------------------
# 5. Fundamental health
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FundamentalHealthInputs:
    revenue_growth_yoy: Decimal | None
    gross_margin_trend_slope: Decimal | None
    fcf_margin: Decimal | None
    net_cash_ratio: Decimal | None
    roic: Decimal | None

    def sub_metrics(self) -> list[Decimal]:
        return [
            v
            for v in (
                self.revenue_growth_yoy,
                self.gross_margin_trend_slope,
                self.fcf_margin,
                self.net_cash_ratio,
                self.roic,
            )
            if v is not None
        ]


def fundamental_health(inputs: FundamentalHealthInputs, min_submetrics: int = 3) -> Decimal | None:
    """Equal-weight average of available sub-metrics; non-computable below ``min_submetrics``."""

    values = inputs.sub_metrics()
    if len(values) < min_submetrics:
        return None
    return sum(values, Decimal(0)) / Decimal(len(values))


def gross_margin_trend_slope(margins: list[Decimal]) -> Decimal | None:
    """OLS slope of gross margin across trailing quarters (oldest first)."""

    n = len(margins)
    if n < 2:
        return None
    xs = [Decimal(i) for i in range(n)]
    x_mean = sum(xs, Decimal(0)) / Decimal(n)
    y_mean = sum(margins, Decimal(0)) / Decimal(n)
    numerator = sum(((x - x_mean) * (y - y_mean) for x, y in zip(xs, margins, strict=True)), Decimal(0))
    denominator = sum(((x - x_mean) ** 2 for x in xs), Decimal(0))
    if denominator == 0:
        return None
    return numerator / denominator


def roic(
    operating_income: Decimal, tax_rate: Decimal, equity: Decimal, total_debt: Decimal, cash: Decimal
) -> Decimal | None:
    invested_capital = equity + total_debt - cash
    if invested_capital == 0:
        return None
    return operating_income * (Decimal(1) - tax_rate) / invested_capital


def net_cash_ratio(
    cash: Decimal, short_term_investments: Decimal, total_debt: Decimal, assets: Decimal
) -> Decimal | None:
    if assets == 0:
        return None
    return (cash + short_term_investments - total_debt) / assets


def fcf_margin(cfo: Decimal, capex: Decimal, revenue: Decimal) -> Decimal | None:
    if revenue == 0:
        return None
    return (cfo - capex) / revenue


def revenue_growth_yoy(revenue_current: Decimal, revenue_prior_year: Decimal) -> Decimal | None:
    if revenue_prior_year == 0:
        return None
    return (revenue_current - revenue_prior_year) / revenue_prior_year


# ---------------------------------------------------------------------------
# 6. Valuation brake
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValuationMetrics:
    ev_sales: Decimal | None
    ev_ebitda: Decimal | None
    fcf_yield: Decimal | None


def enterprise_value(market_cap: Decimal, total_debt: Decimal, cash: Decimal) -> Decimal:
    return market_cap + total_debt - cash


def valuation_brake(
    security_metrics: ValuationMetrics,
    cohort_metrics: dict[str, ValuationMetrics],
    security_id: str,
) -> Decimal | None:
    """Cross-sectional z-score each metric within cohort and orient cheap high.

    A metric that is negative or undefined for a given security is dropped
    from that security's composite rather than imputed (arc42 §5.5 leg 6).
    """

    metric_names = ("ev_sales", "ev_ebitda", "fcf_yield")
    oriented_zs: list[Decimal] = []
    for name in metric_names:
        own_value = getattr(security_metrics, name)
        if own_value is None or own_value <= 0:
            continue
        cross_section = [v for sid, m in cohort_metrics.items() if (v := getattr(m, name)) is not None and v > 0]
        if len(cross_section) < 2:
            continue
        mean, std = mean_std(cross_section)
        z = zscore(own_value, mean, std)
        if z is None:
            continue
        oriented_zs.append(z if name == "fcf_yield" else -z)
    if not oriented_zs:
        return None
    return sum(oriented_zs, Decimal(0)) / Decimal(len(oriented_zs))
