from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "fabric" / "notebooks" / "nb_00_bronze_health.py"


class BronzeHealthNotebookContractTests(unittest.TestCase):
    def test_uses_native_fabric_parameter_cell_before_normalization(self):
        notebook = NOTEBOOK.read_text(encoding="utf-8")

        parameter_cell = notebook.index("# --- Parameters: mark this cell as the Fabric parameter cell ---")
        normalization_cell = notebook.index("# --- Normalize and validate injected parameter values ---")
        self.assertLess(parameter_cell, normalization_cell)
        self.assertNotIn("mssparkutils.widgets.get", notebook)
        for parameter in [
            "from_date",
            "to_date",
            "sources_csv",
            "required_sources_csv",
            "expected_schema_version",
            "max_future_minutes",
        ]:
            self.assertIn(f"{parameter} =", notebook[parameter_cell:normalization_cell])

        self.assertIn("source_ids =", notebook)
        self.assertIn("required_source_ids =", notebook)

    def test_distinguishes_exact_duplicates_from_conflicting_payloads(self):
        notebook = NOTEBOOK.read_text(encoding="utf-8")

        self.assertIn('F.sha2("record_json", 256)', notebook)
        self.assertIn('alias("exact_duplicate_rows")', notebook)
        self.assertIn('alias("conflicting_duplicate_keys")', notebook)
        structural_block = notebook[
            notebook.index("structural_error_total ="):notebook.index("health = (", notebook.index("structural_error_total ="))
        ]
        self.assertIn('"conflicting_duplicate_keys"', structural_block)
        self.assertNotIn('"duplicate_rows"', structural_block)

    def test_reports_window_boundary_gaps_separately(self):
        notebook = NOTEBOOK.read_text(encoding="utf-8")

        self.assertIn('"leading_gap_days"', notebook)
        self.assertIn('"trailing_gap_days"', notebook)
        self.assertIn("CHECK_TRAILING_GAP", notebook)


if __name__ == "__main__":
    unittest.main()