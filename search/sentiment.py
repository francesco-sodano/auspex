"""Cached E7 sentiment scoring over evidence documents."""

from datetime import datetime, timezone

from azure.cosmos.exceptions import CosmosResourceNotFoundError

from engine.sentiment import (
    PROMPT_VERSION,
    parse_sentiment_sensor_response,
    sentiment_cache_key,
    sentiment_messages,
)


class CosmosSentimentCache:
    def __init__(self, container) -> None:
        self._container = container

    def get(self, cache_key: str) -> dict | None:
        try:
            return self._container.read_item(item=cache_key, partition_key=cache_key)
        except CosmosResourceNotFoundError:
            return None

    def create(self, document: dict) -> dict:
        try:
            return self._container.create_item(document)
        except Exception as exc:
            if getattr(exc, "status_code", None) != 409:
                raise
            existing = self.get(document["id"])
            if existing is None:
                raise
            immutable_fields = {
                "document_revision_hash",
                "model_version",
                "prompt_version",
                "sentiment",
                "relevance",
                "evidence_quote",
            }
            if any(existing.get(field) != document.get(field) for field in immutable_fields):
                raise RuntimeError("sentiment cache identity has conflicting output")
            return existing


class SentimentService:
    def __init__(self, chat_client, cache: CosmosSentimentCache, model_version: str) -> None:
        if not model_version:
            raise ValueError("model_version is required")
        self._chat = chat_client
        self._cache = cache
        self._model_version = model_version

    def cache_key(self, document: dict) -> str:
        return sentiment_cache_key(
            document["revision_hash"],
            self._model_version,
            PROMPT_VERSION,
        )

    def cached(self, document: dict) -> dict | None:
        return self._cache.get(self.cache_key(document))

    def score(self, document: dict) -> tuple[dict, bool]:
        cache_key = self.cache_key(document)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached, True
        if document.get("source_type") != "news":
            raise ValueError("sentiment scoring is limited to news evidence")
        content = str(document.get("content") or "").strip()
        if not content:
            raise ValueError("news evidence content is required")
        raw_response = self._chat.complete_json(
            sentiment_messages(str(document.get("title") or ""), content)
        )
        score = parse_sentiment_sensor_response(raw_response, content)
        result = {
            "id": cache_key,
            "document_id": document["id"],
            "source_id": document["source_id"],
            "document_revision_hash": document["revision_hash"],
            "model_version": self._model_version,
            "prompt_version": PROMPT_VERSION,
            "sentiment": score.sentiment,
            "relevance": score.relevance,
            "evidence_quote": score.evidence_quote,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        return self._cache.create(result), False


def enrich_with_cached_sentiment(documents: list[dict], service: SentimentService) -> int:
    enriched = 0
    for document in documents:
        if document.get("source_type") != "news":
            continue
        cached = service.cached(document)
        if cached is None:
            continue
        document.update({
            "sentiment": cached["sentiment"],
            "relevance": cached["relevance"],
            "sentiment_model_version": cached["model_version"],
            "sentiment_prompt_version": cached["prompt_version"],
            "sentiment_cache_key": cached["id"],
        })
        enriched += 1
    return enriched


def page_evidence_documents(
    documents: list[dict],
    *,
    limit: int,
    after_id: str = "",
) -> tuple[list[dict], str | None, bool]:
    if limit < 1:
        raise ValueError("limit must be positive")
    eligible = sorted(
        (
            document
            for document in documents
            if document.get("source_type") == "news"
            and str(document.get("id") or "") > after_id
        ),
        key=lambda document: document["id"],
    )
    page = eligible[:limit]
    next_after_id = page[-1]["id"] if page else None
    return page, next_after_id, len(eligible) > limit