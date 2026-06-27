import sys
import types
import unittest
import os
from pathlib import Path
from datetime import date
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
from prices_eod.connector import PricesEodConnector


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


if __name__ == "__main__":
    unittest.main()