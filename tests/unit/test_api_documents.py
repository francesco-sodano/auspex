"""Unit tests for `GET /api/documents/{id}/section/{item}` (arc42 §11)."""

from __future__ import annotations

from datetime import UTC, datetime

from auspex.api.auth import AuthenticatedUser, get_current_user
from auspex.api.repos import get_document_repo
from auspex.api.routes import documents
from auspex.models.document import Document
from auspex.models.enums import DocumentType
from auspex.persistence.blob_client import get_blob_context
from tests.unit.conftest import FakeCosmosRepository, make_router_app


class FakeBlobContext:
    def __init__(self, contents: dict[str, str] | None = None) -> None:
        self.contents = contents or {}
        self.calls: list[tuple[str, str]] = []

    async def download_text(self, container_name: str, path: str) -> str:
        self.calls.append((container_name, path))
        return self.contents[f"{container_name}/{path}"]


def _document(section_blob_paths: dict[str, str] | None = None) -> Document:
    return Document(
        id="doc-1",
        security_id="sec-a",
        source="edgar",
        source_record_id="acc-1",
        document_type=DocumentType.FORM_10K,
        content_hash="sha256:abc",
        retrieved_at=datetime.now(UTC),
        knowledge_date="2026-08-08",
        section_blob_paths=section_blob_paths or {},
    )


def _make_client(document_repo=None, blob=None, authed: bool = True):
    overrides = {
        get_document_repo: lambda: document_repo or FakeCosmosRepository(),
        get_blob_context: lambda: blob or FakeBlobContext(),
    }
    if authed:
        overrides[get_current_user] = lambda: AuthenticatedUser(user_id="owner-1", claims={})
    return make_router_app(documents.router, overrides)


def test_requires_auth():
    client = _make_client(authed=False)
    response = client.get("/api/documents/doc-1/section/item1a")
    assert response.status_code == 401


def test_404_for_unknown_document():
    client = _make_client(document_repo=FakeCosmosRepository([]))
    response = client.get("/api/documents/unknown/section/item1a")
    assert response.status_code == 404


def test_404_for_unknown_section_item():
    document = _document(section_blob_paths={"item1a": "sections/sec-a/doc-1/item1a.txt"})
    client = _make_client(document_repo=FakeCosmosRepository([document]))
    response = client.get("/api/documents/doc-1/section/item7")
    assert response.status_code == 404


def test_returns_verbatim_text_from_the_container_named_in_the_stored_path():
    document = _document(section_blob_paths={"item1a": "sections/sec-a/doc-1/item1a.txt"})
    blob = FakeBlobContext({"sections/sec-a/doc-1/item1a.txt": "Risk factors verbatim text."})
    client = _make_client(document_repo=FakeCosmosRepository([document]), blob=blob)

    response = client.get("/api/documents/doc-1/section/item1a")

    assert response.status_code == 200
    assert response.json() == {"document_id": "doc-1", "item": "item1a", "text": "Risk factors verbatim text."}
    assert blob.calls == [("sections", "sec-a/doc-1/item1a.txt")]
