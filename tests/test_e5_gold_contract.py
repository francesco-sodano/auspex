from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = ROOT / "fabric" / "warehouse"

from tests.fabric_notebook import notebook_code


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class E5GoldContractTests(unittest.TestCase):
    def test_silver_to_gold_notebook_creates_core_gold_tables(self):
        nb = notebook_code("nb_03_silver_to_gold")

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
        nb = notebook_code("nb_03_silver_to_gold")

        self.assertIn("DeltaTable.forName", nb)
        self.assertIn("event_date", nb)
        self.assertIn("knowledge_date", nb)
        self.assertIn("fact_market_daily", nb)
        self.assertIn("fact_insider_txn", nb)
        self.assertIn("price_revision_hash", nb)
        self.assertIn("ingest_ts", nb)
        self.assertIn("event_date IS NULL OR knowledge_date IS NULL", nb)
        self.assertIn("event_date > knowledge_date", nb)
        self.assertIn('F.col("event_date") <= F.col("knowledge_date")', nb)
        self.assertIn("E5 validation failed", nb)
        self.assertIn("t.price_revision_hash = s.price_revision_hash", nb)
        self.assertIn("market_revision_duplicates", nb)
        self.assertIn("market_missing_revision_hash", nb)
        self.assertIn("market_missing_ingest_ts", nb)
        self.assertIn("market_missing_loaded_at", nb)
        self.assertIn("_ensure_not_null_constraints", nb)
        self.assertIn("ADD CONSTRAINT", nb)
        self.assertIn("uncovered_legacy_market_keys", nb)
        self.assertIn("MARKET FACT MIGRATION FAILED", nb)
        self.assertLess(
            nb.index('_merge_all(\n    "fact_market_daily"'),
            nb.index('delete("price_revision_hash IS NULL")'),
        )
        self.assertIn("t.insider_txn_sk = s.insider_txn_sk", nb)

    def test_silver_to_gold_notebook_validates_orphans_and_pit(self):
        nb = notebook_code("nb_03_silver_to_gold")

        self.assertIn("orphan_market", nb)
        self.assertIn("orphan_insider", nb)
        self.assertIn("missing_pit", nb)
        self.assertIn("date_candidates", nb)
        self.assertIn("F.sequence", nb)
        self.assertIn("trading_dates", nb)
        self.assertIn('F.coalesce(F.col("is_trading_day"), F.lit(False))', nb)
        self.assertIn('DeltaTable.forName(spark, "dim_date").delete', nb)

    def test_warehouse_ddl_files_define_e5_contract(self):
        dims = _read(WAREHOUSE / "01_dims.sql")
        facts = _read(WAREHOUSE / "02_facts.sql")
        fx = _read(WAREHOUSE / "03_fx.sql")

        for table in [
            "dim_security", "dim_theme", "bridge_theme_etf",
            "dim_date", "dim_entity", "dim_source",
        ]:
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

        self.assertIn("price_revision_hash", facts)
        self.assertIn("ingest_ts", facts)
        self.assertIn("revision_loaded_at", facts)
        self.assertIn("requires a staged reload", facts)
        self.assertIn("CREATE TABLE dbo.fact_market_daily_revisioned (", facts)
        self.assertIn("INSERT INTO dbo.fact_market_daily_revisioned", facts)
        self.assertIn("BEGIN TRANSACTION", facts)
        self.assertIn("EXEC sp_rename 'dbo.fact_market_daily'", facts)
        self.assertNotIn("ALTER COLUMN price_revision_hash", facts)

        self.assertIn("dbo.fact_fx_rate", fx)
        self.assertIn("event_date", fx)
        self.assertIn("knowledge_date", fx)
        self.assertIn("entity_natural_id VARCHAR(128)", dims)
        self.assertIn("@dim_entity_needs_width_upgrade", dims)
        self.assertIn("max_length < 128", dims)
        self.assertIn("CREATE TABLE dbo.dim_entity_wide", dims)
        self.assertIn("EXEC sp_rename 'dbo.dim_entity'", dims)
        self.assertNotIn("silver_batch_id           VARCHAR(128)", facts)

    def test_warehouse_snapshot_promotion_is_transactional_and_fail_closed(self):
        promotion = _read(WAREHOUSE / "05_promote_lakehouse_snapshot.sql")

        self.assertIn("CREATE OR ALTER PROCEDURE dbo.usp_promote_lakehouse_gold", promotion)
        self.assertIn("BEGIN TRANSACTION", promotion)
        self.assertIn("COMMIT TRANSACTION", promotion)
        self.assertIn("BEGIN TRY", promotion)
        self.assertIn("BEGIN CATCH", promotion)
        self.assertIn("IF @@TRANCOUNT > 0", promotion)
        self.assertIn("ROLLBACK TRANSACTION", promotion)
        self.assertIn("THROW;", promotion)
        self.assertIn("source_snapshot_manifest", promotion)
        self.assertIn("DECLARE @source_snapshot_manifest VARCHAR(8000)", promotion)
        self.assertIn("STRING_AGG", promotion)
        self.assertNotIn("@promotion_run_id VARCHAR(64),\n    @source_snapshot_manifest", promotion)
        self.assertIn("Warehouse per-table row-count reconciliation failed", promotion)
        for table in ["dim_theme", "bridge_theme_etf"]:
            self.assertIn(f"FROM auspex_bronze.dbo.{table}", promotion)
            self.assertIn(f"FROM dbo.{table}", promotion)
        for revision_grain in [
            "accession_no, line_no",
            "accession_no, security_sk, entity_sk, date_sk, holding_revision_hash",
            "accession_no, security_sk, entity_sk, event_date, ownership_revision_hash",
            "transaction_id, contract_revision_hash",
            "indicator_code, event_date, macro_revision_hash",
            "ccy_pair, event_date, fx_revision_hash",
            "snapshot_batch_id, theme_id, security_sk, event_date, theme_revision_hash",
            "event_sk, material_event_revision_hash",
            "filing_event_sk, filing_revision_hash",
            "security_sk, date_sk, model_version",
            "theme_id, security_sk, date_sk, model_version, weight_version",
            "as_of_date, model_version, weight_version",
        ]:
            self.assertIn(revision_grain, promotion)
        self.assertNotIn(
            "GROUP BY security_sk, entity_sk, date_sk, holding_revision_hash",
            promotion,
        )
        self.assertIn("Warehouse feature PIT validation failed", promotion)
        self.assertIn("Warehouse fundamental-anchor PIT/model validation failed", promotion)
        self.assertIn("Warehouse Opportunity Score contract validation failed", promotion)
        self.assertIn("Warehouse Opportunity Score manifest reconciliation failed", promotion)
        self.assertIn("Warehouse fact PIT validation failed", promotion)
        self.assertIn("Warehouse institutional-holding provenance validation failed", promotion)
        self.assertIn("Warehouse theme snapshot provenance validation failed", promotion)
        self.assertIn("Warehouse security dimension orphan validation failed", promotion)
        self.assertIn("Warehouse entity dimension orphan validation failed", promotion)
        self.assertIn("Warehouse source dimension orphan validation failed", promotion)
        self.assertIn("Warehouse date dimension orphan validation failed", promotion)
        for table in [
            "dim_security", "dim_theme", "bridge_theme_etf",
            "dim_date", "dim_entity", "dim_source",
            "fact_market_daily", "fact_insider_txn", "fact_institutional_holding",
            "fact_ownership_event", "fact_news_sentiment", "fact_contract_award",
            "fact_macro", "fact_fx_rate", "fact_fundamentals", "fact_company_news",
            "fact_theme_membership", "fact_material_event", "fact_sec_filing_event",
            "fact_fundamental_anchor",
            "metric_weights", "security_daily_features",
            "fact_theme_opportunity_score", "opportunity_score_snapshot_manifest",
        ]:
            self.assertIn(f"FROM auspex_bronze.dbo.{table}", promotion)


if __name__ == "__main__":
    unittest.main()