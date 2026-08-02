from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = ROOT / "fabric" / "warehouse"
METRICS = WAREHOUSE / "metrics"

from tests.fabric_notebook import notebook_code


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class E6MetricContractTests(unittest.TestCase):
    def test_metrics_notebook_creates_metric_config_and_feature_contract(self):
        nb = notebook_code("nb_04_metrics")

        self.assertIn("CREATE TABLE IF NOT EXISTS metric_weights", nb)
        self.assertIn("CREATE TABLE IF NOT EXISTS security_daily_features", nb)
        self.assertIn("CREATE TABLE IF NOT EXISTS fact_theme_opportunity_score", nb)
        self.assertIn("CREATE TABLE IF NOT EXISTS opportunity_score_snapshot_manifest", nb)
        self.assertIn('DeltaTable.forName(spark, "fact_theme_opportunity_score").delete()', nb)
        self.assertIn('DeltaTable.forName(spark, "opportunity_score_snapshot_manifest").delete()', nb)
        self.assertIn("if date_results:\n        manifest_records.append", nb)
        self.assertIn('"fact_narrative_premium", "fact_narrative_premium_evidence"', nb)
        self.assertIn("composite_growth_score", nb)
        self.assertIn("opportunity_score", nb)
        self.assertIn("THEME_CONTEXT_REQUIRED", nb)
        self.assertIn("OPPORTUNITY_MODEL_VERSION", nb)
        self.assertIn("OPPORTUNITY_WEIGHT_VERSION", nb)
        self.assertIn("latest_theme_snapshot_keys", nb)
        self.assertIn('spark.table("dim_theme").filter', nb)
        self.assertIn('Window.partitionBy(\n    "date_sk", "theme_id"', nb)
        self.assertNotIn('Window.partitionBy(\n    "date_sk", "theme_id", "etf_symbol"', nb)
        self.assertIn("opportunity_active_weights", nb)
        self.assertIn("score_theme(", nb)
        self.assertIn("v_security_score_attribution", nb)
        self.assertIn("_replace_delta_projection", nb)
        self.assertIn("CREATE TABLE {table_name} USING DELTA AS", nb)
        self.assertIn("DROP VIEW IF EXISTS", nb)
        self.assertIn("v_security_daily_features", nb)

    def test_metrics_notebook_uses_pit_filters_and_validation(self):
        nb = notebook_code("nb_04_metrics")

        self.assertIn("knowledge_date", nb)
        self.assertIn("F.greatest(F.col(\"price_event_date\"), F.col(\"price_knowledge_date\"))", nb)
        self.assertIn("asof_latest_window", nb)
        self.assertIn("market_revision_window", nb)
        self.assertIn("market_snapshot_window", nb)
        self.assertIn("price_revision_hash", nb)
        self.assertIn("revision_loaded_at", nb)
        self.assertIn("feature_built_at", nb)
        self.assertIn("No stale market snapshot dates; feature merge is a no-op", nb)
        self.assertNotIn("No stale market snapshot dates; refreshing the latest date", nb)
        self.assertIn("_MARKET_LOOKBACK_CALENDAR_DAYS = 550", nb)
        self.assertIn("_MAX_MARKET_SNAPSHOT_DATES_PER_RUN = 7", nb)
        self.assertIn("F.broadcast(processing_dates)", nb)
        self.assertIn("F.date_sub(F.col(\"dates.as_of\"), _MARKET_LOOKBACK_CALENDAR_DAYS)", nb)
        self.assertIn('F.col("prices.security_sk").alias("security_sk")', nb)
        self.assertIn("deferred_stale_dates", nb)
        self.assertIn("theme_source_freshness", nb)
        self.assertIn("score_manifest_freshness", nb)
        self.assertIn("score_stale_dates", nb)
        self.assertIn("whenMatchedDelete", nb)
        self.assertIn("E6 incremental refresh pending", nb)
        self.assertIn('F.col("prices.price_knowledge_date") <= F.col("dates.as_of")', nb)
        self.assertNotIn('.dropDuplicates(["security_sk", "price_event_date"])', nb)
        self.assertIn("F.col(\"i.knowledge_date\") <= F.col(\"d.as_of\")", nb)
        self.assertIn("max_knowledge_date > as_of", nb)
        self.assertIn("remaining_stale_snapshot_dates", nb)
        self.assertIn("missing_feature_built_at", nb)
        self.assertIn("row_count == 0", nb)
        self.assertIn("serving_row_count != row_count", nb)
        self.assertIn("E6 validation failed", nb)
        for field in [
            "insider_net_buy_ratio_90d", "inst_net_flow_qoq",
            "institutional_holder_count_120d", "news_count_30d",
            "news_volume_z_30d", "contract_award_usd_trailing_90d",
        ]:
            self.assertIn(f'.withColumn("{field}", F.coalesce', nb)

    def test_metrics_notebook_implements_deterministic_score_recipe(self):
        nb = notebook_code("nb_04_metrics")

        self.assertIn("percentile_approx", nb)
        self.assertIn("_winsor", nb)
        self.assertIn("_z", nb)
        self.assertIn("spark.advise.divisionExprConvertRule.enable", nb)
        self.assertIn("percent_rank", nb)
        self.assertIn("metric_weights", nb)
        self.assertIn("must sum to 1.000000", nb)

    def test_metrics_warehouse_sql_defines_required_views(self):
        base = _read(METRICS / "04_base_metrics.sql")
        legs = _read(METRICS / "12b_opportunity_legs.sql")
        score = _read(METRICS / "13_opportunity_score.sql")

        self.assertIn("dbo.metric_weights", base)
        for view_name in [
            "v_market_momentum",
            "v_market_risk",
            "v_risk_adjusted",
            "v_smart_money",
            "v_security_daily_features",
        ]:
            self.assertIn(f"dbo.{view_name}", base)

        for column in [
            "security_sk",
            "ticker",
            "as_of",
            "momentum_3m",
            "realized_vol_252d",
            "insider_net_buy_ratio_90d",
            "composite_growth_score",
            "opportunity_score",
            "score_status",
            "max_knowledge_date",
            "stale_sources_json",
        ]:
            self.assertIn(column, base)

        self.assertIn("dbo.v_opportunity_legs", legs)
        self.assertIn("dbo.fact_theme_opportunity_score", legs)
        self.assertIn("dbo.opportunity_score_snapshot_manifest", legs)
        self.assertIn("valuation_brake_z", legs)
        self.assertIn("dbo.v_opportunity_score", score)
        self.assertIn("dbo.v_security_score_attribution", score)
        self.assertIn("e6b_v1", score)
        self.assertIn("e6b_balanced_v1", score)
        self.assertIn("narrative_premium", score)
        self.assertNotIn("narrative_premium_contribution", score)


if __name__ == "__main__":
    unittest.main()
