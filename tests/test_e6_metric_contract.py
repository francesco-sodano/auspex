from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOKS = ROOT / "fabric" / "notebooks"
WAREHOUSE = ROOT / "fabric" / "warehouse"
METRICS = WAREHOUSE / "metrics"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class E6MetricContractTests(unittest.TestCase):
    def test_metrics_notebook_creates_metric_config_and_feature_contract(self):
        nb = _read(NOTEBOOKS / "nb_04_metrics.py")

        self.assertIn("CREATE TABLE IF NOT EXISTS metric_weights", nb)
        self.assertIn("CREATE TABLE IF NOT EXISTS security_daily_features", nb)
        self.assertIn("composite_growth_score", nb)
        self.assertIn("opportunity_score", nb)
        self.assertIn("INCOMPLETE_E6A_WAITING_E8_E14", nb)
        self.assertIn("_replace_delta_projection", nb)
        self.assertIn("CREATE TABLE {table_name} USING DELTA AS", nb)
        self.assertIn("DROP VIEW IF EXISTS", nb)
        self.assertIn("v_security_daily_features", nb)

    def test_metrics_notebook_uses_pit_filters_and_validation(self):
        nb = _read(NOTEBOOKS / "nb_04_metrics.py")

        self.assertIn("knowledge_date", nb)
        self.assertIn("F.greatest(F.col(\"price_event_date\"), F.col(\"market_knowledge_date\"))", nb)
        self.assertIn("asof_latest_window", nb)
        self.assertIn("F.col(\"i.knowledge_date\") <= F.col(\"d.as_of\")", nb)
        self.assertIn("max_knowledge_date > as_of", nb)
        self.assertIn("row_count == 0", nb)
        self.assertIn("serving_row_count != row_count", nb)
        self.assertIn("E6 validation failed", nb)

    def test_metrics_notebook_implements_deterministic_score_recipe(self):
        nb = _read(NOTEBOOKS / "nb_04_metrics.py")

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
        self.assertIn("valuation_brake_z", legs)
        self.assertIn("dbo.v_opportunity_score", score)
        self.assertIn("INCOMPLETE_E6A_WAITING_E8_E14", score)


if __name__ == "__main__":
    unittest.main()
