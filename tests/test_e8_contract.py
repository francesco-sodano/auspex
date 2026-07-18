from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONNECTORS = ROOT / "connectors"
NOTEBOOKS = ROOT / "fabric" / "notebooks"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class E8ContractTests(unittest.TestCase):
    def test_function_app_registers_remaining_connectors(self):
        app = _read(CONNECTORS / "function_app.py")
        for source_id in [
            "alpha_vantage", "sec_13f", "sec_13dg", "sec_8k", "sec_s1",
            "news", "contracts", "etf_holdings",
        ]:
            self.assertIn(f'"{source_id}"', app)

    def test_source_seed_marks_e8_sources_implemented_but_disabled(self):
        seed = _read(CONNECTORS / "shared" / "sources_seed.json")
        for source_id in ["alpha_vantage", "sec_13f", "sec_13dg", "sec_8k", "sec_s1", "news", "contracts", "etf_holdings"]:
            self.assertIn(f'"source_id": "{source_id}"', seed)
            self.assertIn('"implementation_status": "implemented"', seed)
        self.assertIn('"enabled": false', seed)

    def test_alpha_vantage_gold_notebook_outputs_required_facts(self):
        nb = _read(NOTEBOOKS / "nb_05_alpha_vantage_to_gold.py")
        for table in [
            "fact_fundamentals",
            "fact_company_news",
            "fact_news_sentiment",
            "fact_macro",
            "fact_fx_rate",
            "fact_institutional_holding",
            "fact_theme_membership",
        ]:
            self.assertIn(table, nb)
        self.assertIn("v_fundamentals_latest", nb)
        self.assertIn("missing_pit", nb)
        self.assertIn("event_date", nb)
        self.assertIn("knowledge_date", nb)
        self.assertIn("spark.read.text(paths)", nb)
        self.assertNotIn("spark.read.json(paths)", nb)

    def test_sec_and_contract_notebooks_output_required_facts(self):
        sec_nb = _read(NOTEBOOKS / "nb_06_sec_filings_to_gold.py")
        contracts_nb = _read(NOTEBOOKS / "nb_07_contracts_to_gold.py")

        for table in ["fact_institutional_holding", "fact_ownership_event", "fact_material_event"]:
            self.assertIn(table, sec_nb)
        self.assertIn("missing_pit", sec_nb)
        self.assertIn("fact_contract_award", contracts_nb)
        self.assertIn("missing_pit", contracts_nb)
        self.assertIn("USASpending", contracts_nb)

    def test_e8_facts_are_consumed_by_e6_feature_contract(self):
        nb = _read(NOTEBOOKS / "nb_04_metrics.py")

        for table in [
            "fact_fundamentals",
            "fact_news_sentiment",
            "fact_company_news",
            "fact_contract_award",
            "fact_institutional_holding",
            "fact_ownership_event",
        ]:
            self.assertIn(table, nb)

        for column in [
            "pe_ratio",
            "rev_growth_yoy",
            "news_sentiment_ewma_14d",
            "news_volume_z_30d",
            "contract_award_usd_trailing_90d",
            "inst_net_flow_qoq",
            "inst_new_initiations",
            "activist_13d_flag",
        ]:
            self.assertIn(column, nb)

        self.assertIn("knowledge_date", nb)
        self.assertIn("<= F.col(\"d.as_of\")", nb)
        self.assertIn("Window.partitionBy(F.col(\"d.security_sk\"), F.col(\"d.date_sk\"))", nb)
        self.assertNotIn("Window.partitionBy(\"security_sk\", \"date_sk\").orderBy(F.col(\"f.knowledge_date\")", nb)

    def test_warehouse_sql_defines_e8_fact_contract(self):
        sql = _read(ROOT / "fabric" / "warehouse" / "04_e8_facts.sql")
        for table in [
            "fact_fundamentals",
            "fact_company_news",
            "fact_theme_membership",
            "fact_material_event",
            "fact_sec_filing_event",
            "v_fundamentals_latest",
            "v_company_news",
            "v_news_sentiment_30d",
        ]:
            self.assertIn(f"dbo.{table}", sql)
        self.assertIn("event_date", sql)
        self.assertIn("knowledge_date", sql)


if __name__ == "__main__":
    unittest.main()
