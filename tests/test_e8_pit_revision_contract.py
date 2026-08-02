from pathlib import Path
from datetime import date
import unittest


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = ROOT / "fabric" / "warehouse"

from tests.fabric_notebook import notebook_code


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class E8PitRevisionContractTests(unittest.TestCase):
    def test_pit_examples_preserve_non_null_metrics_and_collapse_revisions(self):
        fundamentals = [
            {"knowledge_date": date(2026, 1, 2), "pe_ratio": 18.0, "rev_growth_yoy": None},
            {"knowledge_date": date(2026, 2, 2), "pe_ratio": None, "rev_growth_yoy": 0.12},
        ]
        eligible_fundamentals = [
            row for row in fundamentals if row["knowledge_date"] <= date(2026, 2, 15)
        ]

        def latest_non_null(metric_name: str):
            return next(
                row[metric_name]
                for row in sorted(
                    eligible_fundamentals,
                    key=lambda row: row["knowledge_date"],
                    reverse=True,
                )
                if row[metric_name] is not None
            )

        self.assertEqual(latest_non_null("pe_ratio"), 18.0)
        self.assertEqual(latest_non_null("rev_growth_yoy"), 0.12)

        contract_revisions = [
            {"award_id": "A", "knowledge_date": date(2026, 1, 3), "amount": 100.0},
            {"award_id": "A", "knowledge_date": date(2026, 2, 3), "amount": 125.0},
            {"award_id": "B", "knowledge_date": date(2026, 1, 5), "amount": 50.0},
        ]
        latest_by_award = {}
        for row in contract_revisions:
            if row["knowledge_date"] <= date(2026, 1, 31):
                latest_by_award[row["award_id"]] = row
        self.assertEqual(sum(row["amount"] for row in latest_by_award.values()), 150.0)

        holding_revisions = [
            {"period": date(2025, 9, 30), "knowledge_date": date(2025, 11, 14), "shares": 100.0},
            {"period": date(2025, 9, 30), "knowledge_date": date(2026, 3, 1), "shares": 140.0},
            {"period": date(2025, 12, 31), "knowledge_date": date(2026, 2, 14), "shares": 160.0},
        ]
        eligible_holdings = [
            row for row in holding_revisions if row["knowledge_date"] <= date(2026, 2, 20)
        ]
        latest_by_period = {}
        for row in eligible_holdings:
            latest_by_period[row["period"]] = row
        ordered_holdings = [latest_by_period[period] for period in sorted(latest_by_period)]
        self.assertEqual(ordered_holdings[1]["shares"] - ordered_holdings[0]["shares"], 60.0)

    def test_warehouse_facts_match_e8_gold_revision_and_provenance_columns(self):
        base_facts = _read(WAREHOUSE / "02_facts.sql")
        e8_facts = _read(WAREHOUSE / "04_e8_facts.sql")

        for column in [
            "fundamentals_kind",
            "fundamentals_revision_hash",
            "news_revision_hash",
            "holding_revision_hash",
            "ownership_revision_hash",
            "material_event_revision_hash",
            "filing_revision_hash",
            "contract_revision_hash",
        ]:
            self.assertIn(column, base_facts + e8_facts)

        for column in [
            "silver_natural_key",
            "silver_batch_id",
            "silver_ingest_ts",
            "silver_source_record_hash",
            "silver_loaded_at",
        ]:
            self.assertIn(column, base_facts)
            self.assertIn(column, e8_facts)

        self.assertIn("silver_batch_id        VARCHAR(256)", e8_facts)

        self.assertIn("award_id", base_facts)
        self.assertIn("entity_sk", base_facts)
        self.assertIn("published_at", base_facts)
        self.assertIn("published_at", e8_facts)

    def test_warehouse_revision_migrations_fail_closed_without_fabricated_values(self):
        facts_sql = (
            _read(WAREHOUSE / "02_facts.sql")
            + _read(WAREHOUSE / "03_fx.sql")
            + _read(WAREHOUSE / "04_e8_facts.sql")
        )

        self.assertIn("Silver-backed staged reload", facts_sql)
        self.assertNotIn("REPLICATE('0', 64)", facts_sql)
        self.assertNotIn("'1900-01-01'", facts_sql)
        self.assertNotIn("ISNULL(CAST(", facts_sql)

    def test_warehouse_serving_views_have_explicit_pit_asof_semantics(self):
        sql = _read(WAREHOUSE / "04_e8_facts.sql")

        self.assertNotIn("WHERE knowledge_date <= event_date", sql)
        self.assertIn("dbo.v_fundamentals_daily_asof", sql)
        self.assertIn("dbo.v_news_sentiment_daily_asof", sql)
        self.assertIn("f.knowledge_date <= a.as_of", sql)
        self.assertIn("n.knowledge_date <= a.as_of", sql)
        self.assertIn("IS NOT NULL", sql)
        self.assertIn("fundamentals_revision_hash", sql)
        self.assertIn("news_revision_hash", sql)

    def test_metrics_rank_eligible_revisions_before_aggregation_and_lag(self):
        nb = notebook_code("nb_04_metrics")

        for column in [
            "fundamentals_revision_hash",
            "silver_loaded_at",
            "award_id",
            "transaction_id",
            "contract_revision_hash",
            "accession_no",
            "holding_revision_hash",
            "ownership_revision_hash",
        ]:
            self.assertIn(column, nb)

        self.assertIn("_latest_non_null_fundamental_metric", nb)
        self.assertIn("eligible_contract_revisions", nb)
        self.assertIn("latest_contract_revisions", nb)
        self.assertIn("eligible_holding_revisions", nb)
        self.assertIn("latest_holding_revisions", nb)
        self.assertIn("eligible_ownership_revisions", nb)
        self.assertIn("latest_ownership_revisions", nb)
        self.assertIn("eligible_news_sentiment_revisions", nb)
        self.assertIn("latest_news_sentiment_revisions", nb)
        self.assertIn("eligible_company_news_revisions", nb)
        self.assertIn("latest_company_news_revisions", nb)

        self.assertLess(nb.index("latest_contract_revisions"), nb.index("contracts_90d ="))
        self.assertLess(nb.index("latest_holding_revisions"), nb.index("inst_with_delta ="))
        self.assertLess(nb.index("inst_with_delta ="), nb.index("institutional_metrics ="))
        self.assertLess(nb.index("latest_ownership_revisions"), nb.index("ownership_metrics ="))
        self.assertLess(nb.index("latest_news_sentiment_revisions"), nb.index("news_sentiment_30d ="))
        self.assertLess(nb.index("latest_company_news_revisions"), nb.index("news_counts ="))
        self.assertLess(nb.index("latest_contract_revisions"), nb.index('.filter(F.col("event_date") >= F.date_sub(F.col("as_of"), 89))'))
        self.assertLess(nb.index("latest_holding_revisions"), nb.index("inst_delta_window"))
        self.assertLess(nb.index("latest_ownership_revisions"), nb.index('.filter(F.col("event_date").isNull() | (F.col("event_date") >= F.date_sub(F.col("as_of"), 365)))'))
        self.assertIn('Window.partitionBy(\n    "date_sk", "transaction_id"', nb)
        self.assertIn('"security_sk", "date_sk", "source_sk", "entity_sk", "event_date"', nb)
        self.assertIn('"security_sk", "date_sk", "source_sk", "entity_sk"', nb)
        self.assertIn('"security_sk", "date_sk", "accession_no", "entity_sk"', nb)

        old_lag_source = "fact_institutional_holding\n    .filter"
        self.assertNotIn(old_lag_source, nb)
        self.assertNotIn(').orderBy("event_date", "holding_natural_key")', nb)


if __name__ == "__main__":
    unittest.main()