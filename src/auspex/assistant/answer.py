"""Two-pass retrieval — Pass 2 grounded, streaming answer (arc42 §5.10, §6.2).

Streams the answer as SSE with inline citation markers; every claim must
resolve to a retrieved ``document_id``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from auspex.assistant.retrieval import RetrievalResult
from auspex.providers.openai_provider import AzureOpenAIClient


class AnswerGenerator:
    prompt_version = "answer-v2"

    def __init__(self, *, openai_client: AzureOpenAIClient, deployment: str, system_prompt: str) -> None:
        self._openai = openai_client
        self._deployment = deployment
        self._system_prompt = system_prompt

    def build_user_content(self, question: str, retrieval: RetrievalResult, conversation_state: dict) -> str:
        payload = {
            "question": question,
            "retrieved_context": [
                {
                    "data_class": item.data_class,
                    "content": item.content,
                    "document_id": item.document_id,
                    "source_url": item.source_url,
                }
                for item in retrieval.items
            ],
            "truncated": retrieval.truncated,
            "truncated_scope": retrieval.truncated_scope,
            "conversation_state": conversation_state,
        }
        return json.dumps(payload, ensure_ascii=False)

    async def stream_answer(
        self, question: str, retrieval: RetrievalResult, conversation_state: dict
    ) -> AsyncIterator[str]:
        user_content = self.build_user_content(question, retrieval, conversation_state)
        async for chunk in self._openai.stream_text(
            deployment=self._deployment, system_prompt=self._system_prompt, user_content=user_content
        ):
            yield chunk
