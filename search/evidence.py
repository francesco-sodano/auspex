"""Deterministic identities for revisioned evidence chunks."""

import base64
import hashlib


def evidence_document_id(
    source_type: str,
    source_id: str,
    revision_hash: str,
    chunk_index: int,
) -> str:
    """Return a Search-safe identity that converges on replay."""
    if not source_type or not source_id or not revision_hash:
        raise ValueError("source_type, source_id, and revision_hash are required")
    if chunk_index < 0:
        raise ValueError("chunk_index must be non-negative")

    natural_key = f"{source_type}|{source_id}|{revision_hash}|{chunk_index}"
    digest = hashlib.sha256(natural_key.encode("utf-8")).digest()
    return "d" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
