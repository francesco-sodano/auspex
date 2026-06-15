"""Write NDJSON batch files to the OneLake bronze layer via ADLS Gen2 API."""
import json
from datetime import datetime, timezone

from azure.identity import DefaultAzureCredential
from azure.storage.filedatalake import DataLakeServiceClient

_ONELAKE_BASE = "https://onelake.dfs.fabric.microsoft.com"


class BronzeWriter:
    def __init__(self, workspace_id: str, lakehouse_name: str = "auspex_bronze") -> None:
        self._fs = DataLakeServiceClient(
            _ONELAKE_BASE, DefaultAzureCredential()
        ).get_file_system_client(workspace_id)
        self._lakehouse = lakehouse_name

    def write(self, source_id: str, batch_id: str, envelopes: list) -> int:
        """Write envelopes as NDJSON; returns bytes written. Overwrites on replay."""
        now = datetime.now(timezone.utc)
        path = (
            f"{self._lakehouse}.Lakehouse/Files/bronze"
            f"/{source_id}/{now.year}/{now.month:02d}/{now.day:02d}/{batch_id}.ndjson"
        )
        data = ("\n".join(json.dumps(e) for e in envelopes) + "\n").encode("utf-8")
        self._fs.get_file_client(path).upload_data(data, overwrite=True)
        return len(data)
