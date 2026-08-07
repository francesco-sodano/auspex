from pathlib import Path
import unittest

from engine.company_package import LEG_WEIGHTS, MODEL_VERSION, WEIGHT_VERSION
from engine.fresh_opportunity import MIN_COHORT_SIZE, MIN_AVAILABLE_LEG_WEIGHT


ROOT = Path(__file__).resolve().parents[1]


class Arc42ContractTests(unittest.TestCase):
    def test_arc42_documents_new_company_engine_only(self):
        document = (ROOT / "doc" / "arc42-auspex.md").read_text(encoding="utf-8")

        self.assertIn(MODEL_VERSION, document)
        self.assertIn(WEIGHT_VERSION, document)
        self.assertIn(f"Minimum theme size is {MIN_COHORT_SIZE}", document)
        self.assertIn(f"at least {int(MIN_AVAILABLE_LEG_WEIGHT * 100)}%", document)
        for leg_name, weight in LEG_WEIGHTS.items():
            display_name = "crowding and positioning" if leg_name == "crowding_positioning" else leg_name.replace("_", " ")
            self.assertIn(display_name, document.lower())
            self.assertIn(f"{int(weight * 100)}%", document)
        for direction in ("ACCELERATING", "STABLE", "DETERIORATING", "UNCERTAIN"):
            self.assertIn(f"`{direction}`", document)
        self.assertNotIn("`opportunity_v1`", document)
        self.assertNotIn("`balanced_v1`", document)

    def test_arc42_documents_destructive_boundary_and_no_fabric_daily_path(self):
        document = (ROOT / "doc" / "arc42-auspex.md").read_text(encoding="utf-8")

        self.assertIn("Preserve only Cosmos `app_users` and `portfolio_transactions`", document)
        self.assertIn("DELETE-LEGACY-AUSPEX-ENGINE", document)
        self.assertIn("No Fabric capacity, notebook, Warehouse promotion, or Search synchronization", document)
        self.assertIn("GET /api/opportunities", document)
        self.assertIn("research-only disclosure", document)


if __name__ == "__main__":
    unittest.main()
