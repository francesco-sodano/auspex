"""Replay-safe evidence projection indexing."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from itertools import islice

from .evidence import evidence_document_id


SEARCH_FIELDS = {
    "id",
    "security_sk",
    "symbol",
    "source_type",
    "source_id",
    "source_name",
    "source_url",
    "title",
    "content",
    "event_date",
    "knowledge_date",
    "published_at",
    "revision_hash",
    "chunk_index",
    "generation",
    "content_status",
    "sentiment",
    "relevance",
    "sentiment_model_version",
    "sentiment_prompt_version",
    "sentiment_cache_key",
}

OPTIONAL_FIELDS = {
    "security_sk",
    "symbol",
    "source_name",
    "source_url",
    "title",
    "published_at",
    "sentiment",
    "relevance",
    "sentiment_model_version",
    "sentiment_prompt_version",
    "sentiment_cache_key",
}
REQUIRED_FIELDS = SEARCH_FIELDS - OPTIONAL_FIELDS


def _batches(values: list, size: int):
    iterator = iter(values)
    while batch := list(islice(iterator, size)):
        yield batch


def _parse_date(value: str) -> date:
    normalized = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).date() if "T" in normalized else date.fromisoformat(normalized)


def _search_datetime(value: str | None) -> str | None:
    if not value:
        return None
    normalized = str(value)
    if "T" not in normalized:
        return f"{normalized}T00:00:00Z"
    parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def validate_projection(documents: list[dict]) -> str:
    if not documents:
        raise ValueError("evidence projection is empty")
    generations = {document.get("generation") for document in documents}
    if len(generations) != 1 or None in generations:
        raise ValueError("evidence projection must contain exactly one generation")
    seen_ids = set()
    for document in documents:
        missing = REQUIRED_FIELDS - document.keys()
        if missing:
            raise ValueError(f"evidence document is missing fields: {sorted(missing)}")
        expected_id = evidence_document_id(
            document["source_type"],
            document["source_id"],
            document["revision_hash"],
            int(document["chunk_index"]),
        )
        if document["id"] != expected_id:
            raise ValueError("evidence document id does not match its revision identity")
        if document["id"] in seen_ids:
            raise ValueError("evidence projection contains duplicate ids")
        if _parse_date(document["event_date"]) > _parse_date(document["knowledge_date"]):
            raise ValueError("evidence event_date cannot exceed knowledge_date")
        if not str(document["content"]).strip():
            raise ValueError("evidence content cannot be empty")
        seen_ids.add(document["id"])
    return next(iter(generations))


class EvidenceIndexer:
    def __init__(self, search_client, embeddings, schema: dict) -> None:
        self._search = search_client
        self._embeddings = embeddings
        self._schema = schema

    def sync(
        self,
        documents: list[dict],
        batch_size: int = 16,
        embedding_workers: int = 1,
    ) -> dict:
        if embedding_workers < 1 or embedding_workers > 8:
            raise ValueError("embedding_workers must be between 1 and 8")
        generation = validate_projection(documents)
        self._search.ensure_index(self._schema)
        existing_ids = self._search.list_generation_ids(generation)
        pending_documents = [
            document for document in documents if document["id"] not in existing_ids
        ]
        uploaded = 0
        batches = iter(_batches(pending_documents, batch_size))
        with ThreadPoolExecutor(max_workers=embedding_workers) as executor:
            while True:
                batch_window = list(islice(batches, embedding_workers))
                if not batch_window:
                    break
                future_batches = {
                    executor.submit(
                        self._embeddings.embed,
                        [document["content"] for document in batch],
                    ): batch
                    for batch in batch_window
                }
                for future in as_completed(future_batches):
                    batch = future_batches[future]
                    vectors = future.result()
                    if len(vectors) != len(batch):
                        raise RuntimeError("embedding count does not match evidence batch")
                    search_documents = []
                    for document, vector in zip(batch, vectors):
                        search_document = {
                            field: document.get(field)
                            for field in SEARCH_FIELDS
                            if document.get(field) is not None
                        }
                        search_document["event_date"] = _search_datetime(document["event_date"])
                        search_document["knowledge_date"] = _search_datetime(document["knowledge_date"])
                        search_document["published_at"] = _search_datetime(document.get("published_at"))
                        search_document["content_vector"] = vector
                        search_documents.append(search_document)
                    uploaded += self._search.upload_documents(search_documents)
        deleted = self._search.delete_stale_generation(generation)
        return {
            "generation": generation,
            "documents": len(documents),
            "existing": len(existing_ids),
            "uploaded": uploaded,
            "deleted_stale": deleted,
        }
