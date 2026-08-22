"""Unit tests for the six deterministic legs (arc42 §5.5 "Leg detail")."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from auspex.models.enums import Form4TransactionCode
from auspex.scoring.legs import (
    FUNDAMENTAL_HEALTH_REASON_NO_CROSS_SECTION,
    FUNDAMENTAL_HEALTH_REASON_TOO_FEW,
    AttentionEvent,
    FundamentalHealthInputs,
    InsiderTxnEvent,
    NarrativeClaimEvent,
    ThemeClaimEvent,
    ValuationMetrics,
    attention_acceleration,
    enterprise_value,
    fcf_margin,
    fundamental_health,
    gross_margin_trend_slope,
    narrative_premium,
    net_cash_ratio,
    revenue_growth_yoy,
    roic,
    smart_money,
    thesis_linkage,
    valuation_brake,
)


class TestThesisLinkage:
    def test_none_when_no_claims(self):
        """No evidence is not a measurement of zero linkage.

        Returning ``Decimal(0)`` made "this issuer published nothing we could
        link to a theme" indistinguishable from "published material that links
        to no theme", and let the leg count towards coverage on a security that
        had evidenced nothing at all.
        """

        assert thesis_linkage([]) is None

    def test_sums_and_clips_at_one(self):
        events = [
            ThemeClaimEvent(theme_strength_value=Decimal("1.0"), document_authority=Decimal("1.0"), age_days=0)
        ] * 5
        result = thesis_linkage(events)
        assert result == Decimal(1)  # clipped, since 5 * 1.0 * 1.0 = 5 > 1

    def test_zero_strength_evidence_is_still_evidence(self):
        events = [
            ThemeClaimEvent(theme_strength_value=Decimal("0"), document_authority=Decimal("1.0"), age_days=0)
        ]
        assert thesis_linkage(events) == Decimal(0)

    def test_recency_decay_reduces_older_claims(self):
        recent = thesis_linkage(
            [ThemeClaimEvent(theme_strength_value=Decimal("0.6"), document_authority=Decimal("1.0"), age_days=0)]
        )
        old = thesis_linkage(
            [ThemeClaimEvent(theme_strength_value=Decimal("0.6"), document_authority=Decimal("1.0"), age_days=180)]
        )
        assert recent is not None and old is not None
        assert recent > old


class TestAttentionAcceleration:
    def test_none_when_no_events(self):
        assert attention_acceleration([]) is None

    def test_more_recent_activity_is_positive(self):
        events = [
            AttentionEvent(materiality_weight=Decimal("1.0"), document_authority=Decimal("1.0"), days_ago=d)
            for d in range(10)
        ]
        result = attention_acceleration(events)
        assert result is not None
        assert result > 0

    def test_clipped_at_plus_1_5(self):
        events = [
            AttentionEvent(materiality_weight=Decimal("1.0"), document_authority=Decimal("1.0"), days_ago=0)
            for _ in range(1000)
        ]
        assert attention_acceleration(events) == Decimal("1.5")

    def test_none_when_nothing_falls_in_the_observation_window(self):
        """An event 90 days back says nothing about the last 60 days."""

        events = [AttentionEvent(materiality_weight=Decimal("1.0"), document_authority=Decimal("1.0"), days_ago=90)]
        assert attention_acceleration(events) is None

    def test_prior_window_only_is_a_real_deceleration(self):
        """Evidence exists, it simply all sits in the earlier window."""

        events = [AttentionEvent(materiality_weight=Decimal("1.0"), document_authority=Decimal("1.0"), days_ago=45)]
        result = attention_acceleration(events)
        assert result is not None
        assert result < 0

    def test_equal_activity_in_both_windows_is_flat_not_missing(self):
        events = [
            AttentionEvent(materiality_weight=Decimal("1.0"), document_authority=Decimal("1.0"), days_ago=days_ago)
            for days_ago in (5, 40)
        ]
        assert attention_acceleration(events) == Decimal(0)


class TestNarrativePremium:
    def test_none_without_percentile(self):
        assert narrative_premium([], None) is None

    def test_high_narrative_low_fundamentals_is_positive(self):
        events = [NarrativeClaimEvent(strength_value=Decimal("1.0"), age_days=0)]
        result = narrative_premium(events, revenue_growth_percentile=10)
        assert result > 0

    def test_low_narrative_high_fundamentals_is_negative(self):
        result = narrative_premium([], revenue_growth_percentile=90)
        assert result < 0

    def test_bounded_at_plus_minus_one(self):
        events = [NarrativeClaimEvent(strength_value=Decimal("1.0"), age_days=0)] * 10
        result = narrative_premium(events, revenue_growth_percentile=0)
        assert result <= Decimal(1)
        result2 = narrative_premium([], revenue_growth_percentile=100)
        assert result2 >= Decimal(-1)


class TestSmartMoney:
    def test_none_without_market_cap(self):
        assert smart_money([], None) is None
        assert smart_money([], Decimal(0)) is None

    def test_zero_when_no_relevant_transactions(self):
        assert smart_money([], Decimal(1_000_000)) == Decimal(0)

    def test_purchase_is_positive(self):
        events = [
            InsiderTxnEvent(
                code=Form4TransactionCode.P,
                shares=Decimal(1000),
                price_per_share=Decimal(10),
                is_officer_or_director=True,
                is_ten_percent_owner=False,
                days_ago=5,
            )
        ]
        result = smart_money(events, Decimal(1_000_000))
        assert result == Decimal("0.01")  # 1000*10 / 1_000_000

    def test_sale_is_negative(self):
        events = [
            InsiderTxnEvent(
                code=Form4TransactionCode.S,
                shares=Decimal(1000),
                price_per_share=Decimal(10),
                is_officer_or_director=True,
                is_ten_percent_owner=False,
                days_ago=5,
            )
        ]
        assert smart_money(events, Decimal(1_000_000)) == Decimal("-0.01")

    def test_excluded_codes_ignored(self):
        events = [
            InsiderTxnEvent(
                code=Form4TransactionCode.A,
                shares=Decimal(100000),
                price_per_share=Decimal(50),
                is_officer_or_director=True,
                is_ten_percent_owner=False,
                days_ago=1,
            )
        ]
        assert smart_money(events, Decimal(1_000_000)) == Decimal(0)

    def test_ten_percent_owner_half_weight(self):
        events = [
            InsiderTxnEvent(
                code=Form4TransactionCode.P,
                shares=Decimal(1000),
                price_per_share=Decimal(10),
                is_officer_or_director=False,
                is_ten_percent_owner=True,
                days_ago=5,
            )
        ]
        result = smart_money(events, Decimal(1_000_000))
        assert result == Decimal("0.005")

    def test_transactions_outside_90_days_excluded(self):
        events = [
            InsiderTxnEvent(
                code=Form4TransactionCode.P,
                shares=Decimal(1000),
                price_per_share=Decimal(10),
                is_officer_or_director=True,
                is_ten_percent_owner=False,
                days_ago=91,
            )
        ]
        assert smart_money(events, Decimal(1_000_000)) == Decimal(0)


class TestFundamentalHealth:
    """Sub-metrics are standardised inside the cohort before being combined.

    The previous implementation averaged the raw numbers directly. Those numbers
    are not commensurable: ``net_cash_ratio`` swings across whole units while
    ``gross_margin_trend_slope`` lives near 0.02, so the average was dominated by
    whichever sub-metric happened to have the widest natural units. Standardising
    each sub-metric against the same cohort first makes the equal weighting real.
    """

    @staticmethod
    def _cohort(**overrides: dict[str, Decimal | None]) -> dict[str, FundamentalHealthInputs]:
        base = {
            "peer1": FundamentalHealthInputs(
                revenue_growth_yoy=Decimal("0.10"),
                gross_margin_trend_slope=Decimal("0.00"),
                fcf_margin=Decimal("0.05"),
                net_cash_ratio=Decimal("0.00"),
                roic=Decimal("0.08"),
            ),
            "peer2": FundamentalHealthInputs(
                revenue_growth_yoy=Decimal("0.20"),
                gross_margin_trend_slope=Decimal("0.01"),
                fcf_margin=Decimal("0.08"),
                net_cash_ratio=Decimal("0.10"),
                roic=Decimal("0.12"),
            ),
            "peer3": FundamentalHealthInputs(
                revenue_growth_yoy=Decimal("0.05"),
                gross_margin_trend_slope=Decimal("-0.01"),
                fcf_margin=Decimal("0.02"),
                net_cash_ratio=Decimal("-0.05"),
                roic=Decimal("0.04"),
            ),
        }
        base.update(overrides)  # type: ignore[arg-type]
        return base  # type: ignore[return-value]

    def test_none_with_insufficient_submetrics(self):
        inputs = FundamentalHealthInputs(
            revenue_growth_yoy=Decimal("0.1"),
            gross_margin_trend_slope=None,
            fcf_margin=None,
            net_cash_ratio=None,
            roic=None,
        )
        result = fundamental_health(inputs, cohort_inputs={**self._cohort(), "self": inputs})
        assert result.value is None
        assert result.reason_not_computable == FUNDAMENTAL_HEALTH_REASON_TOO_FEW

    def test_combines_standardised_submetrics(self):
        inputs = FundamentalHealthInputs(
            revenue_growth_yoy=Decimal("0.30"),
            gross_margin_trend_slope=Decimal("0.02"),
            fcf_margin=Decimal("0.10"),
            net_cash_ratio=Decimal("0.20"),
            roic=Decimal("0.18"),
        )
        result = fundamental_health(inputs, cohort_inputs={**self._cohort(), "self": inputs})
        assert result.value is not None
        assert result.available_sub_metrics == 5
        assert result.sub_metric_coverage == Decimal(1)
        # Best-in-cohort on every sub-metric, so every standardised score is positive.
        assert all(z > 0 for z in result.sub_metric_z.values())
        assert result.value > 0

    def test_result_is_invariant_to_submetric_units(self):
        """Rescaling one sub-metric's units across the whole cohort changes nothing.

        This is the regression that motivated the change: multiplying
        ``net_cash_ratio`` by 100 for every member is a pure change of units and
        must not move anyone's health score. Under raw averaging it moved every
        score dramatically.
        """

        inputs = FundamentalHealthInputs(
            revenue_growth_yoy=Decimal("0.30"),
            gross_margin_trend_slope=Decimal("0.02"),
            fcf_margin=Decimal("0.10"),
            net_cash_ratio=Decimal("0.20"),
            roic=Decimal("0.18"),
        )
        cohort = {**self._cohort(), "self": inputs}

        def rescale(item: FundamentalHealthInputs) -> FundamentalHealthInputs:
            return replace(
                item,
                net_cash_ratio=None if item.net_cash_ratio is None else item.net_cash_ratio * 100,
            )

        rescaled_cohort = {key: rescale(value) for key, value in cohort.items()}

        before = fundamental_health(inputs, cohort_inputs=cohort)
        after = fundamental_health(rescale(inputs), cohort_inputs=rescaled_cohort)

        assert before.value is not None
        assert after.value == before.value

    def test_missing_submetric_contributes_neutrally(self):
        full = FundamentalHealthInputs(
            revenue_growth_yoy=Decimal("0.30"),
            gross_margin_trend_slope=Decimal("0.02"),
            fcf_margin=Decimal("0.10"),
            net_cash_ratio=Decimal("0.20"),
            roic=Decimal("0.18"),
        )
        partial = replace(full, roic=None)
        cohort = {**self._cohort(), "self": full}

        full_result = fundamental_health(full, cohort_inputs=cohort)
        partial_result = fundamental_health(partial, cohort_inputs={**cohort, "self": partial})

        assert full_result.value is not None and partial_result.value is not None
        assert partial_result.available_sub_metrics == 4
        assert partial_result.sub_metric_coverage == Decimal(4) / Decimal(5)
        # The trace still names the sub-metric so the omission is explainable.
        assert partial_result.sub_metric_z["roic"] is None
        # Neutral, not renormalised: the score moves toward zero rather than
        # inflating the remaining sub-metrics.
        assert abs(partial_result.value) < abs(full_result.value)

    def test_no_cross_section_is_reported_explicitly(self):
        inputs = FundamentalHealthInputs(
            revenue_growth_yoy=Decimal("0.30"),
            gross_margin_trend_slope=Decimal("0.02"),
            fcf_margin=Decimal("0.10"),
            net_cash_ratio=Decimal("0.20"),
            roic=Decimal("0.18"),
        )
        # Every peer identical -> zero dispersion on every sub-metric.
        flat = {f"peer{i}": inputs for i in range(4)}
        result = fundamental_health(inputs, cohort_inputs=flat)
        assert result.value is None
        assert result.reason_not_computable == FUNDAMENTAL_HEALTH_REASON_NO_CROSS_SECTION

    def test_revenue_growth_yoy(self):
        assert revenue_growth_yoy(Decimal(120), Decimal(100)) == Decimal("0.2")
        assert revenue_growth_yoy(Decimal(100), Decimal(0)) is None

    def test_gross_margin_trend_slope_increasing(self):
        margins = [Decimal("0.40"), Decimal("0.42"), Decimal("0.44"), Decimal("0.46")]
        slope = gross_margin_trend_slope(margins)
        assert slope > 0

    def test_gross_margin_trend_slope_needs_two_points(self):
        assert gross_margin_trend_slope([Decimal("0.4")]) is None

    def test_fcf_margin(self):
        assert fcf_margin(Decimal(50), Decimal(20), Decimal(100)) == Decimal("0.3")

    def test_net_cash_ratio(self):
        result = net_cash_ratio(Decimal(100), Decimal(50), Decimal(30), Decimal(500))
        assert result == Decimal("0.24")

    def test_roic(self):
        result = roic(Decimal(100), Decimal("0.21"), Decimal(400), Decimal(100), Decimal(50))
        assert result == (Decimal(100) * Decimal("0.79")) / Decimal(450)


class TestValuationBrake:
    def test_enterprise_value(self):
        assert enterprise_value(Decimal(1000), Decimal(200), Decimal(50)) == Decimal(1150)

    def test_none_when_no_computable_metrics(self):
        own = ValuationMetrics(ev_sales=None, ev_ebitda=None, fcf_yield=None)
        assert valuation_brake(own, {}, "sec-1") is None

    def test_cheap_security_scores_high(self):
        own = ValuationMetrics(ev_sales=Decimal(2), ev_ebitda=None, fcf_yield=None)
        cohort = {
            "sec-1": own,
            "sec-2": ValuationMetrics(ev_sales=Decimal(10), ev_ebitda=None, fcf_yield=None),
            "sec-3": ValuationMetrics(ev_sales=Decimal(15), ev_ebitda=None, fcf_yield=None),
        }
        result = valuation_brake(own, cohort, "sec-1")
        assert result > 0  # cheapest (lowest EV/Sales) inverts to a positive z

    def test_expensive_security_scores_low(self):
        expensive = ValuationMetrics(ev_sales=Decimal(20), ev_ebitda=None, fcf_yield=None)
        cohort = {
            "sec-1": expensive,
            "sec-2": ValuationMetrics(ev_sales=Decimal(5), ev_ebitda=None, fcf_yield=None),
            "sec-3": ValuationMetrics(ev_sales=Decimal(3), ev_ebitda=None, fcf_yield=None),
        }
        result = valuation_brake(expensive, cohort, "sec-1")
        assert result < 0

    def test_high_fcf_yield_scores_high_without_sign_inversion(self):
        own = ValuationMetrics(ev_sales=None, ev_ebitda=None, fcf_yield=Decimal("0.12"))
        cohort = {
            "sec-1": own,
            "sec-2": ValuationMetrics(ev_sales=None, ev_ebitda=None, fcf_yield=Decimal("0.04")),
            "sec-3": ValuationMetrics(ev_sales=None, ev_ebitda=None, fcf_yield=Decimal("0.02")),
        }
        assert valuation_brake(own, cohort, "sec-1") > 0

    def test_negative_or_undefined_metric_dropped_not_imputed(self):
        own = ValuationMetrics(ev_sales=Decimal(5), ev_ebitda=Decimal(-1), fcf_yield=None)
        cohort = {
            "sec-1": own,
            "sec-2": ValuationMetrics(ev_sales=Decimal(10), ev_ebitda=Decimal(15), fcf_yield=None),
            "sec-3": ValuationMetrics(ev_sales=Decimal(15), ev_ebitda=Decimal(20), fcf_yield=None),
        }
        # ev_ebitda is negative for sec-1 so only ev_sales contributes to its composite
        result = valuation_brake(own, cohort, "sec-1")
        assert result is not None
