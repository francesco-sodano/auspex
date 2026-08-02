"""Versioned article-level sentiment sensor for E7."""

from dataclasses import dataclass
import hashlib
import json
import re


PROMPT_VERSION = "e7_sentiment_v2"


@dataclass(frozen=True)
class SentimentScore:
    sentiment: float
    relevance: float
    evidence_quote: str


def sentiment_cache_key(
    document_revision_hash: str,
    model_version: str,
    prompt_version: str = PROMPT_VERSION,
) -> str:
    if not document_revision_hash or not model_version or not prompt_version:
        raise ValueError("document revision, model version, and prompt version are required")
    identity = f"{document_revision_hash}|{model_version}|{prompt_version}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def sentiment_messages(title: str, content: str) -> list[dict[str, str]]:
    candidates = evidence_candidates(content)
    return [
        {
            "role": "system",
            "content": (
                "You extract article-level equity sentiment as data, not advice. "
                "Return one JSON object with exactly sentiment, relevance, and evidence_index. "
                "sentiment is a number from -1 to 1. relevance is a number from 0 to 1. "
                "evidence_index is the integer index of the supplied excerpt that best supports "
                "the sentiment. Do not infer facts absent from the excerpts."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Title: {title.strip()}\n\nEvidence excerpts:\n"
                + "\n".join(
                    f"[{index}] {candidate}"
                    for index, candidate in enumerate(candidates)
                )
            ),
        },
    ]


def evidence_candidates(content: str, max_chars: int = 200) -> list[str]:
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    words = list(re.finditer(r"\S+", content))
    if not words:
        raise ValueError("sentiment content is required")

    candidates = []
    start = words[0].start()
    end = words[0].end()
    for word in words[1:]:
        if word.end() - start > max_chars:
            candidates.append(content[start:end])
            start = word.start()
        end = word.end()
    candidates.append(content[start:end])
    return candidates


def _validated_scores(payload: dict) -> tuple[float, float]:
    if isinstance(payload.get("sentiment"), bool) or not isinstance(payload.get("sentiment"), (int, float)):
        raise ValueError("sentiment must be numeric")
    if isinstance(payload.get("relevance"), bool) or not isinstance(payload.get("relevance"), (int, float)):
        raise ValueError("relevance must be numeric")
    sentiment = float(payload["sentiment"])
    relevance = float(payload["relevance"])
    if sentiment < -1.0 or sentiment > 1.0:
        raise ValueError("sentiment must be between -1 and 1")
    if relevance < 0.0 or relevance > 1.0:
        raise ValueError("relevance must be between 0 and 1")
    return sentiment, relevance


def parse_sentiment_sensor_response(raw_response: str, content: str) -> SentimentScore:
    try:
        payload = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise ValueError("sentiment response must be valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "sentiment",
        "relevance",
        "evidence_index",
    }:
        raise ValueError("sentiment response has an invalid field contract")
    sentiment, relevance = _validated_scores(payload)
    evidence_index = payload["evidence_index"]
    candidates = evidence_candidates(content)
    if isinstance(evidence_index, bool) or not isinstance(evidence_index, int):
        raise ValueError("evidence_index must be an integer")
    if evidence_index < 0 or evidence_index >= len(candidates):
        raise ValueError("evidence_index is outside the supplied excerpts")
    return SentimentScore(sentiment, relevance, candidates[evidence_index])


def parse_sentiment_response(raw_response: str, content: str) -> SentimentScore:
    try:
        payload = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise ValueError("sentiment response must be valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "sentiment",
        "relevance",
        "evidence_quote",
    }:
        raise ValueError("sentiment response has an invalid field contract")

    sentiment, relevance = _validated_scores(payload)

    evidence_quote = str(payload["evidence_quote"]).strip()
    if not evidence_quote or len(evidence_quote) > 200 or evidence_quote not in content:
        raise ValueError("evidence_quote must be a verbatim content quote of at most 200 characters")
    return SentimentScore(sentiment, relevance, evidence_quote)