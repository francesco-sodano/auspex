"""Cosmos/Blob persistence — managed identity, idempotent upserts (arc42 §5.11, TC-04)."""

from __future__ import annotations

from auspex.persistence.blob_client import BlobContext, get_blob_context
from auspex.persistence.cosmos_client import (
    CONTAINER_PARTITION_KEYS,
    CosmosContext,
    SourceLedgerCosmosContext,
    get_cosmos_context,
    get_source_ledger_context,
)
from auspex.persistence.repositories import CosmosRepository

__all__ = [
    "BlobContext",
    "get_blob_context",
    "CONTAINER_PARTITION_KEYS",
    "CosmosContext",
    "SourceLedgerCosmosContext",
    "get_cosmos_context",
    "get_source_ledger_context",
    "CosmosRepository",
]
