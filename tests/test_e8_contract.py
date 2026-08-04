from pathlib import Path
import json
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONNECTORS = ROOT / "connectors"

from tests.fabric_notebook import notebook_code


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
        self.assertIn('"has_more": result.has_more', app)
        self.assertIn("_EXPECTED_SCHEMA_VERSIONS", app)
        self.assertIn("cp.upsert_source(source)", app)
        self.assertIn("source_id in _SOURCE_SEEDS", app)
        self.assertIn('"etf_symbols", "etf_series"', app)
        self.assertIn('"enabled", "schedule"', app)
        self.assertIn("contract_changed", app)
        self.assertIn('source.update(_SOURCE_SEEDS["portfolio"])', app)
        self.assertIn('route="serving_projection_status"', app)

    def test_source_seed_enables_mvp_feeds_and_keeps_fallbacks_disabled(self):
        sources = json.loads(_read(CONNECTORS / "shared" / "sources_seed.json"))
        by_id = {source["source_id"]: source for source in sources}
        for source_id in [
            "alpha_vantage", "sec_13f", "sec_13dg", "sec_8k", "sec_s1",
            "news", "contracts", "etf_holdings", "benchmark_prices",
            "sec_companyfacts", "sec_nport",
        ]:
            self.assertEqual(by_id[source_id]["implementation_status"], "implemented")
            self.assertTrue(by_id[source_id]["enabled"])
        self.assertFalse(by_id["prices_yf"]["enabled"])
        self.assertFalse(by_id["fundamentals"]["enabled"])
        self.assertIn("data center", by_id["contracts"]["search_terms"])

    def test_alpha_vantage_source_declares_paid_quota_and_cadence_profiles(self):
        sources = json.loads(_read(CONNECTORS / "shared" / "sources_seed.json"))
        by_id = {source["source_id"]: source for source in sources}

        self.assertEqual(by_id["alpha_vantage"]["rate_limit"]["requests_per_minute"], 75)
        self.assertEqual(by_id["prices_eod"]["rate_limit"]["requests_per_minute"], 75)
        self.assertEqual(by_id["etf_holdings"]["rate_limit"]["requests_per_minute"], 75)
        self.assertEqual(set(by_id["alpha_vantage"]["profiles"]), {
            "news_daily", "macro_daily", "themes_weekly",
            "fundamentals_quarterly", "holdings_quarterly",
        })
        self.assertEqual(by_id["fundamentals"]["implementation_status"], "planned")
        self.assertFalse(by_id["fundamentals"]["enabled"])
        self.assertEqual(by_id["portfolio"]["implementation_status"], "implemented")
        self.assertTrue(by_id["portfolio"]["enabled"])
        self.assertEqual(by_id["portfolio"]["schema_version"], 5)

    def test_alpha_vantage_gold_notebook_outputs_required_facts(self):
        nb = notebook_code("nb_05_alpha_vantage_to_gold")
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

    def test_alpha_vantage_notebook_builds_macro_and_theme_silver_dq_boundary(self):
        nb = notebook_code("nb_05_alpha_vantage_to_gold")

        for table in ["silver_macro_observation", "silver_fx_rate"]:
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", nb)
        for table in ["silver_theme_component_membership", "silver_theme_membership"]:
            self.assertIn(f'.saveAsTable("{table}")', nb)

        for control_table in [
            "silver_parse_errors",
            "silver_dq_quarantine",
            "silver_security_quarantine",
        ]:
            self.assertIn(control_table, nb)

        for revision_hash in [
            "macro_revision_hash",
            "fx_revision_hash",
            "theme_revision_hash",
            "snapshot_batch_id",
            "snapshot_ingest_ts",
        ]:
            self.assertIn(revision_hash, nb)

        self.assertIn('F.get_json_object("raw_json", "$.record.profile")', nb)
        self.assertIn("INVALID_MACRO_PIT_OR_VALUE", nb)
        self.assertIn("INVALID_FX_PIT_OR_RATE", nb)
        self.assertIn("INVALID_THEME_HOLDING", nb)
        self.assertIn("INCOMPLETE_THEME_SNAPSHOT", nb)
        self.assertIn("CONFLICTING_THEME_WEIGHT", nb)
        self.assertIn("NON_SECURITY_THEME_HOLDING", nb)
        self.assertIn("theme_non_security_rows", nb)
        self.assertIn("is_non_security_holding = F.coalesce", nb)
        self.assertIn("SECURITY_UNRESOLVED", nb)
        self.assertIn("_merge_insert_only", nb)
        self.assertIn("active snapshot", nb)
        self.assertIn("F.size(F.col(\"parsed_payload.data\")) == 0", nb)
        self.assertIn("F.size(F.col(\"parsed_payload.holdings\")) == 0", nb)
        self.assertIn("theme_security_lookup.valid_from", nb)
        self.assertIn("incomplete_theme_snapshots", nb)
        self.assertIn("immutable_audit_columns", nb)
        self.assertIn('F.col("knowledge_date")', nb)
        self.assertIn('dropDuplicates(["theme_row_key"])', nb)
        self.assertIn("theme_conflict_keys", nb)
        self.assertIn("eqNullSafe", nb)
        self.assertIn("data_center_buildout", nb)
        self.assertIn("missing_theme_components", nb)
        self.assertIn("weighted_theme_weight", nb)
        self.assertIn("OUT_OF_SCOPE_NON_US_LISTING", nb)
        self.assertIn('isin("NASDAQ", "NYSE", "CBOE")', nb)

    def test_macro_fx_history_and_current_theme_gold_read_silver(self):
        nb = notebook_code("nb_05_alpha_vantage_to_gold")

        silver_stage = nb.index("# --- E8 Silver: macro, FX, and theme membership ---")
        gold_stage = nb.index("# --- Gold promotion from E8 Silver: macro, FX, and themes ---")
        self.assertLess(silver_stage, gold_stage)
        self.assertIn('spark.table("silver_macro_observation")', nb[gold_stage:])
        self.assertIn('spark.table("silver_fx_rate")', nb[gold_stage:])
        self.assertIn('spark.table("silver_theme_membership")', nb[gold_stage:])
        self.assertIn("t.macro_revision_hash = s.macro_revision_hash", nb[gold_stage:])
        self.assertIn("t.fx_revision_hash = s.fx_revision_hash", nb[gold_stage:])
        self.assertIn('.saveAsTable("fact_theme_membership")', nb[gold_stage:])
        self.assertIn("gold_without_silver", nb[gold_stage:])
        self.assertIn("gold_macro_without_silver", nb[gold_stage:])
        self.assertIn("gold_fx_without_silver", nb[gold_stage:])
        self.assertIn("gold_theme_without_silver", nb[gold_stage:])

        direct_macro = 'raw.filter(F.col("function") == "TREASURY_YIELD")'
        direct_fx = 'raw.filter(F.col("function") == "CURRENCY_EXCHANGE_RATE")'
        direct_theme = 'raw.filter(F.col("function") == "ETF_PROFILE")'
        self.assertNotIn(direct_macro, nb[gold_stage:])
        self.assertNotIn(direct_fx, nb[gold_stage:])
        self.assertNotIn(direct_theme, nb[gold_stage:])

    def test_remaining_e8_gold_promotions_are_silver_backed(self):
        notebook_contracts = {
            "nb_05_alpha_vantage_to_gold": [
                ("silver_fundamentals", "fundamentals_revision_hash"),
                ("silver_news", "news_revision_hash"),
                ("silver_av_institutional_holding", "holding_revision_hash"),
            ],
            "nb_06_sec_filings_to_gold": [
                ("silver_sec_filing", "filing_revision_hash"),
                ("silver_13f_holding", "holding_revision_hash"),
                ("silver_ownership_event", "ownership_revision_hash"),
                ("silver_material_event", "material_event_revision_hash"),
            ],
            "nb_07_contracts_to_gold": [
                ("silver_contract_award", "contract_revision_hash"),
            ],
        }

        for notebook_name, tables in notebook_contracts.items():
            nb = notebook_code(notebook_name)
            self.assertIn("# --- Gold promotion from E8 Silver", nb)
            self.assertIn("gold_without_silver", nb)
            if notebook_name in {"nb_05_alpha_vantage_to_gold", "nb_06_sec_filings_to_gold"}:
                self.assertIn("silver_without_gold", nb)
            if notebook_name == "nb_07_contracts_to_gold":
                self.assertIn("resolved_silver_without_gold", nb)
                self.assertIn("entity_unresolved_without_quarantine", nb)
                self.assertIn("security_unresolved_without_quarantine", nb)
            for table_name, revision_hash in tables:
                self.assertIn(f"CREATE TABLE IF NOT EXISTS {table_name}", nb)
                self.assertIn(f'spark.table("{table_name}")', nb)
                self.assertIn(revision_hash, nb)

        alpha_nb = notebook_code("nb_05_alpha_vantage_to_gold")
        self.assertIn("event_date > knowledge_date", alpha_nb)
        self.assertIn("uncovered_legacy_fundamentals", alpha_nb)
        self.assertNotIn("legacy_company_news", alpha_nb)
        self.assertNotIn("legacy_news_sentiment", alpha_nb)
        self.assertIn('.saveAsTable("fact_company_news")', alpha_nb)
        self.assertIn('.saveAsTable("fact_news_sentiment")', alpha_nb)
        self.assertIn('F.col("g.event_date") == F.col("s.event_date")', alpha_nb)
        self.assertIn('F.lit("silver_av_institutional_holding").alias("silver_source_table")', alpha_nb)
        self.assertLess(
            alpha_nb.index("uncovered_legacy_fundamentals"),
            alpha_nb.index('DeltaTable.forName(spark, "fact_fundamentals").delete'),
        )

    def test_sec_and_contract_notebooks_output_required_facts(self):
        sec_nb = notebook_code("nb_06_sec_filings_to_gold")
        contracts_nb = notebook_code("nb_07_contracts_to_gold")

        for table in ["fact_institutional_holding", "fact_ownership_event", "fact_material_event"]:
            self.assertIn(table, sec_nb)
        self.assertIn("missing_pit", sec_nb)
        self.assertIn("_merge_canonical_silver", sec_nb)
        self.assertIn("earlier_observation", sec_nb)
        self.assertIn("exact_normalized_issuer_name_pit", sec_nb)
        self.assertIn("processed_batch_ids", sec_nb)
        self.assertNotIn('F.to_date("ingest_ts").between', sec_nb)
        self.assertIn("spark.read.text(source_paths)", sec_nb)
        self.assertNotIn("spark.read.json(paths)", sec_nb)
        self.assertIn("def _envelope_schema(include_document_content):", sec_nb)
        self.assertIn("def _read_envelopes(source_paths, include_document_content):", sec_nb)
        self.assertIn("envelope_frames.append(_read_envelopes(content_paths, True))", sec_nb)
        self.assertIn("envelope_frames.append(_read_envelopes(metadata_only_paths, False))", sec_nb)
        self.assertIn('F.col("envelope.record.matched_forms").alias("matched_forms")', sec_nb)
        self.assertNotIn('F.get_json_object("raw_json"', sec_nb)
        self.assertIn("native_13f_holdings = F.arrays_zip(", sec_nb)
        self.assertIn("xpath(information_table_content", sec_nb)
        self.assertIn("F.explode_outer(native_13f_holdings)", sec_nb)
        self.assertIn('F.col("source_id") == "sec_13f", F.lit(None).cast("string")', sec_nb)
        self.assertIn('F.col("source_id").isin("sec_8k", "sec_s1"), F.lit(None).cast("string")', sec_nb)
        self.assertIn('.withColumn("item_code", F.explode_outer("item_codes"))', sec_nb)
        self.assertIn('F.concat(F.lit("SEC Item "), F.col("item_code"))', sec_nb)
        self.assertIn("s1_material_rows = filing_pass.filter", sec_nb)
        self.assertIn('F.lit("SEC registration statement")', sec_nb)
        self.assertIn("parsed_material_rows.unionByName(s1_material_rows)", sec_nb)
        self.assertIn("F.size(_xpath_values", sec_nb)
        self.assertNotIn("ET.fromstring(information_text)", sec_nb)
        self.assertNotIn('re.findall(\n            r"<(?:(?:[A-Za-z0-9_]+):)?infoTable', sec_nb)
        self.assertIn("ArrayType(StringType())", sec_nb)
        self.assertIn('F.col("raw_content_present") & F.col("archive_complete")', sec_nb)
        self.assertIn('~F.col("raw_content_present")', sec_nb)
        self.assertIn("INCOMPLETE_ARCHIVE_EVIDENCE", sec_nb)
        self.assertIn("fact_contract_award", contracts_nb)
        self.assertIn("missing_pit", contracts_nb)
        self.assertIn('F.col("event_date") > F.col("knowledge_date")', contracts_nb)
        self.assertIn("USASpending", contracts_nb)
        self.assertIn("spark.read.text(paths)", contracts_nb)
        self.assertNotIn("spark.read.json(paths)", contracts_nb)
        self.assertIn('F.get_json_object("raw_json", "$.record.search_award")', contracts_nb)
        self.assertIn('F.get_json_object("raw_json", "$.record.award")', contracts_nb)
        self.assertIn('F.sha2(F.col("transaction_internal_id"), 256)', contracts_nb)
        self.assertIn("legacy_contract_ids", contracts_nb)
        self.assertIn("legacy contract transaction identities", contracts_nb)
        self.assertIn('DeltaTable.forName(spark, "silver_entity_quarantine").delete("source_id = \'contracts\'")', contracts_nb)
        self.assertIn('DeltaTable.forName(spark, "silver_security_quarantine").delete("source_id = \'contracts\'")', contracts_nb)
        self.assertIn('.whenNotMatchedBySourceDelete(condition="t.source_sk = 6")', contracts_nb)
        self.assertIn('F.coalesce(F.col("transaction_internal_id"), F.lit("missing"))', contracts_nb)
        self.assertIn('F.sha2(F.coalesce(F.col("raw_record"), F.lit("")), 256)', contracts_nb)

    def test_e8_facts_are_consumed_by_e6_feature_contract(self):
        nb = notebook_code("nb_04_metrics")

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
        self.assertIn("theme_revision_hash", sql)
        self.assertIn("snapshot_batch_id", sql)
        self.assertIn("snapshot_ingest_ts", sql)

        base_facts = _read(ROOT / "fabric" / "warehouse" / "02_facts.sql")
        fx = _read(ROOT / "fabric" / "warehouse" / "03_fx.sql")
        self.assertIn("macro_revision_hash", base_facts)
        self.assertIn("fx_revision_hash", fx)
        self.assertIn("Silver-backed staged reload", base_facts)
        self.assertIn("Silver-backed staged reload", fx)
        self.assertIn("Silver-backed staged reload", sql)
        self.assertIn("macro_revision_hash CHAR(64) NOT NULL", base_facts)
        self.assertIn("fx_revision_hash CHAR(64)    NOT NULL", fx)
        self.assertIn("theme_revision_hash CHAR(64)  NOT NULL", sql)
        self.assertRegex(sql, r"filer_name\s+VARCHAR\(8000\)\s+NULL")
        self.assertIn("CREATE TABLE dbo.fact_macro_revisioned", base_facts)
        self.assertIn("CREATE TABLE dbo.fact_fx_rate_revisioned", fx)
        self.assertIn("CREATE TABLE dbo.fact_theme_membership_revisioned", sql)
        self.assertIn("EXEC sp_rename 'dbo.fact_macro'", base_facts)
        self.assertIn("EXEC sp_rename 'dbo.fact_fx_rate'", fx)
        self.assertIn("EXEC sp_rename 'dbo.fact_theme_membership'", sql)

    def test_preproduction_theme_tables_are_clean_current_state(self):
        nb = notebook_code("nb_05_alpha_vantage_to_gold")
        for table in [
            "dim_theme", "bridge_theme_etf",
            "silver_theme_component_membership", "silver_theme_membership",
            "fact_theme_membership",
        ]:
            self.assertIn(f'.saveAsTable("{table}")', nb)
        self.assertIn('mode("overwrite")', nb)
        self.assertIn("latest_theme_batches", nb)
        self.assertNotIn("UPDATE dim_theme SET is_active = false", nb)
        self.assertNotIn("UPDATE bridge_theme_etf SET is_active = false", nb)

    def test_nport_builds_current_candidate_cusip_bridge_for_13f(self):
        source_history = notebook_code("nb_13_source_history_to_silver")
        sec = notebook_code("nb_06_sec_filings_to_gold")
        self.assertIn("cusip_bridge_candidates", source_history)
        self.assertIn('spark.table("silver_nport_holding")', source_history)
        self.assertIn('saveAsTable("dim_security_identifier")', source_history)
        self.assertIn('F.countDistinct("security_sk").alias("security_count")', source_history)
        self.assertIn('F.lit("CUSIP").alias("identifier_type")', source_history)
        self.assertLess(
            source_history.index('"silver_nport_holding", resolved_holdings,'),
            source_history.index('spark.table("silver_nport_holding")'),
        )
        self.assertIn("OUT_OF_SCOPE_13F_HOLDING", sec)
        self.assertIn("AMBIGUOUS_13F_CANDIDATE_IDENTIFIER", sec)
        self.assertIn("processed_13f_batch_ids", sec)
        self.assertIn("t.source_id = 'sec_13f' AND t.batch_id = s.batch_id", sec)
        self.assertIn(".whenMatchedDelete()", sec)
        self.assertNotIn('delete(\n    "source_id = \'sec_13f\'"', sec)


if __name__ == "__main__":
    unittest.main()
