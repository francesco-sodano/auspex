import sys
import types
import unittest
import os
from pathlib import Path
from datetime import date, timedelta
from unittest.mock import patch

CONNECTORS_ROOT = Path(__file__).resolve().parents[1] / "connectors"
sys.path.insert(0, str(CONNECTORS_ROOT))

azure_mod = types.ModuleType("azure")
azure_identity_mod = types.ModuleType("azure.identity")
azure_cosmos_mod = types.ModuleType("azure.cosmos")
azure_cosmos_exceptions_mod = types.ModuleType("azure.cosmos.exceptions")
azure_storage_mod = types.ModuleType("azure.storage")
azure_storage_filedatalake_mod = types.ModuleType("azure.storage.filedatalake")
httpx_mod = types.ModuleType("httpx")


class DefaultAzureCredential:
    pass


class CosmosClient:
    pass


class CosmosResourceNotFoundError(Exception):
    pass


class DataLakeServiceClient:
    pass


class HttpxTimeoutException(Exception):
    pass


class HttpxNetworkError(Exception):
    pass


class HttpxResponse:
    pass


azure_identity_mod.DefaultAzureCredential = DefaultAzureCredential
azure_cosmos_mod.CosmosClient = CosmosClient
azure_cosmos_exceptions_mod.CosmosResourceNotFoundError = CosmosResourceNotFoundError
azure_storage_filedatalake_mod.DataLakeServiceClient = DataLakeServiceClient
httpx_mod.TimeoutException = HttpxTimeoutException
httpx_mod.NetworkError = HttpxNetworkError
httpx_mod.Response = HttpxResponse

sys.modules.setdefault("azure", azure_mod)
sys.modules.setdefault("azure.identity", azure_identity_mod)
sys.modules.setdefault("azure.cosmos", azure_cosmos_mod)
sys.modules.setdefault("azure.cosmos.exceptions", azure_cosmos_exceptions_mod)
sys.modules.setdefault("azure.storage", azure_storage_mod)
sys.modules.setdefault("azure.storage.filedatalake", azure_storage_filedatalake_mod)
sys.modules.setdefault("httpx", httpx_mod)

from shared.base_connector import BaseConnector
from shared.bronze_writer import BronzeWriter
from shared.envelope import deterministic_batch_id
from shared.models import Batch, RunContext, Watermark
from alpha_vantage.connector import AlphaVantageConnector
from contracts.connector import ContractsConnector
from etf_holdings.connector import EtfHoldingsConnector
from news.connector import NewsConnector
from prices_eod.connector import PricesEodConnector
from sec_13f.connector import Sec13FConnector


class FakeControlPlane:
    def __init__(self):
        self.watermark = None
        self.dedup = set()
        self.started = []
        self.ended = []
        self.fail_advance = False

    def start_run(self, run_id, source_id):
        self.started.append((run_id, source_id))

    def end_run(self, run_id, source_id, result):
        self.ended.append((run_id, source_id, result.status))

    def read_watermark(self, source_id):
        return self.watermark

    def advance_watermark(self, source_id, run_id, last_event_ts=None, last_cursor=None):
        if self.fail_advance:
            raise RuntimeError("advance failed")
        self.watermark = Watermark(
            source_id=source_id,
            last_event_ts=last_event_ts,
            last_cursor=last_cursor,
            updated_at="now",
        )

    def check_dedup(self, key, source_id):
        return f"{source_id}:{key}" in self.dedup

    def mark_dedup(self, key, source_id):
        self.dedup.add(f"{source_id}:{key}")


class FakeBronzeWriter:
    def __init__(self, fail=False):
        self.fail = fail
        self.writes = []

    def write(self, source_id, batch_id, envelopes, partition_date):
        if self.fail:
            raise RuntimeError("write failed")
        self.writes.append((source_id, batch_id, envelopes, partition_date))
        return 123


class FakeUniverseBronzeWriter(FakeBronzeWriter):
    def __init__(self, symbols):
        super().__init__()
        self.symbols = symbols

    def read_universe(self):
        return self.symbols


class FakeHttpResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class DummyConnector(BaseConnector):
    source_id = "dummy"
    schema_version = 7

    def __init__(self, cp, bw, batch=None, fetch_error=None):
        super().__init__(cp, bw)
        self.batch = batch
        self.fetch_error = fetch_error

    def fetch(self, since):
        if self.fetch_error:
            raise self.fetch_error
        return self.batch


def sample_batch():
    return Batch(
        records=[{"natural_id": "A"}],
        new_wm=Watermark(source_id="dummy", last_event_ts="2026-06-20", last_cursor="cursor-1"),
        window="2026-06-19-to-2026-06-20",
        partition_date="2026-06-20",
    )


class BaseConnectorTests(unittest.TestCase):
    def test_deterministic_batch_id_is_safe_for_cosmos_and_onelake(self):
        batch_id = deterministic_batch_id("sec_8k", "2026-06-01-to-2026-06-28-forms-8-K,8-K/A")

        self.assertEqual(batch_id, "sec_8k-2026-06-01-to-2026-06-28-forms-8-K-8-K-A")
        self.assertNotIn("/", batch_id)

    def test_success_writes_bronze_marks_dedup_and_advances_watermark(self):
        cp = FakeControlPlane()
        bw = FakeBronzeWriter()
        batch = sample_batch()

        result = DummyConnector(cp, bw, batch=batch).run(RunContext(run_id="run-1", source_id="dummy"))

        batch_id = deterministic_batch_id("dummy", batch.window)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.records_in, 1)
        self.assertEqual(result.bytes_written, 123)
        self.assertEqual(cp.watermark.last_event_ts, "2026-06-20")
        self.assertIn(f"dummy:{batch_id}", cp.dedup)
        self.assertEqual(bw.writes[0][1], batch_id)
        self.assertEqual(bw.writes[0][3], "2026-06-20")
        self.assertEqual(cp.ended[-1], ("run-1", "dummy", "ok"))

    def test_bronze_write_failure_does_not_advance_watermark_or_mark_dedup(self):
        cp = FakeControlPlane()
        bw = FakeBronzeWriter(fail=True)
        batch = sample_batch()

        result = DummyConnector(cp, bw, batch=batch).run(RunContext(run_id="run-1", source_id="dummy"))

        self.assertEqual(result.status, "failed")
        self.assertIn("write failed", result.error)
        self.assertIsNone(cp.watermark)
        self.assertEqual(cp.dedup, set())
        self.assertEqual(cp.ended[-1], ("run-1", "dummy", "failed"))

    def test_dedup_replay_advances_watermark_without_rewriting_bronze(self):
        cp = FakeControlPlane()
        bw = FakeBronzeWriter()
        batch = sample_batch()
        batch_id = deterministic_batch_id("dummy", batch.window)
        cp.dedup.add(f"dummy:{batch_id}")

        result = DummyConnector(cp, bw, batch=batch).run(RunContext(run_id="run-2", source_id="dummy"))

        self.assertEqual(result.status, "skipped")
        self.assertEqual(bw.writes, [])
        self.assertEqual(cp.watermark.last_cursor, "cursor-1")
        self.assertEqual(cp.ended[-1], ("run-2", "dummy", "skipped"))

    def test_fetch_failure_is_logged_and_does_not_write(self):
        cp = FakeControlPlane()
        bw = FakeBronzeWriter()

        result = DummyConnector(cp, bw, fetch_error=RuntimeError("fetch failed")).run(
            RunContext(run_id="run-3", source_id="dummy")
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("fetch failed", result.error)
        self.assertEqual(bw.writes, [])
        self.assertIsNone(cp.watermark)
        self.assertEqual(cp.ended[-1], ("run-3", "dummy", "failed"))


class BronzeWriterTests(unittest.TestCase):
    def test_bronze_path_uses_partition_date_not_current_date(self):
        writer = object.__new__(BronzeWriter)
        writer._lakehouse_root = "lakehouse-id"

        path = writer._bronze_path("sec_form4", "batch-1", "2026-06-20")

        self.assertEqual(path, "lakehouse-id/Files/bronze/sec_form4/2026/06/20/batch-1.ndjson")


class PricesEodConnectorTests(unittest.TestCase):
    def test_chunked_universe_uses_symbol_aware_window(self):
        os.environ["ALPHAVANTAGE_API_KEY"] = "test-key"
        os.environ["AV_RPM"] = "100000"
        today = date.today().isoformat()

        def fake_http_get(url, params=None, **kwargs):
            return FakeHttpResponse({
                "Time Series (Daily)": {
                    today: {
                        "1. open": "10.0",
                        "2. high": "11.0",
                        "3. low": "9.0",
                        "4. close": "10.5",
                        "5. volume": "1000",
                    }
                }
            })

        with patch("prices_eod.connector.http_get", side_effect=fake_http_get):
            first = PricesEodConnector(
                FakeControlPlane(),
                FakeUniverseBronzeWriter(["AAPL", "MSFT", "NVDA"]),
                since_date=today,
                symbol_offset=0,
                symbol_limit=2,
            ).fetch(None)
            second = PricesEodConnector(
                FakeControlPlane(),
                FakeUniverseBronzeWriter(["AAPL", "MSFT", "NVDA"]),
                since_date=today,
                symbol_offset=2,
                symbol_limit=2,
            ).fetch(None)

        self.assertNotEqual(first.window, second.window)
        self.assertIn("offset-0-limit-2", first.window)
        self.assertIn("offset-2-limit-2", second.window)
        self.assertEqual({record["symbol"] for record in first.records}, {"AAPL", "MSFT"})
        self.assertEqual({record["symbol"] for record in second.records}, {"NVDA"})


class E8ConnectorTests(unittest.TestCase):
    def test_alpha_vantage_fetches_symbol_macro_fx_and_etf_records(self):
        os.environ["ALPHAVANTAGE_API_KEY"] = "test-key"
        os.environ["AV_RPM"] = "100000"

        def fake_http_get(url, params=None, **kwargs):
            function_name = params["function"]
            payloads = {
                "OVERVIEW": {"Symbol": params.get("symbol"), "PERatio": "20"},
                "BALANCE_SHEET": {"quarterlyReports": []},
                "CASH_FLOW": {"quarterlyReports": []},
                "NEWS_SENTIMENT": {"feed": []},
                "INSTITUTIONAL_HOLDINGS": {"data": []},
                "ETF_PROFILE": {"holdings": []},
                "TREASURY_YIELD": {"data": [{"date": date.today().isoformat(), "value": "4.25"}]},
                "CURRENCY_EXCHANGE_RATE": {"Realtime Currency Exchange Rate": {
                    "1. From_Currency Code": "USD",
                    "3. To_Currency Code": "CHF",
                    "5. Exchange Rate": "0.81",
                }},
            }
            return FakeHttpResponse(payloads[function_name])

        with patch("alpha_vantage.connector.http_get", side_effect=fake_http_get):
            batch = AlphaVantageConnector(
                FakeControlPlane(),
                FakeUniverseBronzeWriter([]),
                symbols=["AAPL"],
                etf_symbols=["QQQ"],
                since_date=date.today().isoformat(),
            ).fetch(None)

        self.assertEqual(len(batch.records), 8)
        self.assertEqual({record["function"] for record in batch.records}, {
            "OVERVIEW", "BALANCE_SHEET", "CASH_FLOW", "NEWS_SENTIMENT", "INSTITUTIONAL_HOLDINGS",
            "ETF_PROFILE", "TREASURY_YIELD", "CURRENCY_EXCHANGE_RATE",
        })
        self.assertIn("symbols-1-of-1", batch.window)

    def test_sec_13f_uses_efts_forms_and_user_agent(self):
        os.environ["EDGAR_USER_AGENT"] = "Auspex test@example.com"

        def fake_http_get(url, params=None, headers=None, **kwargs):
            self.assertEqual(params["forms"], "13F-HR,13F-HR/A")
            self.assertEqual(headers["User-Agent"], "Auspex test@example.com")
            return FakeHttpResponse({"hits": {"hits": [{"_source": {"adsh": "0001", "file_date": date.today().isoformat()}}]}})

        with patch("shared.sec_efts_connector.http_get", side_effect=fake_http_get):
            batch = Sec13FConnector(
                FakeControlPlane(),
                FakeBronzeWriter(),
                source_config={"rate_limit": {"requests_per_minute": 100000}},
                since_date=date.today().isoformat(),
            ).fetch(None)

        self.assertEqual(batch.records[0]["matched_forms"], "13F-HR,13F-HR/A")

    def test_contracts_connector_uses_search_terms(self):
        captured_payloads = []

        def fake_http_post(url, json=None, **kwargs):
            captured_payloads.append(json)
            return FakeHttpResponse({
                "results": [{"Award ID": "A1", "Recipient Name": "MICROSOFT", "Award Amount": 1000}],
                "page_metadata": {"hasNext": False},
            })

        with patch("contracts.connector.http_post", side_effect=fake_http_post):
            batch = ContractsConnector(
                FakeControlPlane(),
                FakeBronzeWriter(),
                search_terms=[{"symbol": "MSFT", "text": "MICROSOFT"}],
                since_date=date.today().isoformat(),
            ).fetch(None)

        self.assertEqual(batch.records[0]["symbol"], "MSFT")
        self.assertEqual(captured_payloads[0]["filters"]["keywords"], ["MICROSOFT"])

    def test_news_connector_fetches_company_news_for_universe(self):
        os.environ["FINNHUB_API_KEY"] = "test-key"

        def fake_http_get(url, params=None, **kwargs):
            self.assertEqual(params["symbol"], "AAPL")
            return FakeHttpResponse([{"id": 1, "headline": "Apple headline", "datetime": 1782518400}])

        with patch("news.connector.http_get", side_effect=fake_http_get):
            batch = NewsConnector(
                FakeControlPlane(),
                FakeUniverseBronzeWriter(["AAPL"]),
                since_date=date.today().isoformat(),
                source_config={"rate_limit": {"requests_per_minute": 100000}},
            ).fetch(None)

        self.assertEqual(batch.records[0]["symbol"], "AAPL")

    def test_news_connector_clips_since_date_to_free_tier_lookback(self):
        os.environ["FINNHUB_API_KEY"] = "test-key"
        os.environ.pop("FINNHUB_MAX_LOOKBACK_DAYS", None)
        captured_params = []
        expected_from = (date.today() - timedelta(days=365)).isoformat()

        def fake_http_get(url, params=None, **kwargs):
            captured_params.append(params)
            return FakeHttpResponse([])

        with patch("news.connector.http_get", side_effect=fake_http_get):
            batch = NewsConnector(
                FakeControlPlane(),
                FakeUniverseBronzeWriter(["AAPL"]),
                since_date=(date.today() - timedelta(days=400)).isoformat(),
                source_config={"rate_limit": {"requests_per_minute": 100000}},
            ).fetch(None)

        self.assertEqual(captured_params[0]["from"], expected_from)
        self.assertIn(f"{expected_from}-to-", batch.window)

    def test_etf_holdings_connector_fetches_alpha_vantage_profile(self):
        os.environ["ALPHAVANTAGE_API_KEY"] = "test-key"
        os.environ["AV_RPM"] = "100000"

        def fake_http_get(url, params=None, **kwargs):
            self.assertEqual(params["function"], "ETF_PROFILE")
            self.assertEqual(params["symbol"], "QQQ")
            return FakeHttpResponse({"holdings": [{"symbol": "MSFT", "weight": "8.5"}]})

        with patch("etf_holdings.connector.http_get", side_effect=fake_http_get):
            batch = EtfHoldingsConnector(FakeControlPlane(), FakeBronzeWriter(), etf_symbols=["QQQ"]).fetch(None)

        self.assertEqual(batch.records[0]["function"], "ETF_PROFILE")
        self.assertEqual(batch.records[0]["context"]["symbol"], "QQQ")


if __name__ == "__main__":
    unittest.main()