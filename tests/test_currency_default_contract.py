from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CurrencyDefaultContractTests(unittest.TestCase):
    def test_usd_is_the_configured_default_currency(self):
        models = (ROOT / "api" / "auspex_api" / "models.py").read_text(encoding="utf-8")
        web = (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
        portfolio_notebook = (
            ROOT / "fabric" / "nb_08_portfolio_derive.Notebook" / "notebook-content.py"
        ).read_text(encoding="utf-8")

        self.assertIn('base_currency="USD"', models)
        self.assertIn("useState(user.base_currency || 'USD')", web)
        self.assertIn('.withColumn("base_currency", F.lit("USD"))', portfolio_notebook)
        self.assertNotIn('base_currency="CHF"', models)

    def test_fx_conversion_structure_remains_available(self):
        connector = (ROOT / "connectors" / "alpha_vantage" / "connector.py").read_text(encoding="utf-8")
        gold_notebook = (
            ROOT / "fabric" / "nb_05_alpha_vantage_to_gold.Notebook" / "notebook-content.py"
        ).read_text(encoding="utf-8")

        self.assertIn("CURRENCY_EXCHANGE_RATE", connector)
        self.assertIn("USDCHF", connector)
        self.assertIn("fact_fx_rate", gold_notebook)


if __name__ == "__main__":
    unittest.main()