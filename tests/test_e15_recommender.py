from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import unittest

from api.auspex_api.recommendations import (
    InMemoryOpportunitySignalRepository,
    RecommendationService,
)
from api.auspex_api.recommender.costs import estimate_costs
from api.auspex_api.recommender.policy import (
    CandidateSignal,
    FinancingPolicy,
    PortfolioContext,
    build_recommendations,
)
from api.auspex_api.recommender.risk_profile import policy_for_profile


class E15RecommenderTests(unittest.TestCase):
    @staticmethod
    def financing_policy():
        return FinancingPolicy(
            max_diluted_share_growth=Decimal("0.20"),
            min_cash_runway_years=Decimal("1.0"),
            max_shelf_age_days=90,
        )

    def test_zero_notional_has_zero_cost(self):
        self.assertEqual(
            estimate_costs(Decimal("0"), security_country="US").total_base,
            Decimal("0.00"),
        )

    def test_recommendation_route_is_owner_implicit(self):
        function_app = (
            Path(__file__).resolve().parents[1] / "api" / "function_app.py"
        ).read_text(encoding="utf-8")
        self.assertIn('route="recommendations"', function_app)
        self.assertNotIn('route="recommendations/{owner_user_sk}"', function_app)

    def test_score_projection_is_current_shared_data(self):
        notebook = (
            Path(__file__).resolve().parents[1]
            / "fabric" / "nb_08_portfolio_derive.Notebook" / "notebook-content.py"
        ).read_text(encoding="utf-8")
        self.assertIn('F.lit("score:security:")', notebook)
        self.assertIn('F.lit("opportunity_score")', notebook)
        self.assertIn('"coverage_reasons", "attribution", "spread_bps"', notebook)
        self.assertIn('F.col("s.opportunity_score_raw")', notebook)
        self.assertIn("duplicate_effective_scores", notebook)
        self.assertIn("classification_provenance", notebook)
        self.assertIn('spark.table("fact_financing_risk")', notebook)
        self.assertIn("financing_coverage_status", notebook)
        self.assertIn('.unionByName(latest_scores, allowMissingColumns=True)', notebook)

        ingestion_app = (
            Path(__file__).resolve().parents[1] / "connectors" / "function_app.py"
        ).read_text(encoding="utf-8")
        self.assertIn('("quote:", "history:", "fx:", "score:security:", "classification:security:")', ingestion_app)

    def test_engine_is_packaged_with_the_function_app(self):
        package = Path(__file__).resolve().parents[1] / "api" / "auspex_api" / "recommender"
        self.assertTrue((package / "policy.py").is_file())
        self.assertTrue((package / "costs.py").is_file())
        self.assertTrue((package / "risk_profile.py").is_file())

    def test_risk_bands_have_monotonic_caps_and_cash_buffers(self):
        conservative = policy_for_profile("Conservative")
        balanced = policy_for_profile("Balanced")
        growth = policy_for_profile("Growth")
        aggressive = policy_for_profile("Aggressive")

        self.assertLess(conservative.max_position_weight, balanced.max_position_weight)
        self.assertLess(balanced.max_position_weight, growth.max_position_weight)
        self.assertLess(growth.max_position_weight, aggressive.max_position_weight)
        self.assertGreater(conservative.cash_buffer_pct, balanced.cash_buffer_pct)
        self.assertGreater(balanced.cash_buffer_pct, growth.cash_buffer_pct)
        self.assertGreater(growth.cash_buffer_pct, aggressive.cash_buffer_pct)

    def test_buy_respects_position_cap_cash_buffer_and_costs(self):
        recommendations = build_recommendations(
            PortfolioContext(
                total_value_base=Decimal("100000"),
                cash_base=Decimal("20000"),
                risk_profile="Balanced",
                base_currency="USD",
                annual_trade_count=3,
                financing_policy=self.financing_policy(),
            ),
            [CandidateSignal(
                security_sk=101,
                ticker="MSFT",
                opportunity_score=Decimal("90"),
                coverage_status="READY",
                current_value_base=Decimal("0"),
                current_weight=Decimal("0"),
                country="US",
                spread_bps=Decimal("5"),
                opportunity_score_raw=Decimal("1.0"),
                financing_record_available=True,
                diluted_share_growth_yoy=Decimal("0.01"),
                is_burning_cash=False,
            )],
            as_of="2026-07-29",
        )

        recommendation = recommendations[0]
        self.assertEqual(recommendation.action, "BUY")
        self.assertLessEqual(recommendation.target_weight, Decimal("0.10"))
        self.assertLessEqual(
            recommendation.suggested_amount_base + recommendation.estimated_cost_base,
            Decimal("8000"),
        )
        self.assertGreater(recommendation.estimated_cost_base, Decimal("0"))

    def test_overweight_and_weak_holdings_are_trimmed_or_sold(self):
        portfolio = PortfolioContext(
            total_value_base=Decimal("100000"),
            cash_base=Decimal("5000"),
            risk_profile="Balanced",
            base_currency="USD",
            financing_policy=self.financing_policy(),
        )
        recommendations = build_recommendations(
            portfolio,
            [
                CandidateSignal(
                    security_sk=1, ticker="OVER", opportunity_score=Decimal("85"),
                    coverage_status="READY", current_value_base=Decimal("20000"),
                    current_weight=Decimal("0.20"), country="US",
                ),
                CandidateSignal(
                    security_sk=2, ticker="WEAK", opportunity_score=Decimal("30"),
                    coverage_status="READY", current_value_base=Decimal("7000"),
                    current_weight=Decimal("0.07"), country="CH",
                ),
            ],
            as_of="2026-07-29",
        )

        by_ticker = {row.ticker: row for row in recommendations}
        self.assertEqual(by_ticker["OVER"].action, "TRIM")
        self.assertEqual(by_ticker["OVER"].target_weight, Decimal("0.10"))
        self.assertEqual(by_ticker["WEAK"].action, "SELL")
        self.assertEqual(by_ticker["WEAK"].target_weight, Decimal("0"))
        self.assertLess(by_ticker["WEAK"].suggested_amount_base, Decimal("0"))

    def test_partial_coverage_and_costly_small_trades_are_suppressed(self):
        portfolio = PortfolioContext(
            total_value_base=Decimal("100000"),
            cash_base=Decimal("30000"),
            risk_profile="Balanced",
            base_currency="USD",
            financing_policy=self.financing_policy(),
        )
        recommendations = build_recommendations(
            portfolio,
            [
                CandidateSignal(
                    security_sk=1, ticker="PART", opportunity_score=Decimal("90"),
                    coverage_status="PARTIAL", current_value_base=Decimal("0"),
                    current_weight=Decimal("0"), country="US",
                    coverage_reasons=("missing:fundamental_anchor_z",),
                ),
                CandidateSignal(
                    security_sk=2, ticker="SMALL", opportunity_score=Decimal("62"),
                    coverage_status="READY", current_value_base=Decimal("0"),
                    current_weight=Decimal("0"), country="US",
                    spread_bps=Decimal("500"),
                    opportunity_score_raw=Decimal("0.5"),
                    financing_record_available=True,
                    diluted_share_growth_yoy=Decimal("0.01"),
                    is_burning_cash=False,
                ),
            ],
            as_of="2026-07-29",
        )

        by_ticker = {row.ticker: row for row in recommendations}
        self.assertEqual(by_ticker["PART"].action, "HOLD")
        self.assertIn("coverage", by_ticker["PART"].suppression_reasons)
        self.assertEqual(by_ticker["SMALL"].action, "HOLD")
        self.assertTrue(
            {"minimum_trade", "cost_exceeds_edge"}
            & set(by_ticker["SMALL"].suppression_reasons)
        )

    def test_professional_dealer_caution_is_flag_not_tax_advice(self):
        recommendations = build_recommendations(
            PortfolioContext(
                total_value_base=Decimal("100000"), cash_base=Decimal("30000"),
                risk_profile="Aggressive", base_currency="CHF", annual_trade_count=24,
                financing_policy=self.financing_policy(),
            ),
            [CandidateSignal(
                security_sk=1, ticker="MSFT", opportunity_score=Decimal("90"),
                coverage_status="READY", current_value_base=Decimal("0"),
                current_weight=Decimal("0"), country="US",
                opportunity_score_raw=Decimal("1.0"),
                financing_record_available=True,
                diluted_share_growth_yoy=Decimal("0.01"),
                is_burning_cash=False,
            )],
            as_of="2026-07-29",
        )

        self.assertIn(
            "swiss_professional_securities_dealer_review",
            recommendations[0].tax_flags,
        )
        self.assertIn("not tax advice", recommendations[0].rationale.lower())

    def test_service_combines_current_owner_portfolio_with_shared_signals(self):
        class Identity:
            def product_user(self, principal):
                self.principal = principal
                return SimpleNamespace(
                    user_sk="owner-a", risk_profile="Balanced", base_currency="USD",
                )

        class Portfolio:
            def portfolio_summary(self, principal):
                self.principal = principal
                return {
                    "status": "ready", "base_currency": "USD",
                    "valuation_as_of": "2026-07-29", "total_cash_base": "20000.00",
                    "total_value_base": "100000.00",
                    "holdings": [{
                        "security_sk": 101, "ticker": "MSFT", "country": "US",
                        "market_value_base": "5000.00", "weight": "0.05",
                    }],
                }

            def annual_trade_count(self, principal, year):
                self.trade_count_args = (principal, year)
                return 3

        identity = Identity()
        portfolio = Portfolio()
        service = RecommendationService(
            identity,
            portfolio,
            InMemoryOpportunitySignalRepository([{
                "security_sk": 101, "ticker": "MSFT", "opportunity_score": "90",
                "coverage_status": "READY", "country": "US", "spread_bps": "5",
                "theme_id": "enterprise_technology", "coverage_reasons": [],
                "opportunity_score_raw": "1.0",
                "financing_coverage_status": "READY",
                "diluted_share_growth_yoy": "0.01", "is_burning_cash": False,
                "as_of": "2026-07-29",
            }]),
            financing_policy=self.financing_policy(),
        )

        result = service.recommendations("principal-a")

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["as_of"], "2026-07-29")
        self.assertEqual(result["risk_profile"], "Balanced")
        self.assertEqual(result["recommendations"][0]["ticker"], "MSFT")
        self.assertEqual(result["recommendations"][0]["opportunity_score_raw"], "1.0")
        self.assertEqual(identity.principal, "principal-a")
        self.assertEqual(portfolio.principal, "principal-a")

    def test_service_withholds_when_portfolio_valuation_is_incomplete(self):
        identity = SimpleNamespace(product_user=lambda _: SimpleNamespace(
            user_sk="owner-a", risk_profile="Balanced", base_currency="USD",
        ))
        portfolio = SimpleNamespace(
            portfolio_summary=lambda _: {
                "status": "pending_ingestion", "base_currency": "USD",
                "valuation_as_of": None, "total_cash_base": "1000.00",
                "total_value_base": None, "holdings": [],
            },
            annual_trade_count=lambda *_: 0,
        )
        service = RecommendationService(
            identity, portfolio, InMemoryOpportunitySignalRepository([]),
        )

        result = service.recommendations("principal-a")

        self.assertEqual(result["status"], "withheld")
        self.assertEqual(result["reasons"], ["portfolio_valuation_incomplete"])
        self.assertEqual(result["recommendations"], [])

    def test_service_omits_suppressed_holds_for_unowned_candidates(self):
        identity = SimpleNamespace(product_user=lambda _: SimpleNamespace(
            user_sk="owner-a", risk_profile="Growth", base_currency="USD",
        ))
        portfolio = SimpleNamespace(
            portfolio_summary=lambda _: {
                "status": "ready", "base_currency": "USD",
                "valuation_as_of": "2026-07-29", "total_cash_base": "0.00",
                "total_value_base": "100000.00", "holdings": [],
            },
            annual_trade_count=lambda *_: 0,
        )
        service = RecommendationService(
            identity,
            portfolio,
            InMemoryOpportunitySignalRepository([{
                "security_sk": 101, "ticker": "AAPL", "opportunity_score": "100",
                "coverage_status": "READY", "country": "US", "spread_bps": "5",
                "theme_id": "enterprise_technology", "coverage_reasons": [],
                "opportunity_score_raw": "1.0",
                "financing_coverage_status": "READY",
                "diluted_share_growth_yoy": "0.01", "is_burning_cash": False,
                "as_of": "2026-07-29",
            }]),
            financing_policy=self.financing_policy(),
        )

        result = service.recommendations("principal-a")

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["recommendations"], [])

    def test_service_orders_actionable_cap_trim_before_higher_score_hold(self):
        identity = SimpleNamespace(product_user=lambda _: SimpleNamespace(
            user_sk="owner-a", risk_profile="Growth", base_currency="USD",
        ))
        portfolio = SimpleNamespace(
            portfolio_summary=lambda _: {
                "status": "ready", "base_currency": "USD",
                "valuation_as_of": "2026-08-06", "total_cash_base": "5000.00",
                "total_value_base": "100000.00", "holdings": [
                    {"security_sk": 1, "ticker": "HOLD", "country": "US", "market_value_base": "10000", "weight": "0.10"},
                    {"security_sk": 2, "ticker": "TRIM", "country": "US", "market_value_base": "18000", "weight": "0.18"},
                ],
            },
            annual_trade_count=lambda *_: 0,
        )
        service = RecommendationService(
            identity,
            portfolio,
            InMemoryOpportunitySignalRepository([
                {"security_sk": 1, "ticker": "HOLD", "opportunity_score": "99", "coverage_status": "PARTIAL", "theme_id": "healthcare", "coverage_reasons": ["missing:theme_proxy_weight"], "as_of": "2026-08-06"},
                {"security_sk": 2, "ticker": "TRIM", "opportunity_score": "90", "coverage_status": "PARTIAL", "theme_id": "healthcare", "coverage_reasons": ["missing:theme_proxy_weight"], "as_of": "2026-08-06"},
            ]),
        )

        result = service.recommendations("principal-a")

        self.assertEqual(result["recommendations"][0]["ticker"], "TRIM")
        self.assertEqual(result["recommendations"][0]["action"], "TRIM")
        self.assertEqual(result["recommendations"][1]["action"], "HOLD")

    def test_absolute_floor_suppresses_score_driven_increase(self):
        recommendation = build_recommendations(
            PortfolioContext(
                total_value_base=Decimal("100000"), cash_base=Decimal("30000"),
                risk_profile="Growth", base_currency="USD",
                financing_policy=self.financing_policy(),
            ),
            [CandidateSignal(
                security_sk=1, ticker="WEAKCOHORT", opportunity_score=Decimal("90"),
                coverage_status="READY", current_value_base=Decimal("0"),
                current_weight=Decimal("0"), country="US",
                opportunity_score_raw=Decimal("-0.01"),
                financing_record_available=True,
                diluted_share_growth_yoy=Decimal("0.01"), is_burning_cash=False,
            )],
            as_of="2026-08-06",
        )[0]

        self.assertEqual(recommendation.action, "HOLD")
        self.assertIn("absolute_floor", recommendation.suppression_reasons)

    def test_financing_veto_fails_closed_without_record_or_configuration(self):
        candidate = CandidateSignal(
            security_sk=1, ticker="FIN", opportunity_score=Decimal("90"),
            coverage_status="READY", current_value_base=Decimal("0"),
            current_weight=Decimal("0"), country="US",
            opportunity_score_raw=Decimal("1.0"),
        )
        recommendation = build_recommendations(
            PortfolioContext(
                total_value_base=Decimal("100000"), cash_base=Decimal("30000"),
                risk_profile="Growth", base_currency="USD",
            ),
            [candidate],
            as_of="2026-08-06",
        )[0]

        self.assertEqual(recommendation.action, "HOLD")
        self.assertIn("financing", recommendation.suppression_reasons)

    def test_recent_shelf_filing_suppresses_score_driven_increase(self):
        recommendation = build_recommendations(
            PortfolioContext(
                total_value_base=Decimal("100000"), cash_base=Decimal("30000"),
                risk_profile="Growth", base_currency="USD",
                financing_policy=self.financing_policy(),
            ),
            [CandidateSignal(
                security_sk=1, ticker="SHELF", opportunity_score=Decimal("90"),
                coverage_status="READY", current_value_base=Decimal("0"),
                current_weight=Decimal("0"), country="US",
                opportunity_score_raw=Decimal("1.0"),
                financing_record_available=True,
                diluted_share_growth_yoy=Decimal("0.01"), is_burning_cash=False,
                days_since_shelf_filing=10, shelf_form="S-3ASR",
            )],
            as_of="2026-08-06",
        )[0]

        self.assertEqual(recommendation.action, "HOLD")
        self.assertIn("financing", recommendation.suppression_reasons)

    def test_service_preserves_withheld_holding_score_and_classification(self):
        identity = SimpleNamespace(product_user=lambda _: SimpleNamespace(
            user_sk="owner-a", risk_profile="Growth", base_currency="USD",
        ))
        portfolio = SimpleNamespace(
            portfolio_summary=lambda _: {
                "status": "ready", "base_currency": "USD",
                "valuation_as_of": "2026-08-05", "total_cash_base": "1000.00",
                "total_value_base": "100000.00", "holdings": [{
                    "security_sk": 202, "ticker": "RGTI", "country": "US",
                    "market_value_base": "2500.00", "weight": "0.025",
                    "theme_id": "quantum_computing",
                }],
            },
            annual_trade_count=lambda *_: 0,
        )
        service = RecommendationService(
            identity,
            portfolio,
            InMemoryOpportunitySignalRepository([{
                "security_sk": 101, "ticker": "MSFT", "opportunity_score": "80",
                "coverage_status": "READY", "country": "US", "spread_bps": "5",
                "theme_id": "enterprise_technology", "coverage_reasons": [],
                "as_of": "2026-08-05",
            }]),
        )

        result = service.recommendations("principal-a")
        rgti = next(row for row in result["recommendations"] if row["ticker"] == "RGTI")

        self.assertIsNone(rgti["opportunity_score"])
        self.assertEqual(rgti["coverage_status"], "WITHHELD")
        self.assertEqual(rgti["theme_id"], "quantum_computing")
        self.assertEqual(rgti["coverage_reasons"], ["missing:opportunity_score"])


if __name__ == "__main__":
    unittest.main()