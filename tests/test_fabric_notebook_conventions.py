import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOKS = ROOT / "fabric" / "notebooks"


def _read(name: str) -> str:
    return (NOTEBOOKS / name).read_text(encoding="utf-8")


def _notebook_code(name: str) -> str:
    notebook = json.loads(_read(name))
    return "\n".join(
        "\n".join(cell.get("source", []))
        for cell in notebook["cells"]
        if cell.get("cell_type") == "code"
    )


class FabricNotebookConventionTests(unittest.TestCase):
    def test_parameter_cells_execute_without_prior_imports(self):
        for name in [
            "nb_00_bronze_health.py",
            "nb_00_entity_resolution.py",
            "nb_01_form4_to_silver.py",
            "nb_02_prices_to_silver.py",
            "nb_05_alpha_vantage_to_gold.py",
            "nb_06_sec_filings_to_gold.py",
            "nb_07_contracts_to_gold.py",
        ]:
            with self.subTest(notebook=name):
                code = _read(name)
                start = code.index("# --- Parameters: mark this cell as the Fabric parameter cell ---")
                end = code.index("# COMMAND ----------", start)
                exec(code[start:end], {})

        triage = json.loads(_read("nb_01a_form4_quarantine_triage.ipynb"))
        parameter_cell = next(
            cell for cell in triage["cells"]
            if cell.get("cell_type") == "code"
            and "mark this cell as the Fabric parameter cell" in "\n".join(cell.get("source", []))
        )
        exec("\n".join(parameter_cell["source"]), {})

    def test_date_window_notebooks_use_native_parameter_and_normalization_cells(self):
        for name in [
            "nb_00_bronze_health.py",
            "nb_01_form4_to_silver.py",
            "nb_02_prices_to_silver.py",
            "nb_05_alpha_vantage_to_gold.py",
            "nb_06_sec_filings_to_gold.py",
            "nb_07_contracts_to_gold.py",
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
        setup = _read("nb_00_entity_resolution.py")
        self.assertIn("# --- Parameters: mark this cell as the Fabric parameter cell ---", setup)
        self.assertIn("# --- Normalize and validate injected parameter values ---", setup)
        self.assertNotIn("mssparkutils.widgets", setup)

        triage = _notebook_code("nb_01a_form4_quarantine_triage.ipynb")
        self.assertIn("# Parameters: mark this cell as the Fabric parameter cell", triage)
        self.assertIn("# Normalize and validate injected parameter values", triage)
        self.assertNotIn("mssparkutils.widgets", triage)

    def test_full_history_notebooks_are_explicitly_parameterless(self):
        for name in ["nb_03_silver_to_gold.py", "nb_04_metrics.py"]:
            with self.subTest(notebook=name):
                code = _read(name)
                self.assertNotIn("from_date =", code)
                self.assertNotIn("mssparkutils.widgets", code)
                self.assertIn("_require_table", code)

    def test_merge_helpers_do_not_recount_source_dataframes(self):
        for name in [
            "nb_03_silver_to_gold.py",
            "nb_04_metrics.py",
            "nb_05_alpha_vantage_to_gold.py",
            "nb_06_sec_filings_to_gold.py",
            "nb_07_contracts_to_gold.py",
        ]:
            with self.subTest(notebook=name):
                code = _read(name)
                self.assertIn("operationMetrics", code)
                self.assertNotIn("Merged {source_df.count()}", code)

    def test_prices_use_replay_stable_knowledge_dates(self):
        prices = _read("nb_02_prices_to_silver.py")
        self.assertGreaterEqual(prices.count('F.to_date("ingest_ts").alias("knowledge_date")'), 2)
        self.assertNotIn('F.current_date().alias("knowledge_date")', prices)


if __name__ == "__main__":
    unittest.main()