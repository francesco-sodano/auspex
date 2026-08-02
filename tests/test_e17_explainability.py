from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest

from api.auspex_api.decision_log import InMemoryDecisionLogRepository
from api.auspex_api.metric_metadata import METRIC_METADATA
from api.auspex_api.recommendation_events import (
    InMemoryRecommendationEventRepository,
    RecommendationExperienceService,
)


ROOT = Path(__file__).resolve().parents[1]


class E17ExplainabilityTests(unittest.TestCase):
    def test_all_home_metrics_have_metadata(self):
        required = {
            "portfolio_value", "net_contributed_capital", "total_gain_loss",
            "cash_available", "stocks_value", "position_weight", "latest_price",
            "opportunity_score", "target_weight", "suggested_amount",
            "estimated_cost", "confidence", "monthly_outlook_range", "thesis_linkage",
            "attention_acceleration", "smart_money", "fundamental_health",
            "valuation_brake", "crowding_positioning",
        }

        self.assertTrue(required.issubset(METRIC_METADATA))
        for key in required:
            metadata = METRIC_METADATA[key]
            self.assertEqual(metadata["key"], key)
            self.assertTrue(metadata["display_name"])
            self.assertTrue(metadata["plain_description"])
            self.assertIn(metadata["direction"], {
                "higher_is_better", "lower_is_better", "contextual",
            })
            self.assertIn(metadata["tier"], {"simple", "advanced"})

    def test_score_projection_contains_all_six_attribution_legs(self):
        notebook = (
            ROOT / "fabric" / "nb_08_portfolio_derive.Notebook" / "notebook-content.py"
        ).read_text(encoding="utf-8")
        recommendation_service = (
            ROOT / "api" / "auspex_api" / "recommendations.py"
        ).read_text(encoding="utf-8")

        for leg in (
            "thesis_linkage", "attention_acceleration", "smart_money",
            "fundamental_health", "valuation_brake", "crowding_positioning",
        ):
            self.assertIn(f'("{leg}", "{leg}_contribution")', notebook)
            self.assertIn(f'"{leg}_contribution"', notebook)
        self.assertIn('"attribution"', notebook)
        self.assertIn('payload["opportunity_score"]', recommendation_service)
        self.assertIn('payload["attribution"]', recommendation_service)

    def test_metric_metadata_has_warehouse_and_api_contracts(self):
        sql = (
            ROOT / "fabric" / "warehouse" / "metrics" / "19_metric_metadata.sql"
        ).read_text(encoding="utf-8")
        function_app = (ROOT / "api" / "function_app.py").read_text(encoding="utf-8")

        self.assertIn("CREATE TABLE dbo.metric_metadata", sql)
        self.assertIn("plain_description", sql)
        self.assertIn('route="metric_metadata"', function_app)
        self.assertIn('route="recommendations/{recommendation_id}/disposition"', function_app)
        self.assertIn('route="recommendation_history"', function_app)
        self.assertNotIn('route="recommendation_history/{owner_user_sk}"', function_app)

    def test_disposition_is_owner_implicit_idempotent_and_conflict_safe(self):
        response = {
            "status": "ready",
            "as_of": "2026-07-29",
            "recommendations": [{
                "recommendation_id": "rec-1",
                "ticker": "MSFT",
                "action": "TRIM",
            }],
        }
        identity = SimpleNamespace(
            product_user=lambda principal: SimpleNamespace(
                user_sk="owner-a" if principal == "principal-a" else "owner-b"
            )
        )
        events = InMemoryRecommendationEventRepository()
        service = RecommendationExperienceService(
            identity,
            SimpleNamespace(recommendations=lambda _: response),
            InMemoryDecisionLogRepository(),
            events,
            clock=lambda: datetime(2026, 7, 30, tzinfo=timezone.utc),
        )
        payload = {
            "client_request_id": "request-1",
            "disposition": "ACCEPTED",
        }

        first, created = service.record_disposition("principal-a", "rec-1", payload)
        replay, replay_created = service.record_disposition("principal-a", "rec-1", payload)

        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(first, replay)
        self.assertNotIn("owner_user_sk", first)
        self.assertEqual(service.history("principal-b")["events"], [])

        with self.assertRaisesRegex(ValueError, "different data"):
            service.record_disposition("principal-a", "rec-1", {
                **payload,
                "disposition": "DISMISSED",
            })

    def test_history_combines_immutable_decisions_and_user_events(self):
        response = {
            "status": "ready",
            "as_of": "2026-07-29",
            "recommendations": [{
                "recommendation_id": "rec-1", "ticker": "MSFT", "action": "TRIM",
            }],
        }
        service = RecommendationExperienceService(
            SimpleNamespace(product_user=lambda _: SimpleNamespace(user_sk="owner-a")),
            SimpleNamespace(recommendations=lambda _: response),
            InMemoryDecisionLogRepository(),
            InMemoryRecommendationEventRepository(),
            clock=lambda: datetime(2026, 7, 30, tzinfo=timezone.utc),
        )
        service.record_disposition("principal-a", "rec-1", {
            "client_request_id": "request-1", "disposition": "DISMISSED",
        })

        history = service.history("principal-a")

        self.assertEqual(history["current_dispositions"], {"rec-1": "DISMISSED"})
        self.assertEqual(history["events"][0]["recommendation_id"], "rec-1")
        self.assertEqual(history["events"][0]["recommendation"]["action"], "TRIM")

    def test_stale_recommendation_cannot_receive_disposition(self):
        service = RecommendationExperienceService(
            SimpleNamespace(product_user=lambda _: SimpleNamespace(user_sk="owner-a")),
            SimpleNamespace(recommendations=lambda _: {
                "status": "stale", "as_of": "2026-07-20",
                "recommendations": [{
                    "recommendation_id": "rec-1", "ticker": "MSFT", "action": "TRIM",
                }],
            }),
            InMemoryDecisionLogRepository(),
            InMemoryRecommendationEventRepository(),
        )

        with self.assertRaisesRegex(ValueError, "ready recommendation"):
            service.record_disposition("principal-a", "rec-1", {
                "client_request_id": "request-1", "disposition": "ACCEPTED",
            })

    def test_spa_uses_semantic_tables_metadata_attribution_and_history(self):
        app = (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")

        for contract in (
            "fetch('/api/metric_metadata')", "fetch('/api/recommendation_history')",
            "<table className=\"holdings-table\"", "<table className=\"recommendation-table\"",
            "recommendation.attribution", "Accept suggestion", "Dismiss suggestion",
            "does not place a trade", "Decision history", "citation.excerpt",
            "citation.event_date", "citation.content_status",
            "Monthly outlook", "The range is withheld until measured portfolio volatility",
        ):
            self.assertIn(contract, app)


if __name__ == "__main__":
    unittest.main()