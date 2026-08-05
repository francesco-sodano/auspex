from pathlib import Path
import unittest

from engine.thesis import LEG_WEIGHTS, MIN_THEME_COHORT, MODEL_VERSION, WEIGHT_VERSION


ROOT = Path(__file__).resolve().parents[1]


class Arc42ContractTests(unittest.TestCase):
    def test_arc42_documents_current_opportunity_score_contract(self):
        document = (ROOT / "doc" / "arc42-auspex.md").read_text(encoding="utf-8")

        self.assertIn(f"`{MODEL_VERSION}`", document)
        self.assertIn(f"`{WEIGHT_VERSION}`", document)
        self.assertEqual(MIN_THEME_COHORT, 8)
        self.assertIn("minimum cohort is eight securities", document)
        documented_leg_names = {
            "crowding_positioning": "crowding and positioning",
        }
        for leg_name, weight in LEG_WEIGHTS.items():
            self.assertIn(
                documented_leg_names.get(leg_name, leg_name.replace("_", " ")),
                document.lower(),
            )
            self.assertIn(f"{int(weight * 100)}%", document)
        for status in ("READY", "PARTIAL", "WITHHELD"):
            self.assertIn(f"`{status}`", document)
        for source in ("TRS", "MANUAL", "LLM"):
            self.assertIn(f"`{source}`", document)
        self.assertIn("empirical percentile rank", document)
        self.assertIn("max-of-themes selection bias", document)
        self.assertIn("not a calibrated return model", document)

    def test_arc42_documents_recommendation_and_classification_boundaries(self):
        document = (ROOT / "doc" / "arc42-auspex.md").read_text(encoding="utf-8")

        for threshold in ("`>= 80`", "`>= 70`", "`>= 60`", "`< 45`"):
            self.assertIn(threshold, document)
        for profile in ("Conservative", "Balanced", "Growth", "Aggressive"):
            self.assertIn(profile, document)
        self.assertIn("provenance = llm", document)
        self.assertIn("capped at `0.85`", document)
        self.assertIn("SEC 10-K Item 1 or 20-F Item 4", document)
        self.assertIn("| COHR | `data_center_buildout` |", document)
        self.assertIn("| VRT | `data_center_buildout` |", document)
        self.assertIn("| RGTI | `quantum_computing` |", document)
        self.assertIn("classified security may remain visibly unscored", document)
        self.assertIn("Notebook Job Scheduler", document)
        self.assertIn("pipeline's last modified user", document)


if __name__ == "__main__":
    unittest.main()
