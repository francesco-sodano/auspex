from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOKS = ROOT / "fabric" / "notebooks"


def _read(name: str) -> str:
    return (NOTEBOOKS / name).read_text(encoding="utf-8")


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

        self.assertIn("security_sk", nb)
        self.assertIn("SECURITY_UNRESOLVED", nb)
        self.assertIn("FORM4_PROCESSING_FAILED", nb)
        self.assertIn("FORM4_WORKER_FAILED", nb)
        self.assertIn("TXN_PARSE_FAILED", nb)
        self.assertIn("terminal_quarantine_set", nb)
        self.assertIn("NO_NONDERIVATIVE_TXNS", nb)
        self.assertIn("DeltaTable.forName(spark, \"silver_security_quarantine\")", nb)
        self.assertIn("natural_key", nb)


    def test_prices_silver_resolves_security_sk_and_quarantines_unresolved_symbols(self):
        nb = _read("nb_02_prices_to_silver.py")

        self.assertIn("security_sk", nb)
        self.assertIn("SECURITY_UNRESOLVED", nb)
        self.assertIn("DeltaTable.forName(spark, \"silver_security_quarantine\")", nb)
        self.assertIn("resolved_prices", nb)


if __name__ == "__main__":
    unittest.main()