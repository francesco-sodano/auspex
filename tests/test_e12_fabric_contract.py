from pathlib import Path
import unittest

from tests.fabric_notebook import notebook_code


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = ROOT / "fabric" / "warehouse"


class E12FabricContractTests(unittest.TestCase):
    def test_portfolio_notebook_mirrors_ledger_and_derives_valuation(self):
        notebook = notebook_code("nb_08_portfolio_derive")

        for table in [
            "silver_portfolio_transaction",
            "fact_portfolio_position",
            "fact_portfolio_valuation",
        ]:
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", notebook)
        self.assertIn("owner_user_sk", notebook)
        self.assertIn("knowledge_date", notebook)
        self.assertIn("fact_market_daily", notebook)
        self.assertIn("fact_fx_rate", notebook)
        self.assertIn("missing_prices", notebook)
        self.assertIn("missing_fx", notebook)
        self.assertIn("history:security:", notebook)
        self.assertIn("session_number", notebook)
        self.assertIn("F.col(\"session_number\") <= 7", notebook)
        self.assertIn("price_history_documents", notebook)
        self.assertIn('F.collect_list(F.struct("date", "price"))', notebook)
        self.assertIn('alias("prices")', notebook)
        function_app = (ROOT / "connectors" / "function_app.py").read_text(encoding="utf-8")
        self.assertIn('("quote:", "history:", "fx:", "score:security:")', function_app)
        self.assertIn('bw.read_serving_projection("market_history")', function_app)
        self.assertIn('F.when(F.col("coverage_complete")', notebook)
        self.assertIn("Files/serving/security_catalog", notebook)
        self.assertIn("Files/serving/market_data", notebook)
        self.assertIn("Files/serving/market_history", notebook)
        self.assertIn("ambiguous_current_tickers", notebook)
        self.assertIn("ambiguous_current_isins", notebook)
        self.assertIn("conflicting_portfolio_revisions", notebook)
        self.assertIn('dropDuplicates(["owner_user_sk", "transaction_id"])', notebook)
        self.assertIn("corrects_transaction_id", notebook)
        self.assertIn("linked_transaction_id", notebook)
        self.assertIn("cost_category", notebook)
        self.assertIn("gross_amount", notebook)
        self.assertIn("source_amount", notebook)
        self.assertIn("fx_rate_to_settlement", notebook)
        self.assertIn("affects_cash", notebook)
        self.assertIn("superseded_transaction_ids", notebook)
        self.assertIn("effective_ledger", notebook)
        self.assertIn("invalid_correction_order", notebook)
        self.assertNotIn("correction_chains", notebook)
        self.assertIn("ledger_asof", notebook)
        self.assertIn('F.col("event_date") <= F.to_date(F.lit(to_date))', notebook)
        self.assertIn('F.col("knowledge_date") <= F.to_date(F.lit(to_date))', notebook)
        self.assertIn("invalid_security_currencies", notebook)
        self.assertIn('positions.join(', notebook)
        self.assertIn('"security_sk"', notebook)
        self.assertIn("position_missing_fx", notebook)
        self.assertIn("position_usd_value", notebook)
        self.assertIn('F.col("quantity").cast(DecimalType(20, 8)).alias("quantity")', notebook)
        self.assertIn("market_value_base", notebook)
        self.assertIn("gics_sector", notebook)
        self.assertIn("country", notebook)
        self.assertIn("position_weight", notebook)
        self.assertIn(').alias("usd_fx")', notebook)
        self.assertIn("dated_fx", notebook)
        self.assertIn("latest_quotes_by_security", notebook)
        self.assertIn("latest_quotes_by_ticker", notebook)
        self.assertIn('F.lit("quote:security:")', notebook)
        self.assertIn('F.lit("fx_alias")', notebook)
        self.assertIn('F.lit("fx")', notebook)
        self.assertIn("invalid_portfolio_rows", notebook)
        self.assertIn("unresolved_security_events", notebook)
        self.assertIn('F.col("transaction_type").isin("OPENING_POSITION", "BUY", "SELL", "DIVIDEND")', notebook)
        self.assertIn("fx_rate_to_base", notebook)
        self.assertIn('F.col("transaction_type").isin("OPENING_POSITION", "BUY", "SELL")', notebook)
        self.assertIn("portfolio_snapshot_manifest", notebook)
        self.assertIn("snapshot_id", notebook)
        self.assertIn("'completed' AS status", notebook)

    def test_metrics_priority_date_fails_before_destructive_rebuild(self):
        notebook = notebook_code("nb_04_metrics")

        validation = notebook.index("priority_as_of_date must exist in fact_market_daily")
        score_delete = notebook.index('DeltaTable.forName(spark, "fact_theme_opportunity_score").delete()')
        self.assertLess(validation, score_delete)

    def test_warehouse_portfolio_contract_is_owner_scoped(self):
        dimensions = (WAREHOUSE / "06_portfolio_dims.sql").read_text(encoding="utf-8")
        facts = (WAREHOUSE / "07_portfolio_facts.sql").read_text(encoding="utf-8")
        views = (WAREHOUSE / "08_portfolio_views.sql").read_text(encoding="utf-8")
        promotion = (WAREHOUSE / "09_promote_portfolio_snapshot.sql").read_text(encoding="utf-8")

        self.assertIn("dbo.dim_account", dimensions)
        for table in [
            "dbo.fact_portfolio_transaction",
            "dbo.fact_portfolio_position",
            "dbo.fact_portfolio_valuation",
        ]:
            self.assertIn(table, facts)
            self.assertIn("owner_user_sk", facts)
        for view in [
            "dbo.v_effective_portfolio_transactions",
            "dbo.v_cash_balance",
            "dbo.v_portfolio_positions",
            "dbo.v_portfolio_summary",
            "dbo.v_portfolio_exposures",
            "dbo.v_rebalance_inputs",
        ]:
            self.assertIn(view, views)
        self.assertIn("owner_user_sk", views)
        self.assertIn("fx_rate_to_base", facts)
        self.assertIn("base_currency", facts)
        self.assertIn("corrects_transaction_id", facts)
        self.assertIn("linked_transaction_id", facts)
        self.assertIn("cost_category", facts)
        self.assertIn("gross_amount", facts)
        self.assertIn("source_amount", facts)
        self.assertIn("fx_rate_to_settlement", facts)
        self.assertIn("affects_cash", facts)
        self.assertIn("parent_correction", views)
        self.assertIn("market_value_base", facts)
        self.assertIn("position_weight", facts)
        self.assertIn("gics_sector", facts)
        self.assertIn("country", facts)
        self.assertIn("CREATE OR ALTER PROCEDURE dbo.usp_promote_portfolio_snapshot", promotion)
        self.assertIn("BEGIN TRANSACTION", promotion)
        self.assertIn("ROLLBACK TRANSACTION", promotion)
        self.assertIn("FROM auspex_bronze.dbo.silver_portfolio_transaction", promotion)
        self.assertIn("FROM auspex_bronze.dbo.fact_portfolio_position", promotion)
        self.assertIn("FROM auspex_bronze.dbo.fact_portfolio_valuation", promotion)
        self.assertIn("corrects_transaction_id", promotion)
        self.assertIn("linked_transaction_id", promotion)
        self.assertIn("cost_category", promotion)
        self.assertIn("market_value_base", promotion)
        self.assertIn("position_weight", promotion)
        self.assertIn("portfolio_snapshot_manifest", promotion)
        self.assertIn("status = 'completed'", promotion)


if __name__ == "__main__":
    unittest.main()