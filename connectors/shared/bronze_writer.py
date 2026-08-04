"""Write NDJSON batch files to the OneLake bronze layer via ADLS Gen2 API."""
import json
import re
import time
from datetime import date
from decimal import Decimal

from azure.identity import DefaultAzureCredential
from azure.storage.filedatalake import DataLakeServiceClient

_ONELAKE_BASE = "https://onelake.dfs.fabric.microsoft.com"
_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
_MAX_ATTEMPTS = 4
_TRANSIENT_ERROR_MARKERS = (
    "currently not available",
    "Gateway Timeout",
    "Gateway Time-out",
    "Internal Server Error",
    "temporarily unavailable",
    "Service Unavailable",
)


def _is_transient_onelake_error(exc: Exception) -> bool:
    message = str(exc)
    return any(marker.lower() in message.lower() for marker in _TRANSIENT_ERROR_MARKERS)


def _is_missing_onelake_path(exc: Exception) -> bool:
    if getattr(exc, "status_code", None) != 404:
        return False
    error_code = str(getattr(exc, "error_code", "") or "")
    message = str(exc).lower()
    return error_code in {"PathNotFound", "BlobNotFound"} or any(
        marker in message
        for marker in ("specified path does not exist", "specified blob does not exist")
    )


def _with_onelake_retries(operation):
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return operation()
        except Exception as exc:
            if attempt == _MAX_ATTEMPTS or not _is_transient_onelake_error(exc):
                raise
            time.sleep(2 ** attempt)


class BronzeWriter:
    def __init__(
        self,
        workspace_id: str,
        lakehouse_name: str = "auspex_bronze",
        universe_container=None,
        portfolio_container=None,
    ) -> None:
        self._fs = DataLakeServiceClient(
            _ONELAKE_BASE, DefaultAzureCredential()
        ).get_file_system_client(workspace_id)
        # When a GUID is passed the ADLS Gen2 path uses bare GUID/Files/...
        # When a friendly name is passed it uses name.Lakehouse/Files/...
        if _UUID_RE.match(lakehouse_name):
            self._lakehouse_root = lakehouse_name
        else:
            self._lakehouse_root = f"{lakehouse_name}.Lakehouse"
        self._universe_container = universe_container
        self._portfolio_container = portfolio_container

    def _bronze_path(self, source_id: str, batch_id: str, partition_date: str) -> str:
        day = date.fromisoformat(partition_date)
        return (
            f"{self._lakehouse_root}/Files/bronze"
            f"/{source_id}/{day.year}/{day.month:02d}/{day.day:02d}/{batch_id}.ndjson"
        )

    def write(self, source_id: str, batch_id: str, envelopes: list, partition_date: str) -> int:
        """Write envelopes as NDJSON; returns bytes written. Overwrites on replay."""
        path = self._bronze_path(source_id, batch_id, partition_date)
        data = ("\n".join(json.dumps(e) for e in envelopes) + "\n").encode("utf-8")
        _with_onelake_retries(lambda: self._fs.get_file_client(path).upload_data(data, overwrite=True))
        return len(data)

    def read_universe(self, name: str = "prices", tier: str = None) -> list:
        """Return a configured symbol universe or one of its named tiers."""
        path = f"{self._lakehouse_root}/Files/config/{name}_universe.json"
        try:
            data = _with_onelake_retries(lambda: self._fs.get_file_client(path).download_file().readall())
            payload = json.loads(data)
            if tier is None:
                return payload.get("symbols", [])
            tiers = payload.get("tiers") or {}
            normalized = {
                tier_name: sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
                for tier_name, symbols in tiers.items()
            }
            active = set(normalized.get("active", []))
            coverage = set(normalized.get("coverage", []))
            if active - coverage:
                raise ValueError("active symbols must be included in coverage")
            active_max = (payload.get("policy") or {}).get("active_max_symbols")
            if active_max is not None and len(active) > int(active_max):
                raise ValueError(f"active universe exceeds configured maximum of {active_max} symbols")
            return normalized.get(tier, [])
        except Exception as exc:
            if _is_missing_onelake_path(exc):
                return []
            raise

    def read_portfolio_universe(self) -> list[str]:
        universe_container = getattr(self, "_universe_container", None)
        documents = universe_container.query_items(
            query="SELECT c.symbol FROM c WHERE c.active = true",
            enable_cross_partition_query=True,
        ) if universe_container is not None else []
        symbols = {
            str(document["symbol"]).strip().upper()
            for document in documents
            if document.get("symbol")
        }
        portfolio_container = getattr(self, "_portfolio_container", None)
        if portfolio_container is None:
            return sorted(symbols)
        transactions = list(portfolio_container.query_items(
            query=(
                "SELECT c.transaction_id, c.transaction_type, c.security_code, "
                "c.quantity, c.corrects_transaction_id, c.linked_transaction_id "
                "FROM c WHERE c.id != '_ledger_revision'"
            ),
            enable_cross_partition_query=True,
        ))
        corrected_ids = {
            transaction["corrects_transaction_id"]
            for transaction in transactions
            if transaction.get("corrects_transaction_id")
        }
        quantities: dict[str, Decimal] = {}
        for transaction in transactions:
            if (
                transaction.get("transaction_id") in corrected_ids
                or transaction.get("linked_transaction_id") in corrected_ids
            ):
                continue
            transaction_type = transaction.get("transaction_type")
            symbol = str(transaction.get("security_code") or "").strip().upper()
            if not symbol or transaction_type not in {"OPENING_POSITION", "BUY", "SELL"}:
                continue
            quantity = Decimal(str(transaction.get("quantity") or "0"))
            quantities[symbol] = quantities.get(symbol, Decimal("0")) + (
                -quantity if transaction_type == "SELL" else quantity
            )
        symbols.update(symbol for symbol, quantity in quantities.items() if quantity != 0)
        return sorted(symbols)

    def read_serving_projection(self, name: str) -> list[dict]:
        root = f"{self._lakehouse_root}/Files/serving/{name}"
        documents = []
        for entry in _with_onelake_retries(lambda: list(self._fs.get_paths(path=root))):
            leaf_name = entry.name.rsplit("/", 1)[-1]
            if entry.is_directory or not leaf_name.startswith("part-"):
                continue
            data = _with_onelake_retries(
                lambda path=entry.name: self._fs.get_file_client(path).download_file().readall()
            )
            documents.extend(
                json.loads(line)
                for line in data.decode("utf-8").splitlines()
                if line.strip()
            )
        return documents

    def write_serving_projection(self, name: str, documents: list[dict]) -> int:
        if not name or "/" in name or "\\" in name:
            raise ValueError("serving projection name is invalid")
        if not documents:
            raise ValueError("serving projection cannot be empty")
        path = f"{self._lakehouse_root}/Files/serving/{name}/part-00000.json"
        data = (
            "\n".join(
                json.dumps(document, sort_keys=True, separators=(",", ":"))
                for document in sorted(documents, key=lambda item: item["id"])
            )
            + "\n"
        ).encode("utf-8")
        _with_onelake_retries(
            lambda: self._fs.get_file_client(path).upload_data(data, overwrite=True)
        )
        return len(data)
