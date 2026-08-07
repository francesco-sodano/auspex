import sys
import types
import unittest
import os
from pathlib import Path
from datetime import date, timedelta
from unittest.mock import patch

os.environ.setdefault("SEC_EFTS_MAX_RPM", "100000")

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


class HttpxTransportError(Exception):
    pass


class HttpxConnectTimeout(HttpxTransportError):
    def __init__(self, message, request=None):
        super().__init__(message)
        self.request = request


class HttpxHttpStatusError(Exception):
    def __init__(self, message, request=None, response=None):
        super().__init__(message)
        self.request = request
        self.response = response


class HttpxRequest:
    def __init__(self, method, url):
        self.method = method
        self.url = url


class HttpxResponse:
    def __init__(self, status_code=None, request=None):
        self.status_code = status_code
        self.request = request


azure_identity_mod.DefaultAzureCredential = DefaultAzureCredential
azure_cosmos_mod.CosmosClient = CosmosClient
azure_cosmos_exceptions_mod.CosmosResourceNotFoundError = CosmosResourceNotFoundError
azure_storage_filedatalake_mod.DataLakeServiceClient = DataLakeServiceClient
httpx_mod.TimeoutException = HttpxTimeoutException
httpx_mod.NetworkError = HttpxNetworkError
httpx_mod.TransportError = HttpxTransportError
httpx_mod.ConnectTimeout = HttpxConnectTimeout
httpx_mod.HTTPStatusError = HttpxHttpStatusError
httpx_mod.Request = HttpxRequest
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
from shared.control_plane import CosmosControlPlane
from shared.envelope import deterministic_batch_id
from shared.models import Batch, RunContext, Watermark
from alpha_vantage.connector import AlphaVantageConnector
from benchmark_prices.connector import BenchmarkPricesConnector
from contracts.connector import ContractsConnector
from etf_holdings.connector import EtfHoldingsConnector, _validate_theme_catalog
from news.connector import NewsConnector
from prices_eod.connector import PricesEodConnector
from sec_form4.connector import SecForm4Connector
from sec_13f.connector import Sec13FConnector
from sec_13dg.connector import Sec13DgConnector
from sec_8k.connector import Sec8KConnector
from sec_s1.connector import SecS1Connector
from sec_companyfacts.connector import SecCompanyFactsConnector


class FakeControlPlane:
    def __init__(self):
        self.watermark = None
        self.dedup = set()
        self.started = []
        self.ended = []
        self.fail_advance = False
        self.market_data = []

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

    def upsert_market_data(self, document):
        self.market_data.append(document)


class VersionedMarketControlPlane(FakeControlPlane):
    def __init__(self, existing=None):
        super().__init__()
        self.existing = existing or {}

    def upsert_market_data(self, document):
        current = self.existing.get(document["id"])
        if current and current.get("as_of", "") > document.get("as_of", ""):
            return current
        self.existing[document["id"]] = document
        self.market_data.append(document)
        return document


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
    def __init__(self, symbols, portfolio_symbols=None):
        super().__init__()
        self.symbols = symbols
        self.portfolio_symbols = portfolio_symbols or []
        self.requested_universes = []

    def read_universe(self, name="prices", tier=None):
        self.requested_universes.append((name, tier))
        return self.symbols

    def read_portfolio_universe(self):
        return self.portfolio_symbols


class FakeHttpResponse:
    def __init__(self, payload, text=""):
        self.payload = payload
        self.text = text

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


class ProjectingDummyConnector(DummyConnector):
    def __init__(self, cp, bw, batch=None, projection_error=None):
        super().__init__(cp, bw, batch=batch)
        self.projected = []
        self.projection_error = projection_error

    def after_bronze_write(self, batch):
        if self.projection_error:
            raise self.projection_error
        self.projected.extend(batch.records)


def sample_batch():
    return Batch(
        records=[{"natural_id": "A"}],
        new_wm=Watermark(source_id="dummy", last_event_ts="2026-06-20", last_cursor="cursor-1"),
        window="2026-06-19-to-2026-06-20",
        partition_date="2026-06-20",
        watermark_from="2026-06-19",
    )


class BaseConnectorTests(unittest.TestCase):
    def test_projection_runs_only_after_successful_bronze_write(self):
        cp = FakeControlPlane()
        connector = ProjectingDummyConnector(cp, FakeBronzeWriter(), batch=sample_batch())

        result = connector.run(RunContext(run_id="project-1", source_id="dummy"))

        self.assertEqual(result.status, "ok")
        self.assertEqual(connector.projected, [{"natural_id": "A"}])
        self.assertIsNotNone(cp.watermark)

    def test_projection_failure_does_not_advance_watermark_or_mark_dedup(self):
        cp = FakeControlPlane()
        connector = ProjectingDummyConnector(
            cp,
            FakeBronzeWriter(),
            batch=sample_batch(),
            projection_error=RuntimeError("projection failed"),
        )

        result = connector.run(RunContext(run_id="project-2", source_id="dummy"))

        self.assertEqual(result.status, "failed")
        self.assertIsNone(cp.watermark)
        self.assertEqual(cp.dedup, set())

    def test_deterministic_batch_id_is_safe_for_cosmos_and_onelake(self):
        batch_id = deterministic_batch_id("sec_8k", "2026-06-01-to-2026-06-28-forms-8-K,8-K/A")

        self.assertEqual(batch_id, "sec_8k-2026-06-01-to-2026-06-28-forms-8-K-8-K-A")
        self.assertNotIn("/", batch_id)

    def test_connector_batch_id_is_versioned_for_schema_migrations(self):
        self.assertEqual(
            deterministic_batch_id("portfolio", "snapshot-abc", 5),
            "portfolio-v5-snapshot-abc",
        )

    def test_success_writes_bronze_marks_dedup_and_advances_watermark(self):
        cp = FakeControlPlane()
        bw = FakeBronzeWriter()
        batch = sample_batch()

        result = DummyConnector(cp, bw, batch=batch).run(RunContext(run_id="run-1", source_id="dummy"))

        batch_id = deterministic_batch_id("dummy", batch.window, DummyConnector.schema_version)
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
        batch_id = deterministic_batch_id("dummy", batch.window, DummyConnector.schema_version)
        cp.dedup.add(f"dummy:{batch_id}")

        result = DummyConnector(cp, bw, batch=batch).run(RunContext(run_id="run-2", source_id="dummy"))

        self.assertEqual(result.status, "skipped")
        self.assertEqual(bw.writes, [])
        self.assertEqual(cp.watermark.last_cursor, "cursor-1")
        self.assertEqual(cp.ended[-1], ("run-2", "dummy", "skipped"))

    def test_backfill_preserves_live_watermark_and_uses_requested_lower_bound(self):
        cp = FakeControlPlane()
        cp.watermark = Watermark(source_id="dummy", last_event_ts="2026-07-18", last_cursor="2026-07-18")
        bw = FakeBronzeWriter()

        result = DummyConnector(cp, bw, batch=sample_batch()).run(
            RunContext(run_id="backfill-1", source_id="dummy", mode="backfill")
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(cp.watermark.last_cursor, "2026-07-18")
        self.assertEqual(bw.writes[0][2][0]["watermark_from"], "2026-06-19")

    def test_pagination_state_is_preserved_for_empty_batches(self):
        cp = FakeControlPlane()
        bw = FakeBronzeWriter()
        batch = Batch(
            records=[],
            new_wm=Watermark(source_id="dummy"),
            window="empty-page",
            has_more=True,
        )

        result = DummyConnector(cp, bw, batch=batch).run(RunContext(run_id="run-page", source_id="dummy"))

        self.assertEqual(result.status, "empty")
        self.assertTrue(result.has_more)

    def test_paged_batches_advance_watermark_only_after_the_final_page(self):
        cp = FakeControlPlane()
        bw = FakeBronzeWriter()
        first_page = sample_batch()
        first_page.has_more = True

        first_result = DummyConnector(cp, bw, batch=first_page).run(
            RunContext(run_id="run-page-1", source_id="dummy")
        )

        self.assertEqual(first_result.status, "ok")
        self.assertTrue(first_result.has_more)
        self.assertIsNone(cp.watermark)

        final_page = sample_batch()
        final_page.window = "2026-06-19-to-2026-06-20-page-2"
        final_page.has_more = False
        final_result = DummyConnector(cp, bw, batch=final_page).run(
            RunContext(run_id="run-page-2", source_id="dummy")
        )

        self.assertEqual(final_result.status, "ok")
        self.assertFalse(final_result.has_more)
        self.assertEqual(cp.watermark.last_event_ts, "2026-06-20")

    def test_provider_error_redacts_query_string_secrets(self):
        cp = FakeControlPlane()
        bw = FakeBronzeWriter()

        result = DummyConnector(
            cp,
            bw,
            fetch_error=RuntimeError("request failed: https://example.test?q=x&apikey=top-secret&token=other-secret"),
        ).run(RunContext(run_id="run-secret", source_id="dummy"))

        self.assertEqual(result.status, "failed")
        self.assertNotIn("top-secret", result.error)
        self.assertNotIn("other-secret", result.error)
        self.assertIn("apikey=[REDACTED]", result.error)

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
    def test_portfolio_universe_includes_effective_ledger_positions(self):
        class FakeContainer:
            def __init__(self, documents):
                self.documents = documents

            def query_items(self, **kwargs):
                return self.documents

        writer = object.__new__(BronzeWriter)
        writer._universe_container = FakeContainer([{"symbol": "CAMT"}])
        writer._portfolio_container = FakeContainer([
            {"transaction_id": "amd-1", "transaction_type": "OPENING_POSITION", "security_code": "AMD", "quantity": "40"},
            {"transaction_id": "amd-2", "transaction_type": "OPENING_POSITION", "security_code": "AMD", "quantity": "42", "corrects_transaction_id": "amd-1"},
            {"transaction_id": "rgti-1", "transaction_type": "BUY", "security_code": "RGTI", "quantity": "173"},
            {"transaction_id": "closed-1", "transaction_type": "BUY", "security_code": "CLOSED", "quantity": "5"},
            {"transaction_id": "closed-2", "transaction_type": "SELL", "security_code": "CLOSED", "quantity": "5"},
            {"transaction_id": "fee-1", "transaction_type": "FEE", "security_code": "RGTI", "linked_transaction_id": "rgti-1"},
        ])

        self.assertEqual(writer.read_portfolio_universe(), ["AMD", "CAMT", "RGTI"])

    def test_serving_projection_reader_combines_json_parts(self):
        class Download:
            def __init__(self, data):
                self.data = data

            def readall(self):
                return self.data

        class File:
            def __init__(self, data):
                self.data = data

            def download_file(self):
                return Download(self.data)

        class Entry:
            def __init__(self, name, is_directory=False):
                self.name = name
                self.is_directory = is_directory

        class FileSystem:
            def get_paths(self, path):
                return [Entry(f"{path}/part-1.json"), Entry(f"{path}/_SUCCESS")]

            def get_file_client(self, path):
                return File(b'{"id":"ticker:MSFT"}\n{"id":"security:101"}\n')

        writer = object.__new__(BronzeWriter)
        writer._lakehouse_root = "lakehouse-id"
        writer._fs = FileSystem()

        documents = writer.read_serving_projection("security_catalog")

        self.assertEqual([document["id"] for document in documents], ["ticker:MSFT", "security:101"])

    def test_bronze_path_uses_partition_date_not_current_date(self):
        writer = object.__new__(BronzeWriter)
        writer._lakehouse_root = "lakehouse-id"

        path = writer._bronze_path("sec_form4", "batch-1", "2026-06-20")

        self.assertEqual(path, "lakehouse-id/Files/bronze/sec_form4/2026/06/20/batch-1.ndjson")

    def test_bronze_write_retries_transient_onelake_errors(self):
        writer = object.__new__(BronzeWriter)
        writer._lakehouse_root = "lakehouse-id"
        attempts = []

        class FakeFileClient:
            def upload_data(self, data, overwrite=True):
                attempts.append((data, overwrite))
                if len(attempts) == 1:
                    raise RuntimeError("Fabric capacity is currently not available")

        class FakeFileSystem:
            def get_file_client(self, path):
                self.path = path
                return FakeFileClient()

        writer._fs = FakeFileSystem()
        with patch("shared.bronze_writer.time.sleep"):
            bytes_written = writer.write("sec_form4", "batch-1", [{"a": 1}], "2026-06-20")

        self.assertEqual(bytes_written, len('{"a": 1}\n'.encode("utf-8")))
        self.assertEqual(len(attempts), 2)

    def test_missing_universe_returns_empty_but_service_failures_propagate(self):
        writer = object.__new__(BronzeWriter)
        writer._lakehouse_root = "lakehouse-id"

        class DownloadFailure(Exception):
            def __init__(self, status_code, message=""):
                super().__init__(message or f"download failed: {status_code}")
                self.status_code = status_code

            def download_file(self):
                raise self

        class FakeFileSystem:
            def __init__(self, status_code):
                self.status_code = status_code

            def get_file_client(self, path):
                return DownloadFailure(self.status_code)

        writer._fs = FakeFileSystem(404)
        writer._fs.get_file_client = lambda path: DownloadFailure(
            404, "The specified path does not exist"
        )
        self.assertEqual(writer.read_universe(), [])

        writer._fs.get_file_client = lambda path: DownloadFailure(
            404, "Fabric capacity is currently not available"
        )
        with self.assertRaises(DownloadFailure):
            writer.read_universe()

        writer._fs = FakeFileSystem(503)
        with self.assertRaises(DownloadFailure):
            writer.read_universe()

    def test_tiered_universe_normalizes_symbols_and_requires_active_subset(self):
        writer = object.__new__(BronzeWriter)
        writer._lakehouse_root = "lakehouse-id"

        class FakeDownload:
            def __init__(self, payload):
                self.payload = payload

            def download_file(self):
                return self

            def readall(self):
                return self.payload

        class FakeFileSystem:
            def __init__(self, payload):
                self.payload = payload

            def get_file_client(self, path):
                self.path = path
                return FakeDownload(self.payload)

        writer._fs = FakeFileSystem(b'{"schema_version":1,"tiers":{"active":[" msft ","AAPL","MSFT"],"coverage":["AAPL","MSFT","NVDA"]}}')
        self.assertEqual(writer.read_universe("alpha_vantage", "active"), ["AAPL", "MSFT"])
        self.assertTrue(writer._fs.path.endswith("Files/config/alpha_vantage_universe.json"))

        writer._fs = FakeFileSystem(b'{"schema_version":1,"tiers":{"active":["MSFT"],"coverage":["AAPL"]}}')
        with self.assertRaisesRegex(ValueError, "active symbols must be included in coverage"):
            writer.read_universe("alpha_vantage", "coverage")

        writer._fs = FakeFileSystem(b'{"schema_version":1,"policy":{"active_max_symbols":1},"tiers":{"active":["AAPL","MSFT"],"coverage":["AAPL","MSFT"]}}')
        with self.assertRaisesRegex(ValueError, "active universe exceeds configured maximum"):
            writer.read_universe("alpha_vantage", "active")


class ControlPlaneTests(unittest.TestCase):
    def test_dedup_marker_never_expires(self):
        written = []

        class FakeContainer:
            def upsert_item(self, item):
                written.append(item)

        class FakeDatabase:
            def get_container_client(self, name):
                self.name = name
                return FakeContainer()

        control_plane = object.__new__(CosmosControlPlane)
        control_plane._db = FakeDatabase()
        control_plane.mark_dedup("batch-1", "sec_form4")

        self.assertEqual(written[0]["ttl"], -1)

    def test_older_market_projection_does_not_replace_newer_alias(self):
        class FakeContainer:
            def __init__(self):
                self.document = {
                    "id": "quote:MSFT",
                    "as_of": "2026-07-21",
                    "price": "420.000000",
                }

            def read_item(self, item, partition_key):
                return self.document

            def upsert_item(self, item):
                self.document = item
                return item

        container = FakeContainer()
        control_plane = object.__new__(CosmosControlPlane)
        control_plane._db = type("FakeDatabase", (), {
            "get_container_client": lambda self, name: container,
        })()

        result = control_plane.upsert_market_data({
            "id": "quote:MSFT",
            "as_of": "2026-07-01",
            "price": "400.000000",
        })

        self.assertEqual(result["price"], "420.000000")
        self.assertEqual(container.document["price"], "420.000000")

    def test_stale_fabric_projection_generations_are_deleted_by_id_partition(self):
        class FakeContainer:
            def __init__(self):
                self.deleted = []

            def query_items(self, query, parameters, enable_cross_partition_query):
                return [{"id": "ticker:OLD"}, {"id": "quote:OLD"}]

            def delete_item(self, item, partition_key):
                self.deleted.append((item, partition_key))

        container = FakeContainer()
        control_plane = object.__new__(CosmosControlPlane)
        control_plane._db = type("FakeDatabase", (), {
            "get_container_client": lambda self, name: container,
        })()

        deleted = control_plane.delete_stale_projection_generation(
            "security_catalog", "2026-07-22"
        )

        self.assertEqual(deleted, 2)
        self.assertEqual(container.deleted, [
            ("ticker:OLD", "ticker:OLD"),
            ("quote:OLD", "quote:OLD"),
        ])


class BackfillRunnerContractTests(unittest.TestCase):
    def test_runner_uses_explicit_pagination_and_current_snapshot_semantics(self):
        runner = (Path(__file__).resolve().parents[1] / "scripts" / "run_historical_backfill.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn('Get-ObjectValue $Result "has_more"', runner)
        self.assertNotIn("Get-ResultRecordsIn $Result) -eq 0", runner)
        self.assertIn("RequirePaginationState", runner)
        self.assertIn("$hasPaginationState", runner)
        self.assertIn('$snapshotDate = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd")', runner)
        self.assertNotIn("AlphaVantageStart", runner)
        self.assertIn('mode = "backfill"', runner)


class PricesEodConnectorTests(unittest.TestCase):
    def test_covered_price_window_stops_before_universe_pagination(self):
        os.environ["ALPHAVANTAGE_API_KEY"] = "test-key"
        writer = FakeUniverseBronzeWriter(["AAPL", "MSFT"])
        connector = PricesEodConnector(
            FakeControlPlane(), writer, symbol_limit=1
        )

        with patch(
            "prices_eod.connector.http_get",
            side_effect=AssertionError("covered window must not call provider"),
        ):
            batch = connector.fetch(Watermark(
                source_id="prices_eod",
                last_event_ts=date.today().isoformat(),
            ))

        self.assertEqual(batch.records, [])
        self.assertFalse(batch.has_more)
        self.assertEqual(writer.requested_universes, [])

    def test_partial_trading_session_fails_instead_of_advancing_watermark(self):
        os.environ["ALPHAVANTAGE_API_KEY"] = "test-key"
        os.environ["AV_RPM"] = "100000"

        def fake_http_get(url, params=None, **kwargs):
            series = {
                "2026-08-04": {
                    "1. open": "10",
                    "2. high": "11",
                    "3. low": "9",
                    "4. close": "10.5",
                    "5. volume": "100",
                }
            } if params["symbol"] == "AAPL" else {}
            return FakeHttpResponse({"Time Series (Daily)": series})

        with patch("prices_eod.connector.http_get", side_effect=fake_http_get):
            with self.assertRaisesRegex(RuntimeError, "landing is incomplete"):
                PricesEodConnector(
                    FakeControlPlane(),
                    FakeUniverseBronzeWriter(["AAPL", "MSFT"]),
                    since_date="2026-08-04",
                    to_date="2026-08-04",
                ).fetch(None)

    def test_provider_error_fails_instead_of_returning_empty_batch(self):
        os.environ["ALPHAVANTAGE_API_KEY"] = "test-key"
        os.environ["AV_RPM"] = "100000"

        with patch(
            "prices_eod.connector.http_get",
            return_value=FakeHttpResponse({"Information": "rate limit exceeded"}),
        ):
            with self.assertRaisesRegex(RuntimeError, "response failed for AAPL"):
                PricesEodConnector(
                    FakeControlPlane(),
                    FakeUniverseBronzeWriter(["AAPL"]),
                    since_date="2026-08-04",
                    to_date="2026-08-04",
                ).fetch(None)

    def test_watermark_stops_at_latest_landed_trading_session(self):
        os.environ["ALPHAVANTAGE_API_KEY"] = "test-key"
        os.environ["AV_RPM"] = "100000"

        with patch("prices_eod.connector.http_get", return_value=FakeHttpResponse({
            "Time Series (Daily)": {
                "2026-07-31": {"1. open": "10", "2. high": "11", "3. low": "9", "4. close": "10.5", "5. volume": "100"},
            }
        })):
            batch = PricesEodConnector(
                FakeControlPlane(),
                FakeUniverseBronzeWriter(["AMD"]),
                since_date="2026-07-31",
                to_date="2026-08-03",
            ).fetch(None)

        self.assertEqual(batch.new_wm.last_event_ts, "2026-07-31")
        self.assertEqual(batch.new_wm.last_cursor, "2026-07-31")

    def test_older_backfill_does_not_replace_newer_quote_projection(self):
        control_plane = VersionedMarketControlPlane({
            "quote:MSFT": {"id": "quote:MSFT", "as_of": "2026-07-21", "price": "420.000000"},
        })
        connector = PricesEodConnector(control_plane, FakeUniverseBronzeWriter(["MSFT"]))

        connector.after_bronze_write(Batch(
            records=[{"symbol": "MSFT", "date": "2026-07-01", "close": 400.0}],
            new_wm=Watermark(source_id="prices_eod"),
            window="older",
        ))

        self.assertEqual(control_plane.existing["quote:MSFT"]["price"], "420.000000")
        self.assertEqual(control_plane.market_data, [])
    def test_successful_run_projects_latest_landed_quote(self):
        os.environ["ALPHAVANTAGE_API_KEY"] = "test-key"
        os.environ["AV_RPM"] = "100000"
        control_plane = FakeControlPlane()
        writer = FakeUniverseBronzeWriter(["MSFT"])
        connector = PricesEodConnector(
            control_plane,
            writer,
            since_date="2026-07-20",
            to_date="2026-07-21",
        )

        def fake_http_get(url, params=None, **kwargs):
            return FakeHttpResponse({"Time Series (Daily)": {
                "2026-07-20": {"1. open": "400", "2. high": "405", "3. low": "399", "4. close": "402", "5. volume": "100"},
                "2026-07-21": {"1. open": "410", "2. high": "422", "3. low": "408", "4. close": "420", "5. volume": "200"},
            }})

        with patch("prices_eod.connector.http_get", side_effect=fake_http_get):
            result = connector.run(RunContext(run_id="quote-1", source_id="prices_eod"))

        self.assertEqual(result.status, "ok")
        self.assertEqual(control_plane.market_data, [{
            "id": "quote:MSFT",
            "ticker": "MSFT",
            "price": "420.000000",
            "currency": "USD",
            "as_of": "2026-07-21",
            "source_id": "prices_eod",
        }])

    def test_portfolio_symbols_are_merged_into_configured_universe(self):
        os.environ["ALPHAVANTAGE_API_KEY"] = "test-key"
        os.environ["AV_RPM"] = "100000"
        today = date.today().isoformat()
        requested_symbols = []

        def fake_http_get(url, params=None, **kwargs):
            requested_symbols.append(params["symbol"])
            return FakeHttpResponse({"Time Series (Daily)": {}})

        writer = FakeUniverseBronzeWriter(
            ["AAPL", "MSFT"],
            portfolio_symbols=["MSFT", "NVDA"],
        )
        with patch("prices_eod.connector.http_get", side_effect=fake_http_get):
            PricesEodConnector(
                FakeControlPlane(),
                writer,
                since_date=today,
            ).fetch(None)

        self.assertEqual(requested_symbols, ["AAPL", "MSFT", "NVDA"])
        self.assertEqual(writer.requested_universes, [("alpha_vantage", "coverage")])

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
        self.assertTrue(first.has_more)
        self.assertFalse(second.has_more)
        self.assertEqual(first.watermark_from, today)

    def test_historical_backfill_can_request_full_alpha_vantage_output(self):
        os.environ["ALPHAVANTAGE_API_KEY"] = "test-key"
        os.environ["AV_RPM"] = "100000"
        captured_params = []
        today = date.today().isoformat()

        def fake_http_get(url, params=None, **kwargs):
            captured_params.append(params)
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
            batch = PricesEodConnector(
                FakeControlPlane(),
                FakeUniverseBronzeWriter(["AAPL"]),
                since_date=today,
                to_date=today,
                outputsize="full",
            ).fetch(None)

        self.assertEqual(captured_params[0]["outputsize"], "full")
        self.assertIn("outputsize-full", batch.window)


class BenchmarkPricesConnectorTests(unittest.TestCase):
    def test_watermark_stops_at_latest_landed_trading_session(self):
        os.environ["ALPHAVANTAGE_API_KEY"] = "test-key"
        os.environ["AV_RPM"] = "100000"
        payload = {"Time Series (Daily)": {
            "2026-07-31": {
                "1. open": "10", "2. high": "11", "3. low": "9", "4. close": "10.5",
                "5. adjusted close": "10.4", "6. volume": "100",
                "7. dividend amount": "0", "8. split coefficient": "1",
            }
        }}

        with patch("benchmark_prices.connector.http_get", return_value=FakeHttpResponse(payload)):
            batch = BenchmarkPricesConnector(
                FakeControlPlane(),
                FakeUniverseBronzeWriter([]),
                etf_symbols=["SMH"],
                since_date="2026-07-31",
                to_date="2026-08-03",
            ).fetch(None)

        self.assertEqual(batch.new_wm.last_event_ts, "2026-07-31")
        self.assertEqual(batch.new_wm.last_cursor, "2026-07-31")

    def test_historical_backfill_accepts_to_date_override(self):
        os.environ["ALPHAVANTAGE_API_KEY"] = "test-key"
        os.environ["AV_RPM"] = "100000"

        def fake_http_get(url, params=None, **kwargs):
            return FakeHttpResponse({
                "Time Series (Daily)": {
                    "2020-01-03": {
                        "1. open": "10.0",
                        "2. high": "11.0",
                        "3. low": "9.0",
                        "4. close": "10.5",
                        "5. volume": "1000",
                    },
                    "2020-01-06": {
                        "1. open": "12.0",
                        "2. high": "13.0",
                        "3. low": "11.0",
                        "4. close": "12.5",
                        "5. volume": "2000",
                    },
                }
            })

        with patch("prices_eod.connector.http_get", side_effect=fake_http_get):
            batch = PricesEodConnector(
                FakeControlPlane(),
                FakeUniverseBronzeWriter(["AAPL"]),
                since_date="2020-01-01",
                to_date="2020-01-03",
                outputsize="full",
            ).fetch(None)

        self.assertEqual([record["date"] for record in batch.records], ["2020-01-03"])
        self.assertIn("2020-01-01-to-2020-01-03", batch.window)


class SecForm4ConnectorTests(unittest.TestCase):
    def test_historical_backfill_accepts_since_date_override(self):
        os.environ["EDGAR_USER_AGENT"] = "Auspex test@example.com"
        captured_params = []

        def fake_http_get(url, params=None, headers=None, **kwargs):
            captured_params.append(params)
            return FakeHttpResponse({"hits": {"hits": []}})

        with patch("shared.sec_efts_connector.http_get", side_effect=fake_http_get), patch.object(
            Sec13FConnector, "_enrich_filing",
            side_effect=lambda record, headers: {**record, "sec_archive": {"archive_status": "complete"}},
        ):
            batch = SecForm4Connector(
                FakeControlPlane(),
                FakeBronzeWriter(),
                since_date="2020-01-01",
                to_date="2020-12-31",
                source_config={"rate_limit": {"requests_per_minute": 100000}},
            ).fetch(None)

        self.assertEqual(captured_params[0]["startdt"], "2020-01-01")
        self.assertEqual(captured_params[0]["enddt"], "2020-01-01")
        self.assertEqual(captured_params[-1]["startdt"], "2020-12-31")
        self.assertEqual(captured_params[-1]["enddt"], "2020-12-31")
        self.assertIn("2020-01-01-to-2020-12-31", batch.window)

    def test_historical_backfill_splits_form4_into_daily_efts_queries(self):
        os.environ["EDGAR_USER_AGENT"] = "Auspex test@example.com"
        captured_ranges = []

        def fake_http_get(url, params=None, headers=None, **kwargs):
            captured_ranges.append((params["startdt"], params["enddt"], params["from"]))
            return FakeHttpResponse({"hits": {"hits": []}})

        with patch("shared.sec_efts_connector.http_get", side_effect=fake_http_get), patch.object(
            Sec13FConnector, "_enrich_filing",
            side_effect=lambda record, headers: {**record, "sec_archive": {"archive_status": "complete"}},
        ):
            SecForm4Connector(
                FakeControlPlane(),
                FakeBronzeWriter(),
                since_date="2014-05-01",
                to_date="2014-05-03",
                source_config={"rate_limit": {"requests_per_minute": 100000}},
            ).fetch(None)

        self.assertEqual(captured_ranges, [
            ("2014-05-01", "2014-05-01", 0),
            ("2014-05-02", "2014-05-02", 0),
            ("2014-05-03", "2014-05-03", 0),
        ])

    def test_missing_hits_page_fails_instead_of_silently_advancing(self):
        os.environ["EDGAR_USER_AGENT"] = "Auspex test@example.com"

        def fake_http_get(url, params=None, headers=None, **kwargs):
            return FakeHttpResponse({})

        with patch("shared.sec_efts_connector.http_get", side_effect=fake_http_get):
            with self.assertRaisesRegex(RuntimeError, "EFTS and browse-edgar fallback failed"):
                SecForm4Connector(
                    FakeControlPlane(),
                    FakeBronzeWriter(),
                    since_date="2014-02-01",
                    to_date="2014-02-28",
                    source_config={"rate_limit": {"requests_per_minute": 100000}},
                ).fetch(None)

    def test_form4_amendments_are_accepted_without_mutating_efts_source(self):
        os.environ["EDGAR_USER_AGENT"] = "Auspex test@example.com"
        source = {"adsh": "0001", "file_date": "2025-01-03", "form": "4/A"}

        def fake_http_get(url, params=None, headers=None, **kwargs):
            return FakeHttpResponse({"hits": {"hits": [{"_source": source}]}})

        with patch("shared.sec_efts_connector.http_get", side_effect=fake_http_get):
            batch = SecForm4Connector(
                FakeControlPlane(),
                FakeBronzeWriter(),
                since_date="2025-01-03",
                to_date="2025-01-03",
                source_config={"rate_limit": {"requests_per_minute": 100000}},
            ).fetch(None)

        self.assertEqual(batch.records, [source])
        self.assertNotIn("matched_forms", source)

    def test_browse_edgar_fallback_rejects_unrelated_forms(self):
        os.environ["EDGAR_USER_AGENT"] = "Auspex test@example.com"
        atom = """<feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>485BPOS - Wrong Issuer (0000000001)</title>
<link href="https://www.sec.gov/wrong"/>
<summary>Filed: 2025-01-03 AccNo: 0000000001-25-000001</summary>
<category term="485BPOS"/></entry>
<entry><title>4 - Right Issuer (0000000002)</title>
<link href="https://www.sec.gov/right"/>
<summary>Filed: 2025-01-03 AccNo: 0000000002-25-000002</summary>
<category term="4"/></entry>
</feed>"""

        def fake_http_get(url, params=None, headers=None, **kwargs):
            if "browse-edgar" in url:
                return FakeHttpResponse({}, text=atom)
            raise RuntimeError("EFTS unavailable")

        with patch("shared.sec_efts_connector.http_get", side_effect=fake_http_get):
            batch = SecForm4Connector(
                FakeControlPlane(),
                FakeBronzeWriter(),
                since_date="2025-01-03",
                to_date="2025-01-03",
                source_config={"rate_limit": {"requests_per_minute": 100000}},
            ).fetch(None)

        self.assertEqual([record["form"] for record in batch.records], ["4"])
        self.assertIn("raw_atom", batch.records[0])

    def test_explicit_sec_error_payload_fails_loudly(self):
        os.environ["EDGAR_USER_AGENT"] = "Auspex test@example.com"

        def fake_http_get(url, params=None, headers=None, **kwargs):
            if "browse-edgar" in url:
                raise RuntimeError("fallback unavailable")
            return FakeHttpResponse({"error": "bad query"})

        with patch("shared.sec_efts_connector.http_get", side_effect=fake_http_get):
            with self.assertRaisesRegex(RuntimeError, "SEC EFTS and browse-edgar fallback failed"):
                SecForm4Connector(
                    FakeControlPlane(),
                    FakeBronzeWriter(),
                    since_date="2014-02-01",
                    to_date="2014-02-28",
                    source_config={"rate_limit": {"requests_per_minute": 100000}},
                ).fetch(None)

    def test_later_page_failure_falls_back_to_browse_edgar_and_dedupes(self):
        os.environ["EDGAR_USER_AGENT"] = "Auspex test@example.com"
        first_page_hits = [
            {"_source": {"adsh": f"0000000000-25-{i:06d}", "file_date": "2025-01-03", "form": "4"}}
            for i in range(100)
        ]
        atom = """<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry>
<title>4 - Existing Issuer (0000000000) ()</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/0/000000000025000000/0000000000-25-000000-index.htm"/>
<summary type="html">&lt;b&gt;Filed:&lt;/b&gt; 2025-01-03 &lt;b&gt;AccNo:&lt;/b&gt; 0000000000-25-000000 &lt;b&gt;Size:&lt;/b&gt; 1 KB</summary>
<category scheme="https://www.sec.gov/" label="form type" term="4"/>
</entry>
<entry>
<title>4 - New Issuer (0000000101) ()</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/101/000000010125000101/0000000101-25-000101-index.htm"/>
<summary type="html">&lt;b&gt;Filed:&lt;/b&gt; 2025-01-03 &lt;b&gt;AccNo:&lt;/b&gt; 0000000101-25-000101 &lt;b&gt;Size:&lt;/b&gt; 1 KB</summary>
<category scheme="https://www.sec.gov/" label="form type" term="4"/>
</entry>
</feed>"""

        def fake_http_get(url, params=None, headers=None, **kwargs):
            if "browse-edgar" in url:
                if params["type"] == "4":
                    return FakeHttpResponse({}, text=atom)
                self.assertEqual(params["type"], "4/A")
                return FakeHttpResponse({}, text='<feed xmlns="http://www.w3.org/2005/Atom"/>')
            if params["from"] == 0:
                return FakeHttpResponse({"hits": {"hits": first_page_hits}})
            raise RuntimeError("SEC EFTS 500")

        with patch("shared.sec_efts_connector.http_get", side_effect=fake_http_get):
            batch = SecForm4Connector(
                FakeControlPlane(),
                FakeBronzeWriter(),
                since_date="2025-01-03",
                to_date="2025-01-03",
                source_config={"rate_limit": {"requests_per_minute": 100000}},
            ).fetch(None)

        self.assertEqual(len(batch.records), 101)
        self.assertIn("0000000101-25-000101", {record["adsh"] for record in batch.records})


class E8ConnectorTests(unittest.TestCase):
    def test_covered_news_window_stops_before_universe_and_provider_calls(self):
        os.environ["FINNHUB_API_KEY"] = "test-key"
        writer = FakeUniverseBronzeWriter(["AAPL", "MSFT"])
        connector = NewsConnector(FakeControlPlane(), writer, symbol_limit=1)

        with patch(
            "news.connector.http_get",
            side_effect=AssertionError("covered window must not call Finnhub"),
        ):
            batch = connector.fetch(Watermark(
                source_id="news",
                last_event_ts=date.today().isoformat(),
            ))

        self.assertEqual(batch.records, [])
        self.assertFalse(batch.has_more)
        self.assertEqual(writer.requested_universes, [])

    def test_covered_benchmark_window_stops_before_provider_calls(self):
        os.environ["ALPHAVANTAGE_API_KEY"] = "test-key"
        connector = BenchmarkPricesConnector(
            FakeControlPlane(),
            FakeBronzeWriter(),
            etf_symbols=["VTI", "QTUM"],
            symbol_limit=1,
        )

        with patch(
            "benchmark_prices.connector.http_get",
            side_effect=AssertionError("covered window must not call provider"),
        ):
            batch = connector.fetch(Watermark(
                source_id="benchmark_prices",
                last_event_ts=date.today().isoformat(),
            ))

        self.assertEqual(batch.records, [])
        self.assertFalse(batch.has_more)

    def test_covered_companyfacts_window_stops_before_universe_pagination(self):
        os.environ["EDGAR_USER_AGENT"] = "Auspex/1.0 test@example.com"
        writer = FakeUniverseBronzeWriter(["AAPL", "MSFT"])
        connector = SecCompanyFactsConnector(
            FakeControlPlane(), writer, symbol_limit=1
        )

        with patch(
            "sec_companyfacts.connector.http_get",
            side_effect=AssertionError("covered window must not call SEC"),
        ):
            batch = connector.fetch(Watermark(
                source_id="sec_companyfacts",
                last_event_ts=date.today().isoformat(),
            ))

        self.assertEqual(batch.records, [])
        self.assertFalse(batch.has_more)
        self.assertEqual(writer.requested_universes, [])

    def test_companyfacts_404_is_row_level_absence_but_500_propagates(self):
        os.environ["EDGAR_USER_AGENT"] = "Auspex/1.0 test@example.com"
        connector = SecCompanyFactsConnector(
            FakeControlPlane(),
            FakeUniverseBronzeWriter(["MISSING"]),
            symbols=["MISSING"],
            since_date=date.today().isoformat(),
        )
        request = HttpxRequest("GET", "https://data.sec.gov/companyfacts")

        with patch.object(
            connector, "_fetch_ticker_to_cik", return_value={"MISSING": "0001821866"}
        ), patch(
            "sec_companyfacts.connector.http_get",
            side_effect=HttpxHttpStatusError(
                "not found", request=request, response=HttpxResponse(404, request)
            ),
        ):
            batch = connector.fetch(None)

        self.assertEqual(batch.records[0]["status"], "missing_companyfacts")
        self.assertEqual(batch.records[0]["context"]["cik"], "0001821866")
        self.assertIsNone(batch.records[0]["payload"])

        with patch.object(
            connector, "_fetch_ticker_to_cik", return_value={"MISSING": "0001821866"}
        ), patch(
            "sec_companyfacts.connector.http_get",
            side_effect=HttpxHttpStatusError(
                "server error", request=request, response=HttpxResponse(500, request)
            ),
        ), self.assertRaisesRegex(HttpxHttpStatusError, "server error"):
            connector.fetch(None)

    def test_alpha_vantage_profiles_select_cadence_functions_and_universe_tiers(self):
        os.environ["ALPHAVANTAGE_API_KEY"] = "test-key"
        os.environ["AV_RPM"] = "100000"
        expected = {
            "news_daily": ({"NEWS_SENTIMENT"}, ("alpha_vantage", "active")),
            "fundamentals_quarterly": ({"OVERVIEW", "BALANCE_SHEET", "CASH_FLOW"}, ("alpha_vantage", "coverage")),
            "holdings_quarterly": ({"INSTITUTIONAL_HOLDINGS"}, ("alpha_vantage", "coverage")),
            "macro_daily": ({"TREASURY_YIELD", "CURRENCY_EXCHANGE_RATE"}, None),
            "themes_weekly": ({"ETF_PROFILE"}, None),
        }

        def fake_http_get(url, params=None, **kwargs):
            return FakeHttpResponse({"function": params["function"]})

        for profile, (expected_functions, expected_universe) in expected.items():
            with self.subTest(profile=profile):
                writer = FakeUniverseBronzeWriter(["MSFT", "AAPL"])
                with patch("alpha_vantage.connector.http_get", side_effect=fake_http_get):
                    batch = AlphaVantageConnector(
                        FakeControlPlane(),
                        writer,
                        profile=profile,
                        etf_symbols=["QQQ"],
                        since_date=date.today().isoformat(),
                    ).fetch(None)

                self.assertEqual({record["function"] for record in batch.records}, expected_functions)
                self.assertTrue(all(record["profile"] == profile for record in batch.records))
                self.assertIn(f"profile-{profile}", batch.window)
                self.assertEqual(writer.requested_universes, [] if expected_universe is None else [expected_universe])

    def test_alpha_vantage_profile_uses_an_independent_watermark_scope(self):
        os.environ["ALPHAVANTAGE_API_KEY"] = "test-key"
        connector = AlphaVantageConnector(
            FakeControlPlane(),
            FakeUniverseBronzeWriter(["AAPL"]),
            profile="news_daily",
        )

        self.assertEqual(connector.watermark_source_id, "alpha_vantage:news_daily")

    def test_alpha_vantage_skips_window_already_covered_by_watermark(self):
        os.environ["ALPHAVANTAGE_API_KEY"] = "test-key"
        writer = FakeUniverseBronzeWriter(["AAPL"])
        connector = AlphaVantageConnector(
            FakeControlPlane(),
            writer,
            profile="news_daily",
        )

        with patch(
            "alpha_vantage.connector.http_get",
            side_effect=AssertionError("covered window must not call provider"),
        ):
            batch = connector.fetch(Watermark(
                source_id="alpha_vantage:news_daily",
                last_event_ts=date.today().isoformat(),
            ))

        self.assertEqual(batch.records, [])
        self.assertFalse(batch.has_more)
        self.assertEqual(batch.new_wm.last_event_ts, date.today().isoformat())
        self.assertEqual(writer.requested_universes, [])
        self.assertEqual(batch.watermark_from, (date.today() + timedelta(days=1)).isoformat())

    def test_alpha_vantage_profile_uses_registry_chunk_size_and_rejects_empty_universe(self):
        os.environ["ALPHAVANTAGE_API_KEY"] = "test-key"
        os.environ["AV_RPM"] = "100000"
        source_config = {"profiles": {"news_daily": {"symbol_limit": 1}}}

        with patch("alpha_vantage.connector.http_get", return_value=FakeHttpResponse({"feed": []})):
            batch = AlphaVantageConnector(
                FakeControlPlane(),
                FakeUniverseBronzeWriter(["MSFT", "AAPL"]),
                profile="news_daily",
                source_config=source_config,
            ).fetch(None)

        self.assertEqual(len(batch.records), 1)
        self.assertTrue(batch.has_more)

        with self.assertRaisesRegex(RuntimeError, "active universe is empty"):
            AlphaVantageConnector(
                FakeControlPlane(),
                FakeUniverseBronzeWriter([]),
                profile="news_daily",
                source_config=source_config,
            ).fetch(None)

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

        self.assertEqual(len(batch.records), 10)
        self.assertEqual({record["function"] for record in batch.records}, {
            "OVERVIEW", "BALANCE_SHEET", "CASH_FLOW", "NEWS_SENTIMENT", "INSTITUTIONAL_HOLDINGS",
            "ETF_PROFILE", "TREASURY_YIELD", "CURRENCY_EXCHANGE_RATE",
        })
        self.assertIn("symbols-1-of-1", batch.window)

    def test_alpha_vantage_can_skip_repeated_etf_macro_fx_records(self):
        os.environ["ALPHAVANTAGE_API_KEY"] = "test-key"
        os.environ["AV_RPM"] = "100000"

        def fake_http_get(url, params=None, **kwargs):
            return FakeHttpResponse({"function": params["function"]})

        with patch("alpha_vantage.connector.http_get", side_effect=fake_http_get):
            batch = AlphaVantageConnector(
                FakeControlPlane(),
                FakeUniverseBronzeWriter([]),
                symbols=["AAPL"],
                etf_symbols=["QQQ"],
                since_date=date.today().isoformat(),
                include_etfs=False,
                include_global=False,
            ).fetch(None)

        self.assertEqual({record["function"] for record in batch.records}, {
            "OVERVIEW", "BALANCE_SHEET", "CASH_FLOW", "NEWS_SENTIMENT", "INSTITUTIONAL_HOLDINGS",
        })
        self.assertIn("include-etfs-0", batch.window)
        self.assertIn("include-global-0", batch.window)

    def test_sec_13f_uses_efts_forms_and_user_agent(self):
        os.environ["EDGAR_USER_AGENT"] = "Auspex test@example.com"
        captured_forms = []

        def fake_http_get(url, params=None, headers=None, **kwargs):
            captured_forms.append(params["forms"])
            self.assertEqual(headers["User-Agent"], "Auspex test@example.com")
            return FakeHttpResponse({"hits": {"hits": [{"_source": {"adsh": "0001", "file_date": date.today().isoformat(), "form": "13F-HR"}}]}})

        with patch("shared.sec_efts_connector.http_get", side_effect=fake_http_get), patch.object(
            Sec13FConnector,
            "_enrich_filing",
            side_effect=lambda record, headers: {
                **record,
                "sec_archive": {"archive_status": "complete"},
            },
        ):
            batch = Sec13FConnector(
                FakeControlPlane(),
                FakeBronzeWriter(),
                source_config={"rate_limit": {"requests_per_minute": 100000}},
                since_date=date.today().isoformat(),
            ).fetch(None)

        self.assertEqual(captured_forms, ["13F-HR"])
        self.assertEqual(batch.records[0]["form"], "13F-HR")

    def test_sec_form_queries_are_split_and_deduped(self):
        os.environ["EDGAR_USER_AGENT"] = "Auspex test@example.com"
        captured_forms = []

        def fake_http_get(url, params=None, headers=None, **kwargs):
            captured_forms.append(params["forms"])
            return FakeHttpResponse({"hits": {"hits": [{"_source": {"adsh": "0001", "file_date": date.today().isoformat()}}]}})

        with patch("shared.sec_efts_connector.http_get", side_effect=fake_http_get), patch.object(
            Sec13FConnector,
            "_enrich_filing",
            side_effect=lambda record, headers: {
                **record,
                "sec_archive": {"archive_status": "complete"},
            },
        ):
            batch = Sec13FConnector(
                FakeControlPlane(),
                FakeBronzeWriter(),
                source_config={"rate_limit": {"requests_per_minute": 100000}},
                since_date=date.today().isoformat(),
            ).fetch(None)

        self.assertEqual(captured_forms, ["13F-HR"])
        self.assertEqual(len(batch.records), 1)

    def test_sec_s1_queries_are_root_aware_by_form(self):
        os.environ["EDGAR_USER_AGENT"] = "Auspex test@example.com"
        captured_forms = []

        def fake_http_get(url, params=None, headers=None, **kwargs):
            captured_forms.append(params["forms"])
            return FakeHttpResponse({"hits": {"hits": []}})

        with patch("shared.sec_efts_connector.http_get", side_effect=fake_http_get):
            SecS1Connector(
                FakeControlPlane(),
                FakeBronzeWriter(),
                source_config={"rate_limit": {"requests_per_minute": 100000}},
                since_date=date.today().isoformat(),
            ).fetch(None)

        self.assertEqual(captured_forms, ["S-1", "S-3", "S-3ASR", "424B4", "424B5"])

    def test_sec_multi_form_connectors_use_root_aware_queries(self):
        os.environ["EDGAR_USER_AGENT"] = "Auspex test@example.com"

        cases = [
            (Sec13FConnector, ["13F-HR"]),
            (Sec13DgConnector, ["SC 13D", "SC 13G", "SCHEDULE 13D", "SCHEDULE 13G"]),
            (Sec8KConnector, ["8-K"]),
            (SecS1Connector, ["S-1", "S-3", "S-3ASR", "424B4", "424B5"]),
        ]

        for connector_cls, expected_forms in cases:
            with self.subTest(source_id=connector_cls.source_id):
                captured_forms = []

                def fake_http_get(url, params=None, headers=None, **kwargs):
                    captured_forms.append(params["forms"])
                    return FakeHttpResponse({
                        "hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}
                    })

                with patch("shared.sec_efts_connector.http_get", side_effect=fake_http_get):
                    connector_cls(
                        FakeControlPlane(),
                        FakeBronzeWriter(),
                        source_config={"rate_limit": {"requests_per_minute": 100000}},
                        since_date=date.today().isoformat(),
                    ).fetch(None)

                self.assertEqual(captured_forms, expected_forms)

    def test_sec_s1_falls_back_to_browse_edgar_for_unstable_form_filter(self):
        os.environ["EDGAR_USER_AGENT"] = "Auspex test@example.com"
        atom = """<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry>
<title>424B4 - Fatpipe Inc/UT (0001993400) ()</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/1993400/000164117225003434/0001641172-25-003434-index.htm"/>
<summary type="html">&lt;b&gt;Filed:&lt;/b&gt; 2025-04-09 &lt;b&gt;AccNo:&lt;/b&gt; 0001641172-25-003434 &lt;b&gt;Size:&lt;/b&gt; 3 MB</summary>
<updated>2025-04-09T16:47:17-04:00</updated>
<category scheme="https://www.sec.gov/" label="form type" term="424B4"/>
</entry>
</feed>"""

        def fake_http_get(url, params=None, headers=None, **kwargs):
            if "browse-edgar" in url:
                self.assertEqual(params["type"], "424B4")
                return FakeHttpResponse({}, text=atom)
            if "Archives/edgar/data/" in url:
                if url.endswith("-index.html") or url.endswith("-index.htm"):
                    return FakeHttpResponse({}, text="""<html><body>
<table class="tableFile" summary="Document Format Files">
<tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
<tr><td>1</td><td>FORM 424B4</td><td><a href="prospectus.htm">prospectus.htm</a></td><td>424B4</td><td>12000</td></tr>
</table>
</body></html>""")
                if url.endswith("prospectus.htm"):
                    return FakeHttpResponse({}, text="<html><body>Prospectus filing</body></html>")
            if params["forms"] == "424B4":
                raise RuntimeError("SEC EFTS 500")
            return FakeHttpResponse({"hits": {"hits": []}})

        with patch("shared.sec_efts_connector.http_get", side_effect=fake_http_get):
            batch = SecS1Connector(
                FakeControlPlane(),
                FakeBronzeWriter(),
                source_config={"rate_limit": {"requests_per_minute": 100000}},
                since_date="2025-04-09",
                to_date="2025-04-09",
            ).fetch(None)

        self.assertEqual(len(batch.records), 1)
        self.assertEqual(batch.records[0]["adsh"], "0001641172-25-003434")
        self.assertEqual(batch.records[0]["form"], "424B4")
        self.assertEqual(batch.records[0]["sec_fallback"], "browse-edgar")

    def test_contracts_connector_enriches_awards_once_and_preserves_official_evidence(self):
        captured_payloads = []
        captured_detail_calls = []
        search_award = {
            "Award ID": "A1",
            "Recipient Name": "SEARCH NAME",
            "Recipient UEI": "SEARCH-UEI",
            "recipient_id": "search-recipient-id",
            "generated_internal_id": "CONT_AWD_A1_1234",
            "internal_id": "transaction-101",
            "Mod": "P00001",
            "Action Date": "2026-06-01",
            "Transaction Amount": 1000,
            "Transaction Description": "MICROSOFT LICENSES",
        }
        award_detail = {
            "generated_unique_award_id": "CONT_AWD_A1_1234",
            "date_signed": "2026-06-02",
            "recipient": {
                "recipient_name": "LEGAL RECIPIENT LLC",
                "recipient_hash": "recipient-hash-C",
                "recipient_uei": "DETAIL-UEI",
                "recipient_unique_id": "123456789",
                "recipient_cik": "0000789019",
                "parent_recipient_name": "PARENT HOLDINGS INC",
                "parent_recipient_hash": "parent-hash-P",
                "parent_recipient_uei": "PARENT-UEI",
                "parent_recipient_unique_id": "987654321",
            },
        }

        def fake_http_post(url, json=None, **kwargs):
            captured_payloads.append(json)
            return FakeHttpResponse({
                "results": [search_award],
                "page_metadata": {"hasNext": False},
            })

        def fake_http_get(url, **kwargs):
            captured_detail_calls.append((url, kwargs))
            return FakeHttpResponse(award_detail)

        with patch("contracts.connector.http_post", side_effect=fake_http_post), patch(
            "contracts.connector.http_get", side_effect=fake_http_get
        ):
            batch = ContractsConnector(
                FakeControlPlane(),
                FakeBronzeWriter(),
                search_terms=[
                    {"symbol": "MSFT", "text": "MICROSOFT"},
                    {"symbol": "MSFT", "text": "MICROSOFT CORPORATION"},
                ],
                since_date=date.today().isoformat(),
            ).fetch(None)

        self.assertEqual(len(batch.records), 2)
        self.assertEqual(len(captured_detail_calls), 1)
        self.assertTrue(captured_detail_calls[0][0].endswith("/CONT_AWD_A1_1234/"))
        self.assertEqual(captured_detail_calls[0][1]["max_attempts"], 3)
        self.assertEqual(captured_detail_calls[0][1]["timeout"], 30.0)
        self.assertEqual(captured_payloads[0]["filters"]["recipient_search_text"], ["MICROSOFT"])
        self.assertNotIn("keywords", captured_payloads[0]["filters"])
        self.assertIn("Action Date", captured_payloads[0]["fields"])
        self.assertIn("Transaction Amount", captured_payloads[0]["fields"])
        self.assertIn("Mod", captured_payloads[0]["fields"])
        self.assertIn("Recipient UEI", captured_payloads[0]["fields"])
        self.assertIn("recipient_id", captured_payloads[0]["fields"])
        self.assertIn("generated_internal_id", captured_payloads[0]["fields"])
        self.assertEqual(captured_payloads[0]["sort"], "Transaction Amount")

        record = batch.records[0]
        self.assertNotIn("symbol", record)
        self.assertIs(record["search_transaction"], search_award)
        self.assertIs(record["award_detail"], award_detail)
        self.assertEqual(record["award_id"], "A1")
        self.assertEqual(record["generated_award_id"], "CONT_AWD_A1_1234")
        self.assertEqual(record["transaction_internal_id"], "transaction-101")
        self.assertEqual(record["modification_number"], "P00001")
        self.assertEqual(record["action_date"], "2026-06-01")
        self.assertEqual(record["transaction_amount"], 1000)
        self.assertEqual(record["transaction_description"], "MICROSOFT LICENSES")
        self.assertEqual(len(record["transaction_id"]), 64)
        corrected_transaction = dict(search_award)
        corrected_transaction["Action Date"] = "2026-06-02"
        corrected_transaction["Mod"] = "P00002"
        corrected = ContractsConnector._enriched_record("MICROSOFT", corrected_transaction, award_detail)
        self.assertEqual(corrected["transaction_id"], record["transaction_id"])
        self.assertEqual(record["legal_recipient_name"], "LEGAL RECIPIENT LLC")
        self.assertEqual(record["recipient_id"], "recipient-hash-C")
        self.assertEqual(record["recipient_uei"], "DETAIL-UEI")
        self.assertEqual(record["recipient_duns"], "123456789")
        self.assertEqual(record["recipient_cik"], "0000789019")
        self.assertEqual(record["parent_recipient_name"], "PARENT HOLDINGS INC")
        self.assertEqual(record["parent_recipient_id"], "parent-hash-P")
        self.assertEqual(record["parent_recipient_uei"], "PARENT-UEI")
        self.assertEqual(record["parent_recipient_duns"], "987654321")

    def test_contracts_connector_omits_recipient_cik_when_official_payload_omits_it(self):
        search_award = {
            "Award ID": "A1", "generated_internal_id": "CONT_AWD_A1_1234",
            "internal_id": "transaction-101", "Mod": "0", "Action Date": "2026-06-02",
        }
        award_detail = {
            "date_signed": "2026-06-02",
            "recipient": {"recipient_name": "LEGAL RECIPIENT LLC"},
        }

        with patch(
            "contracts.connector.http_post",
            return_value=FakeHttpResponse({"results": [search_award], "page_metadata": {"hasNext": False}}),
        ), patch("contracts.connector.http_get", return_value=FakeHttpResponse(award_detail)):
            batch = ContractsConnector(
                FakeControlPlane(),
                FakeBronzeWriter(),
                search_terms=["LEGAL RECIPIENT"],
                since_date=date.today().isoformat(),
            ).fetch(None)

        self.assertNotIn("recipient_cik", batch.records[0])

    def test_contracts_transient_detail_failure_does_not_land_or_advance_watermark(self):
        cp = FakeControlPlane()
        bw = FakeBronzeWriter()
        search_award = {
            "Award ID": "A1", "generated_internal_id": "CONT_AWD_A1_1234",
            "internal_id": "transaction-101", "Mod": "0", "Action Date": "2026-06-02",
        }

        with patch(
            "contracts.connector.http_post",
            return_value=FakeHttpResponse({"results": [search_award], "page_metadata": {"hasNext": False}}),
        ), patch("contracts.connector.http_get", side_effect=HttpxTimeoutException("detail timed out")):
            result = ContractsConnector(
                cp,
                bw,
                search_terms=["LEGAL RECIPIENT"],
                since_date=date.today().isoformat(),
            ).run(RunContext(run_id="contracts-run-1", source_id="contracts"))

        self.assertEqual(result.status, "failed")
        self.assertIn("detail timed out", result.error)
        self.assertEqual(bw.writes, [])
        self.assertIsNone(cp.watermark)
        self.assertEqual(cp.dedup, set())

    def test_news_connector_fetches_company_news_for_universe(self):
        os.environ["FINNHUB_API_KEY"] = "test-key"

        def fake_http_get(url, params=None, **kwargs):
            self.assertEqual(params["symbol"], "AAPL")
            return FakeHttpResponse([{"id": 1, "headline": "Apple headline", "datetime": 1782518400}])

        writer = FakeUniverseBronzeWriter(["AAPL"], portfolio_symbols=["AAPL"])
        with patch("news.connector.http_get", side_effect=fake_http_get):
            batch = NewsConnector(
                FakeControlPlane(),
                writer,
                since_date=date.today().isoformat(),
                source_config={"rate_limit": {"requests_per_minute": 100000}},
            ).fetch(None)

        self.assertEqual(batch.records[0]["symbol"], "AAPL")
        self.assertEqual(writer.requested_universes, [("alpha_vantage", "active")])

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

    def test_news_connector_chunks_universe_by_offset_and_limit(self):
        os.environ["FINNHUB_API_KEY"] = "test-key"
        captured_symbols = []

        def fake_http_get(url, params=None, **kwargs):
            captured_symbols.append(params["symbol"])
            return FakeHttpResponse([])

        with patch("news.connector.http_get", side_effect=fake_http_get):
            batch = NewsConnector(
                FakeControlPlane(),
                FakeUniverseBronzeWriter(["AAPL", "MSFT", "NVDA"]),
                since_date=date.today().isoformat(),
                symbol_offset=1,
                symbol_limit=1,
                source_config={"rate_limit": {"requests_per_minute": 100000}},
            ).fetch(None)

        self.assertEqual(captured_symbols, ["MSFT"])
        self.assertIn("symbols-1-of-3-offset-1-limit-1", batch.window)

    def test_etf_holdings_connector_fetches_alpha_vantage_profile(self):
        os.environ["ALPHAVANTAGE_API_KEY"] = "test-key"
        os.environ["AV_RPM"] = "100000"

        def fake_http_get(url, params=None, **kwargs):
            self.assertEqual(params["function"], "ETF_PROFILE")
            self.assertEqual(params["symbol"], "SMH")
            return FakeHttpResponse({"holdings": [{"symbol": "MSFT", "weight": "8.5"}]})

        with patch("etf_holdings.connector.http_get", side_effect=fake_http_get):
            batch = EtfHoldingsConnector(FakeControlPlane(), FakeBronzeWriter(), etf_symbols=["SMH"]).fetch(None)

        self.assertEqual(batch.records[0]["function"], "ETF_PROFILE")
        self.assertEqual(batch.records[0]["context"]["symbol"], "SMH")
        self.assertEqual(
            batch.records[0]["context"]["theme_components"][0]["theme_id"],
            "ai_compute_semiconductors",
        )
        self.assertRegex(batch.window, r"-etfs-1-[0-9a-f]{16}$")

    def test_theme_catalog_rejects_invalid_blend_weights(self):
        with self.assertRaisesRegex(ValueError, "blend weights must sum to 1"):
            _validate_theme_catalog({
                "themes": [{
                    "theme_id": "data_center_buildout",
                    "name": "Data Center Buildout",
                    "benchmark_symbol": "DTCR",
                    "components": [
                        {"etf_symbol": "DTCR", "blend_weight": 0.5},
                        {"etf_symbol": "GRID", "blend_weight": 0.2},
                    ],
                }],
            })


if __name__ == "__main__":
    unittest.main()