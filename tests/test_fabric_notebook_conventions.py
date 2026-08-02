import unittest

from tests.fabric_notebook import notebook_cells, notebook_code



def _read(name: str) -> str:
    return notebook_code(name)


class FabricNotebookConventionTests(unittest.TestCase):
    def test_parameter_cells_execute_without_prior_imports(self):
        for name in [
            "nb_00_bronze_health",
            "nb_00_entity_resolution",
            "nb_01_form4_to_silver",
            "nb_02_prices_to_silver",
            "nb_05_alpha_vantage_to_gold",
            "nb_06_sec_filings_to_gold",
            "nb_07_contracts_to_gold",
            "nb_08_portfolio_derive",
            "nb_09_fundamental_anchor",
        ]:
            with self.subTest(notebook=name):
                parameter_cells = [code for marker, code in notebook_cells(name) if marker == "PARAMETERS CELL"]
                self.assertEqual(len(parameter_cells), 1)
                exec(parameter_cells[0], {})

        triage_parameters = [
            code for marker, code in notebook_cells("nb_01a_form4_quarantine_triage")
            if marker == "PARAMETERS CELL"
        ]
        self.assertEqual(len(triage_parameters), 1)
        exec(triage_parameters[0], {})

    def test_date_window_notebooks_use_native_parameter_and_normalization_cells(self):
        for name in [
            "nb_00_bronze_health",
            "nb_01_form4_to_silver",
            "nb_02_prices_to_silver",
            "nb_05_alpha_vantage_to_gold",
            "nb_06_sec_filings_to_gold",
            "nb_07_contracts_to_gold",
            "nb_08_portfolio_derive",
            "nb_09_fundamental_anchor",
        ]:
            with self.subTest(notebook=name):
                code = _read(name)
                parameter_cell = code.index("# --- Parameters: mark this cell as the Fabric parameter cell ---")
                normalization_cell = code.index("# --- Normalize and validate injected parameter values ---")
                self.assertLess(parameter_cell, normalization_cell)
                self.assertNotIn("mssparkutils.widgets", code)
                self.assertIn("from_date =", code[parameter_cell:normalization_cell])
                self.assertIn("to_date =", code[parameter_cell:normalization_cell])
                self.assertIn("from_date must be on or before to_date", code)

    def test_setup_and_triage_notebooks_use_the_same_parameter_lifecycle(self):
        setup = _read("nb_00_entity_resolution")
        self.assertIn("# --- Parameters: mark this cell as the Fabric parameter cell ---", setup)
        self.assertIn("# --- Normalize and validate injected parameter values ---", setup)
        self.assertNotIn("mssparkutils.widgets", setup)

        triage = _read("nb_01a_form4_quarantine_triage")
        self.assertIn("# Parameters: mark this cell as the Fabric parameter cell", triage)
        self.assertIn("# Normalize and validate injected parameter values", triage)
        self.assertNotIn("mssparkutils.widgets", triage)

    def test_source_history_uses_a_runtime_relative_window(self):
        source_history = _read("nb_13_source_history_to_silver")
        self.assertIn('start_date = ""', source_history)
        self.assertIn('end_date = ""', source_history)
        self.assertIn("date.fromisoformat(end_date) - timedelta(days=7)", source_history)
        self.assertNotIn('end_date = "2026-', source_history)

    def test_full_history_notebooks_are_explicitly_parameterless(self):
        for name in ["nb_03_silver_to_gold", "nb_04_metrics"]:
            with self.subTest(notebook=name):
                code = _read(name)
                self.assertNotIn("from_date =", code)
                self.assertNotIn("mssparkutils.widgets", code)
                self.assertIn("_require_table", code)

    def test_merge_helpers_do_not_recount_source_dataframes(self):
        for name in [
            "nb_03_silver_to_gold",
            "nb_04_metrics",
            "nb_05_alpha_vantage_to_gold",
            "nb_06_sec_filings_to_gold",
            "nb_07_contracts_to_gold",
        ]:
            with self.subTest(notebook=name):
                code = _read(name)
                self.assertIn("operationMetrics", code)
                self.assertNotIn("Merged {source_df.count()}", code)

    def test_prices_use_replay_stable_knowledge_dates(self):
        prices = _read("nb_02_prices_to_silver")
        self.assertGreaterEqual(prices.count('F.to_date("ingest_ts").alias("knowledge_date")'), 2)
        self.assertNotIn('F.current_date().alias("knowledge_date")', prices)


if __name__ == "__main__":
    unittest.main()