"""Blob Storage client factory — managed identity, no connection strings (arc42 TC-04).

Layout (arc42 §5.11):
```
documents/{security_id}/{document_id}.{ext}      raw filing or article
sections/{security_id}/{document_id}/{item}.txt  targeted sections, verbatim
exports/{user_id}/{upload_id}.{ext}               broker statements
```
"""

from __future__ import annotations

from functools import lru_cache

from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob.aio import BlobServiceClient

from auspex.settings import Settings, get_settings


class BlobContext:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._credential = DefaultAzureCredential()
        self._client = BlobServiceClient(self._settings.blob_account_url, credential=self._credential)

    async def upload_document_blob(self, security_id: str, document_id: str, ext: str, content: bytes | str) -> str:
        path = f"{security_id}/{document_id}.{ext}"
        container = self._client.get_container_client(self._settings.blob_container_documents)
        data = content.encode("utf-8") if isinstance(content, str) else content
        await container.upload_blob(name=path, data=data, overwrite=True)
        return f"{self._settings.blob_container_documents}/{path}"

    async def upload_section_blob(self, security_id: str, document_id: str, item: str, content: str) -> str:
        path = f"{security_id}/{document_id}/{item}.txt"
        container = self._client.get_container_client(self._settings.blob_container_sections)
        await container.upload_blob(name=path, data=content.encode("utf-8"), overwrite=True)
        return f"{self._settings.blob_container_sections}/{path}"

    async def upload_export_blob(self, user_id: str, upload_id: str, ext: str, content: bytes) -> str:
        path = f"{user_id}/{upload_id}.{ext}"
        container = self._client.get_container_client(self._settings.blob_container_exports)
        await container.upload_blob(name=path, data=content, overwrite=True)
        return f"{self._settings.blob_container_exports}/{path}"

    async def download_text(self, container_name: str, path: str) -> str:
        container = self._client.get_container_client(container_name)
        stream = await container.get_blob_client(path).download_blob()
        data = await stream.readall()
        return data.decode("utf-8")

    async def download_document_text(self, blob_path: str) -> str:
        """Read back the raw text behind a stored ``Document.blob_path``.

        ``blob_path`` is the full path this class itself returns from
        ``upload_document_blob``/``upload_section_blob`` — ``{container}/{path}``
        (arc42 §5.11 layout) — so this splits it back into the container
        name and blob-relative path :meth:`download_text` expects, sparing
        callers (:mod:`auspex.pipeline.repo_access`) from re-deriving that
        convention. Returns ``""`` for an empty/malformed path rather than
        raising — a document whose blob can't be located degrades that
        document's extraction, it does not abort the run.
        """

        if not blob_path or "/" not in blob_path:
            return ""
        container_name, path = blob_path.split("/", 1)
        return await self.download_text(container_name, path)

    async def aclose(self) -> None:
        await self._client.close()
        await self._credential.close()


@lru_cache
def get_blob_context() -> BlobContext:
    return BlobContext()
