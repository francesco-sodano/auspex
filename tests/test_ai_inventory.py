from pathlib import Path
import unittest

from scripts.generate_ai_inventory import update


ROOT = Path(__file__).resolve().parents[1]


class AiInventoryTests(unittest.TestCase):
    def test_root_scripts_are_an_importable_package(self):
        self.assertTrue((ROOT / "scripts" / "__init__.py").exists())

    def test_deterministic_inventory_matches_source_constants(self):
        document = (ROOT / "doc" / "compliance-mvp.md").read_text(encoding="utf-8")
        self.assertEqual(update(document), document)


if __name__ == "__main__":
    unittest.main()
