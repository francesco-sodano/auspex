"""Cosmos repository providers for containers not already wired in `deps.py`.

`auspex.api.deps` is owned by a different worker and out of scope for this
change; this module holds the additional `@lru_cache`-memoised repository
factories the new §11 routes need (``documents``, ``digests``,
``extractions``, ``leg_changes``, ``conversations``), following the exact
pattern `auspex.api.deps` already established — same `CosmosContext`, same
`CosmosRepository` generic wrapper, same managed-identity-only client
(arc42 TC-04).
"""

from __future__ import annotations

from functools import lru_cache

from auspex.models.conversation import ConversationTurn
from auspex.models.document import Document
from auspex.models.extraction import ChannelAExtraction, ChannelBDigest
from auspex.models.scoring import LegChange
from auspex.persistence.cosmos_client import get_cosmos_context
from auspex.persistence.repositories import CosmosRepository


@lru_cache
def get_document_repo() -> CosmosRepository[Document]:
    return CosmosRepository(get_cosmos_context(), "documents", Document)


@lru_cache
def get_digest_repo() -> CosmosRepository[ChannelBDigest]:
    return CosmosRepository(get_cosmos_context(), "digests", ChannelBDigest)


@lru_cache
def get_extraction_repo() -> CosmosRepository[ChannelAExtraction]:
    return CosmosRepository(get_cosmos_context(), "extractions", ChannelAExtraction)


@lru_cache
def get_leg_change_repo() -> CosmosRepository[LegChange]:
    return CosmosRepository(get_cosmos_context(), "leg_changes", LegChange)


@lru_cache
def get_conversation_repo() -> CosmosRepository[ConversationTurn]:
    return CosmosRepository(get_cosmos_context(), "conversations", ConversationTurn)
