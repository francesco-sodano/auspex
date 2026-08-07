from datetime import date
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "connectors"))

from company_engine.provider import _insider_rows, _news_rows, _price_rows
from company_engine.service import CompanyEngineService, _signal_from_packet


AS_OF = date(2026, 8, 7)


def packet(company, value):
    prices = [
        {
            "date": date.fromordinal(AS_OF.toordinal() - index).isoformat(),
            "close": 100.0 + value + index,
            "volume": 1000.0 + index * (1 + value),
        }
        for index in range(30)
    ]
    news = [
        {
            "id": f"news-{company['ticker']}-{index}",
            "date": date.fromordinal(AS_OF.toordinal() - index * 4).isoformat(),
            "headline": f"{company['ticker']} update {index}",
            "summary": "Current company update.",
            "source": "Test",
            "url": None,
        }
        for index in range(max(1, int(value + 3)))
    ]
    return {
        "company": company,
        "as_of": AS_OF.isoformat(),
        "prices": prices,
        "overview": {
            "Description": "artificial intelligence semiconductor processor data center",
            "ProfitMargin": str(0.10 + value / 100),
            "OperatingMarginTTM": str(0.12 + value / 100),
            "QuarterlyRevenueGrowthYOY": str(0.08 + value / 100),
            "ReturnOnEquityTTM": str(0.15 + value / 100),
            "PERatio": str(20 - value),
            "PEGRatio": "1.5",
            "PriceToSalesRatioTTM": "5",
            "EVToEBITDA": "15",
        },
        "insider_transactions": [
            {
                "date": AS_OF.isoformat(),
                "acquisition_or_disposal": "A" if value >= 0 else "D",
                "shares": abs(value) + 1,
                "share_price": 10,
            }
        ],
        "news": news,
        "errors": [],
    }


class FakeControlPlane:
    def __init__(self):
        self.packages = {}
        self.market = {}
        self.security = {}
        self.universe = {}
        self.narratives = {}

    def list_portfolio_transactions(self):
        return [{"security_code": "HIGH", "security_sk": 777}]

    def get_current_company_package(self, security_sk):
        return self.packages.get(security_sk)

    def publish_company_package(self, package):
        from engine.company_package import package_document
        document = package_document(package)
        self.packages[package.security_sk] = {
            **document,
            "id": "current",
            "document_type": "current",
        }
        return document["package_fingerprint"]

    def attach_company_narrative(self, security_sk, package_fingerprint, narrative):
        self.narratives[security_sk] = narrative
        self.packages[security_sk]["narrative"] = narrative

    def upsert_market_data(self, document):
        self.market[document["id"]] = document

    def upsert_security_catalog(self, document):
        self.security[document["id"]] = document

    def container(self, name):
        parent = self
        class Container:
            def upsert_item(self, document):
                parent.universe[document["id"]] = document
        return Container()


class FakeProvider:
    def __init__(self, companies):
        self.companies = companies

    def fetch_company(self, company, as_of):
        value = {"LOW": -1, "MID": 0, "HIGH": 1}[company["ticker"]]
        return packet(company, value)

    def fetch_fx(self, source, target):
        return {"pair": f"{source}{target}", "rate": "0.9", "as_of": AS_OF.isoformat()}


class CompanyEngineServiceTests(unittest.TestCase):
    def test_refresh_publishes_company_packages_and_portfolio_serving_state(self):
        companies = [
            {"security_sk": 1, "ticker": "LOW", "company_name": "Low", "theme_id": "theme", "keywords": ["semiconductor"]},
            {"security_sk": 2, "ticker": "MID", "company_name": "Mid", "theme_id": "theme", "keywords": ["semiconductor"]},
            {"security_sk": 3, "ticker": "HIGH", "company_name": "High", "theme_id": "theme", "keywords": ["semiconductor"]},
        ]
        with tempfile.TemporaryDirectory() as directory:
            universe_path = Path(directory) / "universe.json"
            universe_path.write_text(json.dumps({"version": "test", "companies": companies}))
            cp = FakeControlPlane()
            result = CompanyEngineService(cp, FakeProvider(companies), universe_path).refresh(AS_OF)

        self.assertEqual(result["companies"], 3)
        self.assertEqual(result["themes"], 1)
        self.assertIn(777, cp.packages)
        self.assertEqual(cp.packages[777]["ticker"], "HIGH")
        self.assertEqual(cp.packages[777]["outlook_direction"], "ACCELERATING")
        self.assertIn("narrative", cp.packages[777])
        self.assertIn("quote:security:777", cp.market)
        self.assertIn("history:security:777", cp.market)
        self.assertIn("ticker:HIGH", cp.security)
        self.assertTrue(cp.universe["HIGH"]["active"])

    def test_packet_builds_all_six_fresh_leg_values_and_lineage(self):
        company = {"security_sk": 1, "ticker": "HIGH", "company_name": "High", "theme_id": "theme", "keywords": ["semiconductor"]}
        signal = _signal_from_packet(packet(company, 1))

        self.assertEqual(len(signal.raw_leg_values), 6)
        self.assertTrue(all(value is not None for value in signal.raw_leg_values.values()))
        self.assertTrue(all(signal.leg_evidence[leg] for leg in signal.raw_leg_values))
        self.assertEqual(len(signal.source_cursors), 5)

    def test_provider_normalizers_keep_only_compact_windows(self):
        price_payload = {"Time Series (Daily)": {
            date.fromordinal(AS_OF.toordinal() - index).isoformat(): {
                "5. adjusted close": str(100 + index),
                "6. volume": str(1000 + index),
            }
            for index in range(40)
        }}
        insider_payload = {"data": [
            {"transaction_date": AS_OF.isoformat(), "acquisition_or_disposal": "A", "shares": "10", "share_price": "2"},
            {"transaction_date": "2020-01-01", "acquisition_or_disposal": "D", "shares": "10", "share_price": "2"},
        ]}
        news_payload = [
            {"id": 1, "datetime": int(__import__("datetime").datetime(2026, 8, 7, tzinfo=__import__("datetime").timezone.utc).timestamp()), "headline": "Fresh", "summary": "Fresh", "source": "Test", "url": "https://example.test"}
        ]

        self.assertEqual(len(_price_rows(price_payload, AS_OF)), 30)
        self.assertEqual(len(_insider_rows(insider_payload, AS_OF)), 1)
        self.assertEqual(len(_news_rows(news_payload, date(2026, 6, 9), AS_OF)), 1)


if __name__ == "__main__":
    unittest.main()
