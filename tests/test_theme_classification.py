import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "connectors"))

from search.theme_classification import ThemeClassificationService
from theme_classifier.connector import extract_business_section
from engine.thesis import LEG_WEIGHTS, OpportunityObservation, score_theme


class FakeChat:
    def __init__(self, payload):
        self.payload = payload

    def complete_json(self, messages):
        self.messages = messages
        return json.dumps(self.payload)


class ThemeClassificationTests(unittest.TestCase):
    def test_connector_does_not_request_forced_sec_compression(self):
        source = (Path(__file__).resolve().parents[1] / "connectors" / "theme_classifier" / "connector.py").read_text(encoding="utf-8")

        self.assertNotIn("Accept-Encoding", source)

    def test_extracts_10k_item_one_business_section(self):
        document = (
            "<html><body><h2>Item 1. Business</h2><p>" +
            ("We design data center power and cooling infrastructure. " * 12) +
            "</p><h2>Item 1A. Risk Factors</h2></body></html>"
        )

        section = extract_business_section(document, "10-K")

        self.assertIn("data center power", section)
        self.assertNotIn("Risk Factors", section)

    def test_classifier_caps_confidence_and_preserves_llm_provenance(self):
        chat = FakeChat({
            "theme_id": "data_center_buildout",
            "confidence": 0.98,
            "rationale": "The filing describes power and cooling systems for data centers.",
        })
        service = ThemeClassificationService(
            chat,
            {"data_center_buildout": "Data Center Buildout"},
        )

        result = service.classify(
            ticker="VRT",
            company_name="Vertiv Holdings Co",
            filing_type="10-K",
            business_description="Data center infrastructure. " * 20,
        )

        self.assertEqual(result.theme_id, "data_center_buildout")
        self.assertEqual(result.confidence, 0.85)
        self.assertEqual(result.provenance, "llm")

    def test_classifier_rejects_theme_outside_catalog(self):
        service = ThemeClassificationService(
            FakeChat({"theme_id": "invented", "confidence": 0.7, "rationale": "No."}),
            {"healthcare": "Healthcare"},
        )

        with self.assertRaisesRegex(ValueError, "outside the allowed catalog"):
            service.classify(
                ticker="TEST",
                company_name="Test",
                filing_type="10-K",
                business_description="Business description. " * 20,
            )

    def test_classifier_rejects_short_description(self):
        service = ThemeClassificationService(
            FakeChat({"theme_id": "healthcare", "confidence": 0.7, "rationale": "Healthcare."}),
            {"healthcare": "Healthcare"},
        )

        with self.assertRaisesRegex(ValueError, "too short"):
            service.classify(
                ticker="TEST",
                company_name="Test",
                filing_type="10-K",
                business_description="Short",
            )

    def test_llm_classification_forces_partial_score_coverage(self):
        from datetime import date, datetime, timezone

        observations = [OpportunityObservation(
            theme_id="data_center_buildout",
            security_sk=index,
            date_sk=20260804,
            as_of=date(2026, 8, 4),
            candidate_source="LLM" if index == 1 else "TRS",
            candidate_snapshot_id=f"snapshot-{index}",
            candidate_snapshot_ingest_ts=datetime(2026, 8, 4, tzinfo=timezone.utc),
            membership_weight=float(index),
            news_volume_z_30d=float(index),
            insider_net_buy_ratio_90d=float(index),
            insider_cluster_buy_30d=float(index),
            inst_net_flow_qoq=float(index),
            inst_new_initiations=float(index),
            contract_award_usd_trailing_90d=float(index),
            activist_13d_flag=False,
            profit_margin=float(index),
            rev_growth_yoy=float(index),
            fcf_yield=float(index),
            net_debt_to_ebitda=float(index),
            fundamental_anchor_z=float(index),
            news_count_30d=float(index),
            institutional_holder_count_120d=float(index),
            max_knowledge_date=date(2026, 8, 4),
        ) for index in range(1, 9)]

        result = {row.security_sk: row for row in score_theme(observations, LEG_WEIGHTS)}[1]

        self.assertEqual(result.coverage_status, "PARTIAL")
        self.assertIn("classification:llm", result.coverage_reasons)


if __name__ == "__main__":
    unittest.main()