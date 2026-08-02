import json
from pathlib import Path
import unittest

from tests.fabric_notebook import notebook_cells

ROOT = Path(__file__).resolve().parents[1]
FABRIC = ROOT / "fabric"

NOTEBOOK_NAMES = {
    "nb_00_bronze_health",
    "nb_00_entity_resolution",
    "nb_01_form4_to_silver",
    "nb_01a_form4_quarantine_triage",
    "nb_02_prices_to_silver",
    "nb_03_silver_to_gold",
    "nb_04_metrics",
    "nb_05_alpha_vantage_to_gold",
    "nb_06_sec_filings_to_gold",
    "nb_07_contracts_to_gold",
    "nb_08_portfolio_derive",
    "nb_09_fundamental_anchor",
    "nb_10_evidence_and_iq",
    "nb_11_narrative_intensity",
    "nb_12_narrative_premium",
    "nb_13_source_history_to_silver",
}
PARAMETERLESS_NOTEBOOKS = {"nb_03_silver_to_gold"}
MAINTENANCE_NOTEBOOKS = {"nb_reset_three_year_baseline.ipynb"}


class FabricGitDefinitionTests(unittest.TestCase):
    def test_fabric_notebook_definitions_are_canonical_and_complete(self):
        item_names = {path.stem for path in FABRIC.glob("nb_*.Notebook")}
        self.assertEqual(item_names, NOTEBOOK_NAMES)
        maintenance_names = {
            path.name for path in (FABRIC / "notebooks").iterdir() if path.is_file()
        }
        self.assertEqual(maintenance_names, MAINTENANCE_NOTEBOOKS)

        for notebook_name in sorted(NOTEBOOK_NAMES):
            with self.subTest(notebook=notebook_name):
                item_folder = FABRIC / f"{notebook_name}.Notebook"
                definition = item_folder / "notebook-content.py"
                platform = item_folder / ".platform"

                self.assertTrue(platform.exists())
                self.assertTrue(definition.exists())
                cells = notebook_cells(notebook_name)
                self.assertTrue(cells)
                parameter_count = sum(marker == "PARAMETERS CELL" for marker, _ in cells)
                expected_count = 0 if notebook_name in PARAMETERLESS_NOTEBOOKS else 1
                self.assertEqual(parameter_count, expected_count)

    def test_reset_notebook_defaults_to_dry_run_and_has_required_sections(self):
        notebook_path = FABRIC / "notebooks" / "nb_reset_three_year_baseline.ipynb"
        notebook = json.loads(notebook_path.read_text(encoding="utf-8"))
        code = "\n".join(
            line
            for cell in notebook["cells"]
            if cell["cell_type"] == "code"
            for line in cell["source"]
        )
        markdown = "\n".join(
            line
            for cell in notebook["cells"]
            if cell["cell_type"] == "markdown"
            for line in cell["source"]
        )

        self.assertIn("dry_run = True", code)
        self.assertIn("execute_reset = False", code)
        self.assertIn("reset_enabled = execute_reset and not dry_run", code)
        self.assertIn("protected_paths = [bronze_source_path]", code)
        self.assertIn('audit_table = "ops_reset_audit"', code)

        required_sections = [
            "Configure the Three-Year Bronze Baseline",
            "Inventory Existing Lakehouse Data",
            "Validate Bronze Ingestion Completeness",
            "Preview Obsolete Data Removal",
            "Remove Legacy Derived Data and Checkpoints",
            "Rebuild Tables from the Bronze Baseline",
            "Run Post-Rebuild Data Quality Checks",
            "Persist the Reset Audit Report",
        ]
        for section in required_sections:
            with self.subTest(section=section):
                self.assertIn(section, markdown)


if __name__ == "__main__":
    unittest.main()