import unittest

from tests.fabric_notebook import notebook_code


def _read() -> str:
    return notebook_code("nb_00_bronze_health")


class BronzeHealthNotebookContractTests(unittest.TestCase):
    def test_uses_native_fabric_parameter_cell_before_normalization(self):
        notebook = _read()

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
        notebook = _read()

        self.assertIn('F.sha2("record_json", 256)', notebook)
        self.assertIn('alias("exact_duplicate_rows")', notebook)
        self.assertIn('alias("conflicting_duplicate_keys")', notebook)
        self.assertIn('.groupBy("folder_source", "natural_key")', notebook)
        self.assertIn('alias("cross_batch_exact_duplicate_keys")', notebook)
        self.assertIn('alias("cross_batch_conflicting_keys")', notebook)
        self.assertIn('.filter(F.col("batch_count") > 1)', notebook)
        self.assertIn("cross_batch_conflict_record_samples", notebook)
        self.assertIn('alias("record_sha256")', notebook)
        self.assertIn('"cross_batch_conflict_records": cross_batch_conflict_record_rows', notebook)
        self.assertIn('revision_capable_source_ids = {"prices_eod"}', notebook)
        self.assertIn('"unhandled_cross_batch_conflicting_keys"', notebook)
        structural_block = notebook[
            notebook.index("structural_error_total ="):notebook.index("health = (", notebook.index("structural_error_total ="))
        ]
        self.assertIn('"conflicting_duplicate_keys"', structural_block)
        self.assertIn('"unhandled_cross_batch_conflicting_keys"', structural_block)
        self.assertNotIn('"cross_batch_conflicting_keys"', structural_block)
        self.assertNotIn('"duplicate_rows"', structural_block)
        self.assertNotIn('"cross_batch_duplicate_keys"', structural_block)

    def test_reports_window_boundary_gaps_separately(self):
        notebook = _read()

        self.assertIn('"leading_gap_days"', notebook)
        self.assertIn('"trailing_gap_days"', notebook)
        self.assertIn("CHECK_TRAILING_GAP", notebook)

    def test_returns_structured_health_summary(self):
        notebook = _read()

        self.assertIn("health_summary_json", notebook)
        self.assertIn("failure_summary_json", notebook)
        self.assertIn("BRONZE HEALTH SUMMARY", notebook)
        self.assertIn('RuntimeError(f"BRONZE HEALTH FAILED: {failure_summary_json}")', notebook)
        self.assertIn("mssparkutils.notebook.exit(health_summary_json)", notebook)


if __name__ == "__main__":
    unittest.main()