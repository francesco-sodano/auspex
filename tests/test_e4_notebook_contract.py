import ast
from datetime import date
from decimal import Decimal
import unittest
import xml.etree.ElementTree as ET

from tests.fabric_notebook import notebook_code



def _read(name: str) -> str:
    return notebook_code(name)


class E4NotebookContractTests(unittest.TestCase):
    def test_entity_resolution_notebook_creates_canonical_dim_security(self):
        nb = _read("nb_00_entity_resolution")

        self.assertIn("CREATE TABLE IF NOT EXISTS dim_security", nb)
        self.assertIn("source_id = 'sec_company_tickers'", nb)
        self.assertIn("company_tickers_exchange.json", nb)
        self.assertIn('allowed_exchanges = {"NASDAQ", "NYSE", "CBOE"}', nb)
        self.assertIn('StructField("exchange", StringType(), False)', nb)
        for column in [
            "security_sk",
            "valid_from",
            "valid_to",
            "is_current",
            "resolution_method",
        ]:
            self.assertIn(column, nb)


    def test_quarantine_tables_have_replay_safe_natural_keys(self):
        nb = _read("nb_00_entity_resolution")

        self.assertIn("natural_key", nb)
        self.assertIn("MERGE", nb)
        self.assertIn("silver_security_quarantine", nb)
        self.assertIn("silver_dq_quarantine", nb)


    def test_form4_silver_resolves_security_sk_and_merges_quarantine(self):
        nb = _read("nb_01_form4_to_silver")

        self.assertNotIn("mssparkutils.widgets.get", nb)
        self.assertNotIn('globals().get(name)', nb)
        parameter_cell = nb.index("# --- Parameters: mark this cell as the Fabric parameter cell ---")
        normalization_cell = nb.index("# --- Normalize and validate injected parameter values ---")
        self.assertLess(parameter_cell, normalization_cell)
        for parameter in [
            "from_date",
            "to_date",
            "edgar_user_agent",
            "edgar_requests_per_minute",
            "max_workers",
            "write_batch_size",
            "retry_quarantine_reasons",
        ]:
            self.assertIn(f"{parameter} =", nb[parameter_cell:normalization_cell])
        self.assertIn("security_sk", nb)
        self.assertIn("SECURITY_UNRESOLVED", nb)
        self.assertIn("FORM4_PROCESSING_FAILED", nb)
        self.assertIn("FORM4_WORKER_FAILED", nb)
        self.assertIn("TXN_PARSE_FAILED", nb)
        self.assertIn("terminal_accessions", nb)
        self.assertIn('bronze_df.join(completed_accessions, "accession_no", "left_anti")', nb)
        self.assertIn("NO_NONDERIVATIVE_TXNS", nb)
        self.assertIn("PIT_MISSING", nb)
        self.assertIn("row.get(\"event_date\") > row.get(\"knowledge_date\")", nb)
        self.assertIn("event_date > knowledge_date", nb)
        self.assertIn("legacy_invalid_dates", nb)
        self.assertIn("Quarantined {legacy_invalid_dates.count()} legacy future-event Form 4 rows", nb)
        self.assertIn("retry_quarantine_reasons", nb)
        self.assertIn("_ACTIVE_TERMINAL_REASONS", nb)
        self.assertIn("selected_retry_accessions", nb)
        self.assertIn('.join(selected_retry_accessions, "accession_no", "left_anti")', nb)
        self.assertIn("selected_retry_rows", nb)
        self.assertIn('F.regexp_extract("natural_key", r":(\\d+)$", 1)', nb)
        self.assertIn('F.col("q.line_no").isNotNull() & F.col("ll.accession_no").isNull()', nb)
        self.assertIn('F.col("q.line_no").isNull() & F.col("la.accession_no").isNull()', nb)
        self.assertIn('F.get_json_object("raw_json", "$.record.ciks")', nb)
        self.assertIn("_archive_cik_candidates", nb)
        self.assertIn('/index.json', nb)
        self.assertIn("NO_OWNERSHIP_XML", nb)
        self.assertIn("ARCHIVE_NOT_FOUND", nb)
        self.assertIn('return None, "ARCHIVE_NOT_FOUND", "; ".join(attempts)', nb)
        self.assertIn("Removed {legacy_bad} legacy silver_insider_txn", nb)
        self.assertIn("DeltaTable.forName(spark, \"silver_security_quarantine\")", nb)
        self.assertIn("natural_key", nb)
        self.assertIn('"processed_accessions": len(to_process)', nb)
        self.assertIn("FORM 4 SILVER SUMMARY", nb)
        self.assertIn("mssparkutils.notebook.exit(run_summary_json)", nb)


    def test_form4_seeds_missing_historical_issuers_from_official_xml(self):
        nb = _read("nb_01_form4_to_silver")

        self.assertIn("_seed_historical_securities", nb)
        self.assertIn("SEC_FORM4_XML", nb)
        self.assertIn('F.lit("sec_form4")', nb)
        self.assertIn("F.xxhash64", nb)
        self.assertIn("whenNotMatchedInsertAll()", nb)
        self.assertIn("least(t.valid_from, s.valid_from)", nb)
        self.assertIn("greatest(t.valid_to, s.valid_to)", nb)
        self.assertIn("if not issuer_cik or not ticker", nb)
        self.assertNotIn("issuer_cik in _security_by_cik", nb)
        self.assertIn("if not issuer_cik or not issuer_ticker", nb)
        self.assertIn("_security_by_cik_ticker.get((issuer_cik, issuer_ticker))", nb)
        self.assertIn("Historical source security_sk collision", nb)
        self.assertIn("Duplicate historical dim_security targets", nb)
        self.assertIn("s.company_name < t.company_name", nb)


    def test_form4_resolution_never_substitutes_a_different_ticker(self):
        module = ast.parse(_read("nb_01_form4_to_silver"))
        resolve_node = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == "_resolve_security"
        )
        namespace = {"_security_by_cik_ticker": {("123", "NEW"): 42}}
        exec(compile(ast.Module(body=[resolve_node], type_ignores=[]), "<resolve_security>", "exec"), namespace)
        resolve_security = namespace["_resolve_security"]

        self.assertEqual(resolve_security("123", "new"), (42, "NEW"))
        self.assertEqual(resolve_security("123", "OLD"), (None, "OLD"))
        self.assertEqual(resolve_security("123", None), (None, None))


    def test_form4_event_date_uses_first_parseable_official_candidate(self):
        module = ast.parse(_read("nb_01_form4_to_silver"))
        select_node = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == "_select_form4_event_date"
        )
        def parse_date(value):
            try:
                return date.fromisoformat(value)
            except (TypeError, ValueError):
                return None

        namespace = {"date": date, "_to_date": parse_date}
        exec(compile(ast.Module(body=[select_node], type_ignores=[]), "<event_date>", "exec"), namespace)
        select_event_date = namespace["_select_form4_event_date"]

        self.assertEqual(
            select_event_date("not-a-date", "2024-02-02", "2024-02-03"),
            date(2024, 2, 2),
        )
        self.assertEqual(
            select_event_date(None, None, "2024-02-03"),
            date(2024, 2, 3),
        )
        self.assertIsNone(select_event_date(None, "invalid", ""))


    def test_form4_parser_uses_xml_period_before_bronze_period(self):
        module = ast.parse(_read("nb_01_form4_to_silver"))
        functions = {
            node.name: node
            for node in module.body
            if isinstance(node, ast.FunctionDef)
        }

        def parse_date(value):
            try:
                return date.fromisoformat(value)
            except (TypeError, ValueError):
                return None

        namespace = {
            "date": date,
            "Decimal": Decimal,
            "ET": ET,
            "_to_date": parse_date,
            "_security_by_cik_ticker": {("123", "TEST"): 42},
        }
        for name in ("_txt", "_resolve_security", "_select_form4_event_date", "_parse_form4_xml"):
            exec(
                compile(ast.Module(body=[functions[name]], type_ignores=[]), "<form4_parser>", "exec"),
                namespace,
            )

        rows = namespace["_parse_form4_xml"](
            """
            <ownershipDocument>
              <periodOfReport>2024-02-02</periodOfReport>
              <issuer><issuerCik>0000000123</issuerCik><issuerName>Test Inc</issuerName><issuerTradingSymbol>TEST</issuerTradingSymbol></issuer>
              <reportingOwner><reportingOwnerId><rptOwnerCik>0000000456</rptOwnerCik><rptOwnerName>Owner</rptOwnerName></reportingOwnerId></reportingOwner>
              <nonDerivativeTable><nonDerivativeTransaction>
                <transactionDate><value>not-a-date</value></transactionDate>
                <transactionCodes><transactionCode>P</transactionCode></transactionCodes>
                <transactionAmounts><transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
              </nonDerivativeTransaction></nonDerivativeTable>
            </ownershipDocument>
            """,
            {
                "accession_no": "0000000123-24-000001",
                "file_date": "2024-02-05",
                "period_of_report": "2024-02-03",
                "source_id": "sec_form4",
            },
        )

        self.assertEqual(rows[0]["event_date"], date(2024, 2, 2))
        self.assertEqual(rows[0]["knowledge_date"], date(2024, 2, 5))
        self.assertEqual(rows[0]["security_sk"], 42)


    def test_form4_bronze_reader_tolerates_optional_nested_fields(self):
        nb = _read("nb_01_form4_to_silver")

        self.assertIn("spark.read.text(paths)", nb)
        self.assertNotIn("spark.read.json(paths)", nb)
        self.assertIn('F.get_json_object("raw_json", "$.record.filing_url")', nb)
        self.assertIn('F.get_json_object("raw_json", "$.record.ciks")', nb)
        self.assertIn("ArrayType(StringType())", nb)


    def test_form4_quarantine_triage_preserves_audit_rows_and_classifies_actions(self):
        nb = _read("nb_01a_form4_quarantine_triage")

        self.assertIn("CREATE OR REPLACE VIEW v_sec_form4_quarantine_triage", nb)
        self.assertNotIn("DELETE FROM silver_security_quarantine", nb)
        self.assertNotIn("display(", nb)
        self.assertIn('if gate_summary["gate_status"] == "FAILED":', nb)
        self.assertIn(".show(sample_limit, truncate=False)", nb)
        self.assertIn("OVER (PARTITION BY raw_identifier) = 1 AS is_terminal", nb)
        self.assertNotIn("terminal_accessions AS", nb)
        self.assertNotIn("superseding_outcomes AS", nb)
        for status in ["RESOLVED", "ACCEPTED", "RETRY", "REVIEW"]:
            self.assertIn(status, nb)
        self.assertIn("max_retry_rows", nb)
        self.assertIn("retry_accessions", nb)
        self.assertIn("duplicate_natural_keys", nb)
        self.assertIn("gate_summary_json", nb)
        for retry_counter in [
            "xml_fetch_failed_rows",
            "processing_failed_rows",
            "worker_failed_rows",
            "transient_xml_failure_rows",
            "archive_not_found_rows",
            "retry_min_knowledge_date",
            "retry_max_knowledge_date",
            "security_unresolved_review_rows",
            "security_unresolved_review_accessions",
            "pit_missing_review_rows",
            "pit_missing_review_accessions",
            "other_review_rows",
        ]:
            self.assertIn(retry_counter, nb)
        self.assertNotIn("retry_sample =", nb)
        self.assertNotIn('"retry_reasons": retry_reasons', nb)
        self.assertIn("flush=True", nb)
        self.assertIn("mssparkutils.notebook.exit(gate_summary_json)", nb)


    def test_form4_quarantine_only_parses_line_numbers_for_line_level_reasons(self):
        nb = _read("nb_01a_form4_quarantine_triage")

        self.assertIn(
            "q.reason IN ('SECURITY_UNRESOLVED', 'PIT_MISSING', 'INVALID_DATE')",
            nb,
        )
        self.assertIn("q.line_no IS NULL AND la.accession_no IS NOT NULL", nb)


    def test_form4_terminal_outcome_supersedes_historical_retry_evidence(self):
        nb = _read("nb_01a_form4_quarantine_triage")

        self.assertIn("OVER (PARTITION BY raw_identifier) = 1 AS is_terminal", nb)
        self.assertIn("OVER (PARTITION BY raw_identifier) = 1 AS has_superseding_outcome", nb)
        self.assertIn("'NO_OWNERSHIP_XML'", nb)
        self.assertIn("'ARCHIVE_NOT_FOUND'", nb)
        self.assertIn("WHEN is_terminal THEN 'ACCEPTED'", nb)
        self.assertIn("AND has_superseding_outcome THEN 'ACCEPTED'", nb)
        self.assertIn("'SECURITY_UNRESOLVED'", nb)
        self.assertIn("'PIT_MISSING'", nb)


    def test_prices_silver_resolves_security_sk_and_quarantines_unresolved_symbols(self):
        nb = _read("nb_02_prices_to_silver")

        self.assertIn("security_sk", nb)
        self.assertIn("SECURITY_UNRESOLVED", nb)
        self.assertIn("DeltaTable.forName(spark, \"silver_security_quarantine\")", nb)
        self.assertIn("resolved_prices", nb)
        self.assertIn("price_revision_hash", nb)
        self.assertIn("earliest_revision_window", nb)
        self.assertIn('F.row_number().over(earliest_revision_window)', nb)
        self.assertNotIn('.dropDuplicates(["symbol", "price_date"])', nb)
        self.assertIn("t.price_revision_hash = s.price_revision_hash", nb)
        self.assertIn('whenMatchedUpdateAll(condition="t.ingest_ts IS NULL OR s.ingest_ts < t.ingest_ts")', nb)
        self.assertIn("revision_loaded_at", nb)
        self.assertIn("silver_missing_loaded_at", nb)
        self.assertIn("_ensure_not_null_constraints", nb)
        self.assertIn("ADD CONSTRAINT", nb)
        self.assertIn("ambiguous_current_tickers", nb)
        self.assertIn("PRICE SILVER RESOLUTION FAILED", nb)
        self.assertIn("silver_source_duplicates", nb)
        self.assertIn("PRICE SILVER SOURCE VALIDATION FAILED", nb)
        self.assertIn("PRICE SILVER REVISION VALIDATION FAILED", nb)
        self.assertIn("PRICE SILVER SUMMARY", nb)
        self.assertIn('spark.read.format("binaryFile")', nb)
        self.assertIn('.load("Files/bronze/prices_eod")', nb)
        self.assertNotIn("mssparkutils.fs.ls", nb)
        self.assertIn('"revised_natural_keys": revised_natural_keys', nb)
        self.assertIn('"revised_key_details": revised_key_details', nb)
        self.assertIn("mssparkutils.notebook.exit(run_summary_json)", nb)
        self.assertIn("silver_missing_ingest_ts", nb)


if __name__ == "__main__":
    unittest.main()