"""Uniform read access across in-memory test fixtures and production
Cosmos/Blob-backed sinks (arc42 §6.1).

Every ``*_sink``/``*_repo`` on :class:`~auspex.pipeline.context.PipelineRepos`
is a duck-typed protocol (:mod:`auspex.collectors.base`,
:mod:`auspex.persistence.repositories`) implemented two different ways in
this codebase:

- synchronous in-memory fakes (:mod:`auspex.persistence.memory`, test
  fixtures) exposing a plain ``.all()`` list and an in-process
  ``.documents`` blob dict;
- asynchronous Cosmos/Blob-backed production adapters
  (:mod:`auspex.persistence.repositories`, :mod:`auspex.persistence.blob_client`)
  exposing an async ``.all()`` (or the lower-level ``.query()`` on a bare
  :class:`~auspex.persistence.repositories.CosmosRepository`) and
  ``download_document_text``.

Pipeline steps need to read "every row of X" (new documents this run,
existing extractions/digests, prior scores, ...) and "the raw text behind
this document" without caring which kind of sink they were wired against —
that bridging lives here so :mod:`auspex.pipeline.steps` stays pure
orchestration and genuinely works against either wiring.
"""

from __future__ import annotations

import inspect
from typing import Any


async def fetch_all(sink: Any | None) -> list:
    """Return every stored row for ``sink``, however it exposes them.

    Tries, in order:

    1. a synchronous ``.all()`` (in-memory fixtures);
    2. an asynchronous ``.all()`` (Cosmos-backed adapters in
       :mod:`auspex.persistence.repositories`);
    3. a raw ``.query()`` (a bare
       :class:`~auspex.persistence.repositories.CosmosRepository` passed
       directly as a sink, e.g. ``score_repo``/``recommendation_repo``).

    Returns an empty list for ``None`` or a sink exposing none of the above
    — callers degrade gracefully rather than raising (arc42 §6.1: a missing
    dependency degrades coverage, it does not abort the run).
    """

    if sink is None:
        return []

    all_attr = getattr(sink, "all", None)
    if all_attr is not None:
        result = all_attr()
        return await result if inspect.isawaitable(result) else result

    query_attr = getattr(sink, "query", None)
    if query_attr is not None:
        result = query_attr("SELECT * FROM c")
        return await result if inspect.isawaitable(result) else result

    return []


async def read_blob_text(blob_sink: Any | None, blob_path: str | None) -> str:
    """Return the raw text stored at ``blob_path``.

    Works against both the in-memory fixture
    (:class:`auspex.persistence.memory.InMemoryBlobSink`'s ``.documents``
    dict) and the production adapter
    (:meth:`auspex.persistence.blob_client.BlobContext.download_document_text`).
    Returns ``""`` for a missing path/sink rather than raising — a document
    whose blob is unreadable degrades that document's extraction, it does
    not abort the run.
    """

    if blob_sink is None or not blob_path:
        return ""

    reader = getattr(blob_sink, "download_document_text", None)
    if reader is not None:
        return await reader(blob_path)

    documents = getattr(blob_sink, "documents", None)
    if documents is not None:
        return str(documents.get(blob_path, ""))

    return ""
