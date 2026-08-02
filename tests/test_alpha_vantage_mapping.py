import sys
import unittest
from decimal import Decimal
from pathlib import Path

CONNECTORS_ROOT = Path(__file__).resolve().parents[1] / "connectors"
sys.path.insert(0, str(CONNECTORS_ROOT))

from alpha_vantage.mapping import (  # noqa: E402
    map_balance_sheet,
    map_cash_flow,
    map_currency_exchange_rate,
    map_etf_profile,
    map_news_sentiment,
    map_overview,
    map_treasury_yield,
    to_decimal,
)


class AlphaVantageMappingTests(unittest.TestCase):
    def test_to_decimal_handles_nulls_percent_and_commas(self):
        self.assertEqual(to_decimal("1,234.50"), Decimal("1234.50"))
        self.assertEqual(to_decimal("12.5%"), Decimal("12.5"))
        self.assertIsNone(to_decimal("None"))
        self.assertIsNone(to_decimal("not-a-number"))

    def test_maps_overview_and_statement_payloads(self):
        fetched_at = "2026-06-27T12:00:00+00:00"
        overview = map_overview("ibm", {
            "Currency": "USD",
            "Sector": "Technology",
            "MarketCapitalization": "123456789",
            "PERatio": "22.5",
            "QuarterlyRevenueGrowthYOY": "0.12",
        }, fetched_at)
        balance = map_balance_sheet("ibm", {"quarterlyReports": [{
            "fiscalDateEnding": "2026-03-31",
            "cashAndCashEquivalentsAtCarryingValue": "100",
            "shortTermDebt": "20",
            "longTermDebt": "30",
        }]}, fetched_at)
        cash = map_cash_flow("ibm", {"quarterlyReports": [{
            "fiscalDateEnding": "2026-03-31",
            "operatingCashflow": "200",
            "capitalExpenditures": "-50",
        }]}, fetched_at)

        self.assertEqual(overview["symbol"], "IBM")
        self.assertEqual(overview["pe_ratio"], Decimal("22.5"))
        self.assertEqual(overview["rev_growth_yoy"], Decimal("0.12"))
        self.assertEqual(balance["total_debt"], Decimal("50"))
        self.assertEqual(cash["capital_expenditures"], Decimal("-50"))

    def test_maps_news_macro_fx_and_etf(self):
        fetched_at = "2026-06-27T12:00:00+00:00"
        news = map_news_sentiment("AAPL", {"feed": [{
            "title": "Apple news",
            "url": "https://example.test/a",
            "time_published": "20260627T101500",
            "source": "Example",
            "ticker_sentiment": [{"ticker": "AAPL", "ticker_sentiment_score": "0.25", "relevance_score": "0.9"}],
        }]}, fetched_at)
        macro = map_treasury_yield({"data": [{"date": "2026-06-26", "value": "4.25"}]}, fetched_at)
        fx = map_currency_exchange_rate({"Realtime Currency Exchange Rate": {
            "1. From_Currency Code": "USD",
            "3. To_Currency Code": "CHF",
            "5. Exchange Rate": "0.81",
            "6. Last Refreshed": "2026-06-27 10:00:00",
        }}, fetched_at)
        holdings = map_etf_profile("QQQ", {"holdings": [{"symbol": "MSFT", "weight": "8.5"}]}, fetched_at)

        self.assertEqual(news[0]["event_date"], "2026-06-27")
        self.assertEqual(news[0]["sentiment"], Decimal("0.25"))
        self.assertEqual(macro[0]["indicator_code"], "US_TREASURY_3MONTH")
        self.assertEqual(fx["ccy_pair"], "USDCHF")
        self.assertEqual(fx["rate"], Decimal("0.81"))
        self.assertEqual(holdings[0]["theme_id"], "etf:QQQ")
        self.assertEqual(holdings[0]["blend_weight"], Decimal("1"))
        self.assertTrue(holdings[0]["is_ground_truth"])

        composite = map_etf_profile(
            "DTCR",
            {"holdings": [{"symbol": "EQIX", "weight": "12.5"}]},
            fetched_at,
            [{"theme_id": "data_center_buildout", "blend_weight": 0.5}],
        )
        self.assertEqual(composite[0]["theme_id"], "data_center_buildout")
        self.assertEqual(composite[0]["blend_weight"], Decimal("0.5"))


if __name__ == "__main__":
    unittest.main()
