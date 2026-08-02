import json
from pathlib import Path
from typing import Protocol


PROMPT_VERSION = "e16_grounded_v1"


class RecommendationNarrator(Protocol):
    model_version: str

    def narrate(self, recommendation: dict, citations: list[dict]) -> dict: ...


class AzureOpenAIGroundedNarrator:
    def __init__(self, chat_client, *, model_version: str) -> None:
        self._chat = chat_client
        self.model_version = model_version
        self._instructions = (
            Path(__file__).with_name("foundry_config") / "instructions.txt"
        ).read_text(encoding="utf-8")

    def narrate(self, recommendation: dict, citations: list[dict]) -> dict:
        evidence = [
            {
                "id": citation.get("id"),
                "symbol": citation.get("symbol"),
                "title": citation.get("title"),
                "excerpt": citation.get("excerpt"),
                "event_date": citation.get("event_date"),
                "knowledge_date": citation.get("knowledge_date"),
                "content_status": citation.get("content_status"),
            }
            for citation in citations
        ]
        response = self._chat.complete_json([
            {"role": "system", "content": self._instructions},
            {
                "role": "user",
                "content": json.dumps({
                    "task": "Explain the supplied deterministic recommendation.",
                    "output_schema": {
                        "recommendation_id": "exact supplied value",
                        "ticker": "exact supplied value",
                        "action": "exact supplied value",
                        "explanation": "plain-language grounded explanation",
                        "uncertainty": "explicit limitations",
                        "evidence_ids": ["one or more supplied evidence IDs"],
                    },
                    "recommendation": recommendation,
                    "untrusted_evidence": evidence,
                }, sort_keys=True),
            },
        ])
        try:
            parsed = json.loads(response)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("narrator returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("narrator output must be an object")
        return parsed