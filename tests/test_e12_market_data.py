from decimal import Decimal
from pathlib import Path
import unittest

from api.auspex_api.market_data import (
    CosmosMarketDataRepository,
    CosmosSecurityCatalog,
    CosmosUniverseRepository,
)


ROOT = Path(__file__).resolve().parents[1]


class FakeContainer:
    def __init__(self, documents=None):
        self.documents = documents or {}
        self.reads = []
        self.upserts = []
        self.queries = []

    def read_item(self, item, partition_key):
        self.reads.append((item, partition_key))
        document = self.documents.get((item, partition_key))
        if document is None:
            error = RuntimeError("not found")
            error.status_code = 404
            raise error
        return document

    def upsert_item(self, document):
        self.upserts.append(document)
        return document

    def query_items(self, query, parameters=None, enable_cross_partition_query=False):
        self.queries.append((query, parameters, enable_cross_partition_query))
        pair = next((item["value"] for item in parameters or [] if item["name"] == "@pair"), None)
        as_of = next((item["value"] for item in parameters or [] if item["name"] == "@as_of"), None)
        matches = [
            document
            for document in self.documents.values()
            if document.get("pair") == pair and document.get("as_of", "") <= as_of
        ]
        return sorted(matches, key=lambda document: document["as_of"], reverse=True)[:1]


class E12MarketDataTests(unittest.TestCase):
    def test_security_catalog_resolves_ticker_and_isin_with_point_reads(self):
        security = {
            "security_sk": 101,
            "ticker": "MSFT",
            "isin": "US5949181045",
            "company_name": "Microsoft Corporation",
            "currency": "USD",
            "exchange": "NASDAQ",
        }
        container = FakeContainer({
            ("ticker:MSFT", "ticker:MSFT"): {"id": "ticker:MSFT", **security},
            ("isin:US5949181045", "isin:US5949181045"): {"id": "isin:US5949181045", **security},
            ("security:101", "security:101"): {"id": "security:101", **security},
        })
        catalog = CosmosSecurityCatalog(container)

        self.assertEqual(catalog.resolve("msft").security_sk, 101)
        self.assertEqual(catalog.resolve("US5949181045").ticker, "MSFT")
        self.assertEqual(catalog.get(101).company_name, "Microsoft Corporation")
        self.assertEqual(container.reads, [
            ("ticker:MSFT", "ticker:MSFT"),
            ("isin:US5949181045", "isin:US5949181045"),
            ("security:101", "security:101"),
        ])

    def test_unknown_security_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "security was not found"):
            CosmosSecurityCatalog(FakeContainer()).resolve("UNKNOWN")

    def test_security_catalog_searches_ticker_prefix_and_limits_results(self):
        class SearchContainer(FakeContainer):
            def query_items(self, query, parameters, enable_cross_partition_query):
                self.queries.append((query, parameters, enable_cross_partition_query))
                return [
                    {"id": "ticker:MU", "security_sk": 1, "ticker": "MU", "isin": None, "company_name": "Micron Technology", "currency": "USD", "exchange": "NASDAQ"},
                    {"id": "ticker:MUR", "security_sk": 2, "ticker": "MUR", "isin": None, "company_name": "Murphy Oil", "currency": "USD", "exchange": "NYSE"},
                ]

        container = SearchContainer()
        results = CosmosSecurityCatalog(container).search("mu")

        self.assertEqual([result.ticker for result in results], ["MU", "MUR"])
        self.assertIn("STARTSWITH", container.queries[0][0])
        self.assertTrue(container.queries[0][2])

    def test_market_data_uses_point_reads_and_supports_inverse_fx(self):
        container = FakeContainer({
            ("quote:security:101", "quote:security:101"): {
                "id": "quote:security:101", "ticker": "MSFT", "price": "420.00",
                "currency": "USD", "as_of": "2026-07-21",
            },
            ("quote:MSFT", "quote:MSFT"): {
                "id": "quote:MSFT", "ticker": "MSFT", "price": "420.00",
                "currency": "USD", "as_of": "2026-07-21",
            },
            ("fx:USDCHF", "fx:USDCHF"): {
                "id": "fx:USDCHF", "pair": "USDCHF", "rate": "0.80000000",
                "as_of": "2026-07-21",
            },
            ("history:security:101", "history:security:101"): {
                "id": "history:security:101", "ticker": "MSFT",
                "prices_json": '[{"date":"2026-07-21","price":"420.00"}]',
            },
            ("score:security:101", "score:security:101"): {
                "id": "score:security:101", "opportunity_score": "82.5",
                "theme_id": "enterprise_technology",
            },
        })
        repository = CosmosMarketDataRepository(container)

        self.assertEqual(repository.quote("msft", 101)["price"], "420.00")
        self.assertEqual(repository.price_history("msft", 101)["prices"][0]["price"], "420.00")
        self.assertEqual(repository.score(101)["opportunity_score"], "82.5")
        inverse = repository.fx_rate("CHF", "USD")

        self.assertEqual(Decimal(inverse["rate"]), Decimal("1.25"))
        self.assertEqual(container.reads[-2:], [
            ("fx:CHFUSD", "fx:CHFUSD"),
            ("fx:USDCHF", "fx:USDCHF"),
        ])

    def test_market_data_supports_usd_cross_fx(self):
        container = FakeContainer({
            ("fx:USDCHF", "fx:USDCHF"): {
                "id": "fx:USDCHF", "rate": "0.80000000", "as_of": "2026-07-21",
            },
            ("fx:USDEUR", "fx:USDEUR"): {
                "id": "fx:USDEUR", "rate": "0.90000000", "as_of": "2026-07-20",
            },
        })

        result = CosmosMarketDataRepository(container).fx_rate("CHF", "EUR")

        self.assertEqual(Decimal(result["rate"]), Decimal("1.125"))
        self.assertEqual(result["as_of"], "2026-07-20")

    def test_historical_fx_selects_latest_rate_known_on_or_before_event_date(self):
        container = FakeContainer({
            ("fx:USDCHF:2026-07-01", "fx:USDCHF:2026-07-01"): {
                "id": "fx:USDCHF:2026-07-01", "kind": "fx", "pair": "USDCHF",
                "rate": "0.79000000", "as_of": "2026-07-01",
            },
            ("fx:USDCHF:2026-07-21", "fx:USDCHF:2026-07-21"): {
                "id": "fx:USDCHF:2026-07-21", "kind": "fx", "pair": "USDCHF",
                "rate": "0.81000000", "as_of": "2026-07-21",
            },
        })

        result = CosmosMarketDataRepository(container).fx_rate(
            "USD", "CHF", as_of="2026-07-10"
        )

        self.assertEqual(result["rate"], "0.79000000")
        self.assertEqual(result["as_of"], "2026-07-01")
        self.assertTrue(container.queries[0][2])

    def test_historical_fx_fails_closed_without_prior_rate(self):
        container = FakeContainer({
            ("fx:USDCHF:2026-07-21", "fx:USDCHF:2026-07-21"): {
                "id": "fx:USDCHF:2026-07-21", "kind": "fx", "pair": "USDCHF",
                "rate": "0.81000000", "as_of": "2026-07-21",
            },
        })

        self.assertIsNone(
            CosmosMarketDataRepository(container).fx_rate(
                "USD", "CHF", as_of="2026-07-10"
            )
        )

    def test_universe_upsert_contains_no_owner_identity(self):
        container = FakeContainer()
        catalog = CosmosSecurityCatalog(FakeContainer({
            ("ticker:MSFT", "ticker:MSFT"): {
                "id": "ticker:MSFT", "security_sk": 101, "ticker": "MSFT",
                "isin": "US5949181045", "company_name": "Microsoft Corporation",
                "currency": "USD", "exchange": "NASDAQ",
            },
        }))

        CosmosUniverseRepository(container).onboard(catalog.resolve("MSFT"))

        document = container.upserts[0]
        self.assertEqual(document["id"], "MSFT")
        self.assertEqual(document["symbol"], "MSFT")
        self.assertNotIn("owner_user_sk", document)


if __name__ == "__main__":
    unittest.main()