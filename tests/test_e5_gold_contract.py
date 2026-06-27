from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOKS = ROOT / "fabric" / "notebooks"
WAREHOUSE = ROOT / "fabric" / "warehouse"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class E5GoldContractTests(unittest.TestCase):
    def test_silver_to_gold_notebook_creates_core_gold_tables(self):
        nb = _read(NOTEBOOKS / "nb_03_silver_to_gold.py")

        for table in [
            "dim_date",
            "dim_source",
            "dim_entity",
            "fact_market_daily",
            "fact_insider_txn",
            "fact_institutional_holding",
            "fact_ownership_event",
            "fact_news_sentiment",
            "fact_contract_award",
            "fact_macro",
            "fact_fx_rate",
        ]:
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", nb)

    def test_silver_to_gold_notebook_uses_idempotent_merges_and_pit_columns(self):
        nb = _read(NOTEBOOKS / "nb_03_silver_to_gold.py")

        self.assertIn("DeltaTable.forName", nb)
        self.assertIn("event_date", nb)
        self.assertIn("knowledge_date", nb)
        self.assertIn("fact_market_daily", nb)
        self.assertIn("fact_insider_txn", nb)
        self.assertIn("event_date IS NULL OR knowledge_date IS NULL", nb)
        self.assertIn("E5 validation failed", nb)
        self.assertIn("t.security_sk = s.security_sk AND t.date_sk = s.date_sk", nb)
        self.assertIn("t.insider_txn_sk = s.insider_txn_sk", nb)

    def test_silver_to_gold_notebook_validates_orphans_and_pit(self):
        nb = _read(NOTEBOOKS / "nb_03_silver_to_gold.py")

        self.assertIn("orphan_market", nb)
        self.assertIn("orphan_insider", nb)
        self.assertIn("missing_pit", nb)

    def test_warehouse_ddl_files_define_e5_contract(self):
        dims = _read(WAREHOUSE / "01_dims.sql")
        facts = _read(WAREHOUSE / "02_facts.sql")
        fx = _read(WAREHOUSE / "03_fx.sql")

        for table in ["dim_security", "dim_date", "dim_entity", "dim_source"]:
            self.assertIn(f"dbo.{table}", dims)

        for table in [
            "fact_market_daily",
            "fact_insider_txn",
            "fact_institutional_holding",
            "fact_ownership_event",
            "fact_news_sentiment",
            "fact_contract_award",
            "fact_macro",
        ]:
            self.assertIn(f"dbo.{table}", facts)
            self.assertIn("event_date", facts)
            self.assertIn("knowledge_date", facts)

        self.assertIn("dbo.fact_fx_rate", fx)
        self.assertIn("event_date", fx)
        self.assertIn("knowledge_date", fx)


if __name__ == "__main__":
    unittest.main()