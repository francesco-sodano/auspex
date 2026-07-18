import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOKS = ROOT / "fabric" / "notebooks"


def _read(name: str) -> str:
    return (NOTEBOOKS / name).read_text(encoding="utf-8")


def _read_notebook_code(name: str) -> str:
    notebook = json.loads(_read(name))
    return "\n".join(
        "\n".join(cell.get("source", []))
        for cell in notebook["cells"]
        if cell.get("cell_type") == "code"
    )


class E4NotebookContractTests(unittest.TestCase):
    def test_entity_resolution_notebook_creates_canonical_dim_security(self):
        nb = _read("nb_00_entity_resolution.py")

        self.assertIn("CREATE TABLE IF NOT EXISTS dim_security", nb)
        self.assertIn("source_id = 'sec_company_tickers'", nb)
        for column in [
            "security_sk",
            "valid_from",
            "valid_to",
            "is_current",
            "resolution_method",
        ]:
            self.assertIn(column, nb)


    def test_quarantine_tables_have_replay_safe_natural_keys(self):
        nb = _read("nb_00_entity_resolution.py")

        self.assertIn("natural_key", nb)
        self.assertIn("MERGE", nb)
        self.assertIn("silver_security_quarantine", nb)
        self.assertIn("silver_dq_quarantine", nb)


    def test_form4_silver_resolves_security_sk_and_merges_quarantine(self):
        nb = _read("nb_01_form4_to_silver.py")

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
        self.assertIn("retry_quarantine_reasons", nb)
        self.assertIn("_ACTIVE_TERMINAL_REASONS", nb)
        self.assertIn('F.get_json_object("raw_json", "$.record.ciks")', nb)
        self.assertIn("_archive_cik_candidates", nb)
        self.assertIn('/index.json', nb)
        self.assertIn("NO_OWNERSHIP_XML", nb)
        self.assertIn("Removed {legacy_bad} legacy silver_insider_txn", nb)
        self.assertIn("DeltaTable.forName(spark, \"silver_security_quarantine\")", nb)
        self.assertIn("natural_key", nb)


    def test_form4_bronze_reader_tolerates_optional_nested_fields(self):
        nb = _read("nb_01_form4_to_silver.py")

        self.assertIn("spark.read.text(paths)", nb)
        self.assertNotIn("spark.read.json(paths)", nb)
        self.assertIn('F.get_json_object("raw_json", "$.record.filing_url")', nb)
        self.assertIn('F.get_json_object("raw_json", "$.record.ciks")', nb)
        self.assertIn("ArrayType(StringType())", nb)


    def test_form4_quarantine_triage_preserves_audit_rows_and_classifies_actions(self):
        nb = _read_notebook_code("nb_01a_form4_quarantine_triage.ipynb")

        self.assertIn("CREATE OR REPLACE VIEW v_sec_form4_quarantine_triage", nb)
        self.assertNotIn("DELETE FROM silver_security_quarantine", nb)
        for status in ["RESOLVED", "ACCEPTED", "RETRY", "REVIEW"]:
            self.assertIn(status, nb)
        self.assertIn("max_retry_rows", nb)
        self.assertIn("retry_accessions", nb)
        self.assertIn("duplicate_natural_keys", nb)


    def test_form4_quarantine_only_parses_line_numbers_for_line_level_reasons(self):
        nb = _read_notebook_code("nb_01a_form4_quarantine_triage.ipynb")

        self.assertIn(
            "q.reason IN ('SECURITY_UNRESOLVED', 'PIT_MISSING', 'INVALID_DATE')",
            nb,
        )
        self.assertIn("q.line_no IS NULL AND la.accession_no IS NOT NULL", nb)


    def test_form4_terminal_outcome_supersedes_historical_retry_evidence(self):
        nb = _read_notebook_code("nb_01a_form4_quarantine_triage.ipynb")

        self.assertIn("terminal_accessions AS", nb)
        self.assertIn("'NO_OWNERSHIP_XML'", nb)
        self.assertIn("WHEN is_terminal THEN 'ACCEPTED'", nb)


    def test_prices_silver_resolves_security_sk_and_quarantines_unresolved_symbols(self):
        nb = _read("nb_02_prices_to_silver.py")

        self.assertIn("security_sk", nb)
        self.assertIn("SECURITY_UNRESOLVED", nb)
        self.assertIn("DeltaTable.forName(spark, \"silver_security_quarantine\")", nb)
        self.assertIn("resolved_prices", nb)


if __name__ == "__main__":
    unittest.main()