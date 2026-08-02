"""Point-in-time Azure AI Search query helpers."""

from datetime import date, datetime, timezone
from typing import Iterable


def _odata_string(value: str) -> str:
    return f"'{value.replace(chr(39), chr(39) * 2)}'"


def _end_of_day(as_of: date | datetime) -> str:
    if isinstance(as_of, datetime):
        normalized = as_of.astimezone(timezone.utc) if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
        return normalized.isoformat(timespec="seconds").replace("+00:00", "Z")
    return f"{as_of.isoformat()}T23:59:59Z"


def _or_filter(field: str, values: Iterable[str]) -> str | None:
    unique_values = sorted(set(values))
    if not unique_values:
        return None
    return "(" + " or ".join(f"{field} eq {_odata_string(value)}" for value in unique_values) + ")"


def build_evidence_filter(
    *,
    as_of: date | datetime,
    security_sks: Iterable[int] = (),
    source_types: Iterable[str] = (),
) -> str:
    """Build the mandatory PIT boundary plus optional evidence scopes."""
    as_of_literal = _end_of_day(as_of)
    clauses = [
        f"event_date le {as_of_literal}",
        f"knowledge_date le {as_of_literal}",
    ]

    normalized_security_sks = sorted(set(security_sks))
    if normalized_security_sks:
        clauses.append(
            "(" + " or ".join(f"security_sk eq {security_sk}" for security_sk in normalized_security_sks) + ")"
        )

    source_filter = _or_filter("source_type", source_types)
    if source_filter:
        clauses.append(source_filter)

    return " and ".join(clauses)


class EvidenceSearchService:
    def __init__(self, search_client) -> None:
        self._search = search_client

    def retrieve(
        self,
        *,
        query: str,
        as_of: date | datetime,
        security_sks: Iterable[int] = (),
        source_types: Iterable[str] = (),
        limit: int = 10,
    ) -> list[dict]:
        normalized_query = query.strip()
        if limit < 1 or limit > 20:
            raise ValueError("limit must be between 1 and 20")
        payload = {
            "search": normalized_query or "*",
            "filter": build_evidence_filter(
                as_of=as_of,
                security_sks=security_sks,
                source_types=source_types,
            ),
            "select": ",".join([
                "id", "security_sk", "symbol", "source_type", "source_id",
                "source_name", "source_url", "title", "content", "event_date",
                "knowledge_date", "published_at", "revision_hash", "content_status",
            ]),
            "top": limit,
        }
        if normalized_query:
            payload.update({
                "queryType": "semantic",
                "semanticConfiguration": "evidence-semantic-config",
                "captions": "extractive",
                "vectorQueries": [{
                    "kind": "text",
                    "text": normalized_query,
                    "fields": "content_vector",
                    "k": max(50, limit),
                }],
            })
        else:
            payload["orderby"] = "knowledge_date desc"

        response = self._search.search(payload)
        citations = []
        for result in response.get("value", []):
            captions = result.get("@search.captions") or []
            excerpt = captions[0].get("text") if captions else str(result.get("content") or "")[:600]
            citations.append({
                "id": result["id"],
                "security_sk": result.get("security_sk"),
                "symbol": result.get("symbol"),
                "source_type": result.get("source_type"),
                "source_id": result.get("source_id"),
                "source_name": result.get("source_name"),
                "url": result.get("source_url"),
                "title": result.get("title"),
                "excerpt": excerpt,
                "event_date": result.get("event_date"),
                "knowledge_date": result.get("knowledge_date"),
                "revision_hash": result.get("revision_hash"),
                "content_status": result.get("content_status"),
                "score": result.get("@search.score"),
                "reranker_score": result.get("@search.rerankerScore"),
            })
        return citations
