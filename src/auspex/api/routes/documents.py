"""Verbatim document section retrieval (arc42 §11 `GET /api/documents/{id}/section/{item}`).

`Document.section_blob_paths` stores each item's blob path already prefixed
with its container name (``sections/{security_id}/{document_id}/{item}.txt``,
see `auspex.persistence.blob_client.BlobContext.upload_section_blob`), so
retrieval here just looks the document up cross-partition by id, splits the
container name back off, and streams the verbatim text via managed identity
— no connection string, no blob path guessing.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from auspex.api.auth import AuthenticatedUser, get_current_user
from auspex.api.repos import get_document_repo
from auspex.models.document import Document
from auspex.persistence.blob_client import BlobContext, get_blob_context
from auspex.persistence.repositories import CosmosRepository

router = APIRouter(prefix="/documents", tags=["documents"])


async def _find_document(repo: CosmosRepository, document_id: str) -> Document | None:
    rows = await repo.query(
        query="SELECT * FROM c WHERE c.id = @id",
        parameters=[{"name": "@id", "value": document_id}],
        partition_key=None,
    )
    return rows[0] if rows else None


@router.get("/{document_id}/section/{item}")
async def get_document_section(
    document_id: str,
    item: str,
    user: AuthenticatedUser = Depends(get_current_user),
    document_repo: CosmosRepository = Depends(get_document_repo),
    blob: BlobContext = Depends(get_blob_context),
) -> dict:
    document = await _find_document(document_repo, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown document")

    blob_path = document.section_blob_paths.get(item)
    if blob_path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no section {item!r} for this document")

    container_name, _, path = blob_path.partition("/")
    text = await blob.download_text(container_name, path)
    return {"document_id": document_id, "item": item, "text": text}
