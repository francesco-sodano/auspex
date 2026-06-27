"""Write NDJSON batch files to the OneLake bronze layer via ADLS Gen2 API."""
import json
import re
from datetime import date

from azure.identity import DefaultAzureCredential
from azure.storage.filedatalake import DataLakeServiceClient

_ONELAKE_BASE = "https://onelake.dfs.fabric.microsoft.com"
_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)


class BronzeWriter:
    def __init__(self, workspace_id: str, lakehouse_name: str = "auspex_bronze") -> None:
        self._fs = DataLakeServiceClient(
            _ONELAKE_BASE, DefaultAzureCredential()
        ).get_file_system_client(workspace_id)
        # When a GUID is passed the ADLS Gen2 path uses bare GUID/Files/...
        # When a friendly name is passed it uses name.Lakehouse/Files/...
        if _UUID_RE.match(lakehouse_name):
            self._lakehouse_root = lakehouse_name
        else:
            self._lakehouse_root = f"{lakehouse_name}.Lakehouse"

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
        self._fs.get_file_client(path).upload_data(data, overwrite=True)
        return len(data)

    def read_universe(self) -> list:
        """Return the prices symbol universe written by nb_01. Empty list if not yet seeded."""
        path = f"{self._lakehouse_root}/Files/config/prices_universe.json"
        try:
            data = self._fs.get_file_client(path).download_file().readall()
            return json.loads(data).get("symbols", [])
        except Exception:
            return []
