"""Two-pass retrieval planner — Pass 1 (arc42 §5.10).

Converts the owner's question + conversation state into a deterministic
:class:`~auspex.models.conversation.RetrievalPlan`. The planner never
answers the question and never invents data; only a fixed, taxonomy-checked
vocabulary of data classes may appear in the plan.
"""

from __future__ import annotations

import json

from auspex.models.conversation import ConversationState, RetrievalPlan
from auspex.providers.openai_provider import AzureOpenAIClient

FIXED_DATA_CLASSES = frozenset(
    {
        "score_snapshot",
        "leg_history",
        "leg_changes",
        "document_digest",
        "document_section",
        "risk_diff",
        "fundamentals",
        "insider_activity",
        "portfolio_state",
        "recommendations",
        "narrative_history",
        "performance",
    }
)


class RetrievalPlanner:
    prompt_version = "planner-v1"

    def __init__(self, *, openai_client: AzureOpenAIClient, deployment: str, system_prompt: str) -> None:
        self._openai = openai_client
        self._deployment = deployment
        self._system_prompt = system_prompt

    def build_user_content(self, question: str, state: ConversationState, universe_tickers: list[str]) -> str:
        payload = {
            "question": question,
            "conversation_state": state.model_dump(mode="json"),
            "universe": universe_tickers,
        }
        return json.dumps(payload, ensure_ascii=False)

    def parse_response(self, raw_json: str) -> RetrievalPlan:
        data = json.loads(raw_json)
        date_range = data.get("date_range") or {}
        data_classes = [dc for dc in data.get("data_classes", []) if dc in FIXED_DATA_CLASSES]
        return RetrievalPlan(
            securities=data.get("securities", []),
            date_range_start=date_range.get("start"),
            date_range_end=date_range.get("end"),
            data_classes=data_classes,
            structured_filters=data.get("structured_filters", {}),
            needs_verbatim=bool(data.get("needs_verbatim", False)),
        )

    async def plan(self, question: str, state: ConversationState, universe_tickers: list[str]) -> RetrievalPlan:
        user_content = self.build_user_content(question, state, universe_tickers)
        raw_json = await self._openai.complete_json(
            deployment=self._deployment, system_prompt=self._system_prompt, user_content=user_content
        )
        return self.parse_response(raw_json)
