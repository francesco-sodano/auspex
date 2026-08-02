import json
import os
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from unittest.mock import patch
import unittest

from tests.test_connectors import FakeControlPlane, FakeUniverseBronzeWriter

from benchmark_prices.connector import BenchmarkPricesConnector
from sec_companyfacts.connector import SecCompanyFactsConnector
from sec_13dg.connector import Sec13DgConnector
from sec_nport.connector import SecNportConnector


ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, payload=None, text=""):
        self._payload = payload
        self.text = text
        self.extensions = {}

    def json(self):
        return self._payload


class HistoricalSourceConnectorTests(unittest.TestCase):
    def setUp(self):
        os.environ["ALPHAVANTAGE_API_KEY"] = "test"
        os.environ["AV_RPM"] = "100000"
        os.environ["EDGAR_USER_AGENT"] = "Auspex tests test@example.com"
        os.environ["SEC_EFTS_MAX_RPM"] = "100000"
        os.environ["SEC_NPORT_MAX_RPM"] = "100000"

    def test_benchmark_prices_preserves_adjustments_and_actions(self):
        response = FakeResponse({
            "Time Series (Daily)": {
                "2026-07-14": {
                    "1. open": "100.0",
                    "2. high": "102.0",
                    "3. low": "99.0",
                    "4. close": "101.0",
                    "5. adjusted close": "100.5",
                    "6. volume": "1234",
                    "7. dividend amount": "0.25",
                    "8. split coefficient": "1.0",
                }
            }
        })
        connector = BenchmarkPricesConnector(
            FakeControlPlane(), FakeUniverseBronzeWriter([]), symbols=["QQQ"],
            since_date="2026-07-14", to_date="2026-07-14",
        )

        with patch("benchmark_prices.connector.http_get", return_value=response):
            batch = connector.fetch(None)

        self.assertEqual(len(batch.records), 1)
        self.assertEqual(batch.records[0]["adjusted_close"], 100.5)
        self.assertEqual(batch.records[0]["dividend_amount"], 0.25)
        self.assertEqual(batch.records[0]["split_coefficient"], 1.0)
        self.assertIn("TIME_SERIES_DAILY_ADJUSTED", batch.window)

    def test_companyfacts_uses_coverage_universe_and_keeps_payload(self):
        writer = FakeUniverseBronzeWriter(["MSFT"])
        ticker_map = {"0": {"ticker": "MSFT", "cik_str": 789019}}
        payload = {"cik": 789019, "facts": {"us-gaap": {"Revenues": {}}}}
        connector = SecCompanyFactsConnector(
            FakeControlPlane(), writer, since_date="2023-07-03", to_date="2026-07-14",
        )

        with patch(
            "sec_companyfacts.connector.http_get",
            side_effect=[FakeResponse(ticker_map), FakeResponse(payload)],
        ):
            batch = connector.fetch(None)

        self.assertEqual(writer.requested_universes, [("alpha_vantage", "coverage")])
        self.assertEqual(batch.records[0]["context"], {"symbol": "MSFT", "cik": "0000789019"})
        self.assertIs(batch.records[0]["payload"], payload)
        self.assertEqual(batch.records[0]["status"], "ok")

    def test_nport_requires_fixed_series_and_parses_holdings_without_backdating(self):
        mapping = [{
            "symbol": "QQQ", "cik": "0001067839",
            "series_id": "S000101292", "class_id": "C000271435",
        }]
        connector = SecNportConnector(
            FakeControlPlane(), FakeUniverseBronzeWriter([]), etf_series=mapping,
            since_date="2025-01-01", to_date="2025-12-31",
        )
        xml = """<edgarSubmission>
          <headerData><filerInfo><seriesClassInfo>
            <seriesId>S000101292</seriesId><classId>C000271435</classId>
          </seriesClassInfo></filerInfo></headerData>
          <formData><genInfo><repPdDate>2025-09-30</repPdDate></genInfo>
            <invstOrSecs><invstOrSec><name>Example Inc.</name><title>Common Stock</title>
              <cusip>123456789</cusip><balance>10</balance><units>NS</units>
              <curCd>USD</curCd><valUSD>1000</valUSD><pctVal>1.5</pctVal>
            </invstOrSec></invstOrSecs>
          </formData>
        </edgarSubmission>"""
        filing = {
            "cik": "0001067839", "form": "NPORT-P",
            "report_date": "2025-09-30", "filing_date": "2025-11-19",
            "acceptance_datetime": "2025-11-19T12:30:00.000Z",
            "accession_no": "0001067839-25-000007",
            "primary_document": "xslFormNPORT-P_X01/primary_doc.xml",
            "submissions_file": "CIK0001067839.json",
        }

        record = connector._record(
            filing, connector._etf_series[0], ET.fromstring(xml),
            connector._primary_document_url(filing), xml,
        )

        self.assertEqual(record["event_date"], "2025-09-30")
        self.assertEqual(record["knowledge_date"], "2025-11-19T12:30:00.000Z")
        self.assertEqual(record["status"], "matched")
        self.assertEqual(record["holdings"][0]["cusip"], "123456789")
        self.assertNotEqual(record["event_date"], record["filing_date"])

    def test_nport_retains_unmatched_downloaded_primary_xml(self):
        mapping = [{
            "symbol": "QQQ", "cik": "0001067839",
            "series_id": "S000101292", "class_id": "C000271435",
        }]
        connector = SecNportConnector(
            FakeControlPlane(), FakeUniverseBronzeWriter([]), etf_series=mapping,
            since_date="2025-11-19", to_date="2025-11-19",
        )
        xml = """<edgarSubmission>
          <headerData><filerInfo><seriesClassInfo>
            <seriesId>S000999999</seriesId><classId>C000999999</classId>
          </seriesClassInfo></filerInfo></headerData>
          <formData><invstOrSecs><invstOrSec>
            <name>Unmatched Inc.</name><cusip>987654321</cusip><valUSD>250</valUSD>
          </invstOrSec></invstOrSecs></formData>
        </edgarSubmission>"""
        filing = {
            "cik": "0001067839", "form": "NPORT-P",
            "report_date": "2025-09-30", "filing_date": "2025-11-19",
            "acceptance_datetime": "2025-11-19T12:30:00.000Z",
            "accession_no": "0001067839-25-000008",
            "primary_document": "primary_doc.xml",
            "submissions_file": "CIK0001067839.json",
        }

        with patch.object(connector, "_filings_for_cik", return_value=[filing]), patch.object(
            connector, "_fetch_primary_xml", return_value=xml,
        ) as fetch_primary_xml:
            batch = connector.fetch(None)

        fetch_primary_xml.assert_called_once()
        self.assertEqual(len(batch.records), 1)
        self.assertEqual(batch.records[0]["status"], "unmatched_series")
        self.assertEqual(batch.records[0]["filed_series_ids"], ["S000999999"])
        self.assertEqual(batch.records[0]["holdings"][0]["cusip"], "987654321")
        self.assertEqual(batch.records[0]["primary_xml"], xml)

    def test_nport_accepts_missing_series_id_only_for_single_series_registrant(self):
        single = [{
            "symbol": "QQQ", "cik": "0001067839",
            "series_id": "S000101292", "class_id": "C000271435",
        }]
        connector = SecNportConnector(
            FakeControlPlane(), FakeUniverseBronzeWriter([]), etf_series=single,
            since_date="2025-01-01", to_date="2025-12-31",
        )
        xml = "<root><seriesName>QQQ</seriesName></root>"
        filing = {
            "cik": "0001067839", "form": "NPORT-P",
            "report_date": "2025-09-30", "filing_date": "2025-11-19",
            "acceptance_datetime": "2025-11-19T12:30:00.000Z",
            "accession_no": "0001067839-25-000009",
            "primary_document": "primary_doc.xml",
            "submissions_file": "CIK0001067839.json",
        }

        with patch.object(connector, "_filings_for_cik", return_value=[filing]), patch.object(
            connector, "_fetch_primary_xml", return_value=xml,
        ):
            batch = connector.fetch(None)

        self.assertEqual(len(batch.records), 1)
        self.assertEqual(batch.records[0]["status"], "matched")
        self.assertEqual(batch.records[0]["series_id"], "S000101292")
        self.assertEqual(batch.records[0]["filed_series_ids"], [])

    def test_registration_enables_historical_mvp_sources_and_fixed_contracts(self):
        seeds = json.loads(
            (ROOT / "connectors" / "shared" / "sources_seed.json").read_text(encoding="utf-8")
        )
        by_source = {row["source_id"]: row for row in seeds}
        for source_id in ("benchmark_prices", "sec_companyfacts", "sec_nport"):
            self.assertIn(source_id, by_source)
            self.assertTrue(by_source[source_id]["enabled"])
            self.assertEqual(by_source[source_id]["schema_version"], 1)
        self.assertEqual(
            {row["symbol"] for row in by_source["sec_nport"]["etf_series"]},
            {"SMH", "XLK", "XLE", "XLV", "DTCR", "PAVE", "GRID"},
        )

    def test_13dg_supports_renamed_forms_and_prefilters_entity_ciks(self):
        connector = Sec13DgConnector(
            FakeControlPlane(), FakeUniverseBronzeWriter([]),
            since_date="2025-07-07", to_date="2025-07-07",
            entity_ciks=["0000355019"], filing_limit=25,
        )
        rows = [
            {"adsh": "a", "form": "SCHEDULE 13D", "ciks": ["0000355019", "0001949366"]},
            {"adsh": "b", "form": "SCHEDULE 13G", "ciks": ["0000919893", "0001890906"]},
        ]
        connector.query_audits = [{"total_hits": 2, "fetched_hits": 2}]
        with patch.object(
            connector, "_fetch_window", side_effect=[rows[:1], [], rows[1:], []]
        ), patch.object(
            connector, "_enrich_filing", side_effect=lambda record, _headers: record,
        ):
            batch = connector.fetch(None)

        self.assertIn("SCHEDULE 13D", connector.forms)
        self.assertIn("SCHEDULE 13G/A", connector.forms)
        self.assertEqual(connector.window_days, 7)
        self.assertEqual([row["adsh"] for row in batch.records], ["a"])
        self.assertEqual(connector.query_total_filings, 2)
        self.assertEqual(connector.filtered_total_filings, 1)
        self.assertTrue(connector.require_exhaustive_efts)

    def test_13dg_sends_exact_query_ciks_to_efts(self):
        connector = Sec13DgConnector(
            FakeControlPlane(), FakeUniverseBronzeWriter([]),
            since_date="2025-07-07", to_date="2025-07-07",
            query_ciks=["355019", "0000919893"],
        )
        captured = []

        def fake_http_get(_url, params=None, **_kwargs):
            captured.append(params.get("ciks"))
            return FakeResponse({
                "hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}
            })

        with patch("shared.sec_efts_connector.http_get", side_effect=fake_http_get):
            connector.fetch(None)

        self.assertEqual(set(captured), {"0000355019,0000919893"})

if __name__ == "__main__":
    unittest.main()
