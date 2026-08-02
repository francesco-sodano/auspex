"""Immutable E21 narrative feature extraction and serving projection."""

from datetime import datetime, timezone
import hashlib
import json

from engine.narrative_features import (
    PROMPT_VERSION,
    PROMPT_SHA256,
    narrative_cache_key,
    narrative_messages,
    parse_narrative_response,
)


class CosmosNarrativeFeatureCache:
    def __init__(self, container) -> None:
        self._container = container

    def get(self, cache_key: str) -> dict | None:
        try:
            return self._container.read_item(item=cache_key, partition_key=cache_key)
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                return None
            raise

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
                "document_id",
                "security_sk",
                "source_id",
                "source_type",
                "document_revision_hash",
                "event_date",
                "knowledge_date",
                "model_version",
                "prompt_version",
                "prompt_sha256",
                "sentiment",
                "relevance",
                "forward_promise_ratio",
                "hype_density",
                "themes",
                "evidence_quotes",
                "theme_evidence",
            }
            if any(existing.get(field) != document.get(field) for field in immutable_fields):
                raise RuntimeError("narrative cache identity has conflicting output")
            return existing

    def list_version(self, model_version: str, prompt_version: str) -> list[dict]:
        return list(self._container.query_items(
            query=(
                "SELECT * FROM c WHERE c.model_version = @model_version "
                "AND c.prompt_version = @prompt_version"
            ),
            parameters=[
                {"name": "@model_version", "value": model_version},
                {"name": "@prompt_version", "value": prompt_version},
            ],
            enable_cross_partition_query=True,
        ))


class NarrativeFeatureService:
    def __init__(
        self,
        chat_client,
        cache,
        *,
        model_version: str,
        prompt_text: str,
    ) -> None:
        if not model_version or not prompt_text.strip():
            raise ValueError("model_version and prompt_text are required")
        self._chat = chat_client
        self._cache = cache
        self.model_version = model_version
        self.prompt_version = PROMPT_VERSION
        self._prompt_text = prompt_text.replace("\r\n", "\n")
        prompt_hash = hashlib.sha256(self._prompt_text.encode("utf-8")).hexdigest()
        if prompt_hash != PROMPT_SHA256:
            raise ValueError(
                f"E21 prompt hash mismatch: expected={PROMPT_SHA256} actual={prompt_hash}"
            )
        self.prompt_sha256 = prompt_hash

    def cache_key(self, document: dict) -> str:
        return narrative_cache_key(
            document["revision_hash"],
            self.model_version,
            self.prompt_version,
        )

    def cached(self, document: dict) -> dict | None:
        return self._cache.get(self.cache_key(document))

    def score(self, document: dict) -> tuple[dict, bool]:
        cache_key = self.cache_key(document)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached, True
        if document.get("source_type") != "news":
            raise ValueError("E21 extraction is limited to news evidence in v1")
        content = str(document.get("content") or "").strip()
        if not content:
            raise ValueError("news evidence content is required")
        messages = narrative_messages(
            str(document.get("title") or ""),
            content,
            self._prompt_text,
        )
        raw_response = self._chat.complete_json(messages)
        try:
            features = parse_narrative_response(raw_response, content)
        except ValueError as exc:
            repair_messages = messages + [
                {"role": "assistant", "content": raw_response},
                {
                    "role": "user",
                    "content": (
                        f"The JSON failed validation: {exc}. Return corrected JSON with exactly "
                        "the required fields and only valid supplied excerpt indexes."
                    ),
                },
            ]
            raw_response = self._chat.complete_json(repair_messages)
            features = parse_narrative_response(raw_response, content)
        result = {
            "id": cache_key,
            "document_id": document["id"],
            "security_sk": document.get("security_sk"),
            "symbol": document.get("symbol"),
            "source_id": document["source_id"],
            "source_type": document["source_type"],
            "document_revision_hash": document["revision_hash"],
            "event_date": document["event_date"],
            "knowledge_date": document["knowledge_date"],
            "published_at": document.get("published_at"),
            "input_generation": document["generation"],
            "model_version": self.model_version,
            "prompt_version": self.prompt_version,
            "prompt_sha256": self.prompt_sha256,
            "sentiment": features.sentiment,
            "relevance": features.relevance,
            "forward_promise_ratio": features.forward_promise_ratio,
            "hype_density": features.hype_density,
            "themes": list(features.themes),
            "evidence_quotes": dict(features.evidence_quotes),
            "theme_evidence": dict(features.theme_evidence),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        return self._cache.create(result), False

    def list_cached(self) -> list[dict]:
        return self._cache.list_version(self.model_version, self.prompt_version)


def build_narrative_projection(
    evidence_documents: list[dict],
    cached_documents: list[dict],
) -> tuple[list[dict], dict]:
    news_documents = {
        document["id"]: document
        for document in evidence_documents
        if document.get("source_type") == "news"
    }
    input_generations = {document.get("generation") for document in evidence_documents}
    if len(input_generations) != 1 or None in input_generations:
        raise ValueError("evidence projection must contain one input generation")
    cache_ids = [document.get("id") for document in cached_documents]
    if len(cache_ids) != len(set(cache_ids)):
        raise ValueError("narrative cache contains duplicate cache ids")
    cache_document_ids = [document.get("document_id") for document in cached_documents]
    if len(cache_document_ids) != len(set(cache_document_ids)):
        raise ValueError("narrative cache contains duplicate document ids")
    cache_by_document = {document["document_id"]: document for document in cached_documents}
    missing = sorted(set(news_documents) - set(cache_by_document))
    if missing:
        raise ValueError(f"narrative cache is incomplete: missing={len(missing)}")
    stale_cache_count = len(set(cache_by_document) - set(news_documents))

    source_generation = next(iter(input_generations))
    identity = "|".join(
        f"{cache_by_document[document_id]['id']}:{document_id}"
        for document_id in sorted(news_documents)
    )
    generation = "e21-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    projection = []
    for document_id in sorted(news_documents):
        cached = dict(cache_by_document[document_id])
        source = news_documents[document_id]
        if cached.get("document_revision_hash") != source.get("revision_hash"):
            raise ValueError("narrative cache revision does not match evidence")
        cached["generation"] = generation
        cached["input_generation"] = source_generation
        projection.append(cached)
    manifest = {
        "generation": generation,
        "input_generation": source_generation,
        "document_count": len(projection),
        "stale_cache_count": stale_cache_count,
        "fingerprint": hashlib.sha256(
            json.dumps(projection, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    return projection, manifest