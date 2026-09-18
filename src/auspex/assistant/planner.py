"""Two-pass retrieval planner — Pass 1 (arc42 §5.10).

Converts the owner's question + conversation state into a deterministic
:class:`~auspex.models.conversation.RetrievalPlan`. The planner never
answers the question and never invents data; only a fixed, taxonomy-checked
vocabulary of data classes may appear in the plan.
"""

from __future__ import annotations

import json
import logging
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict

from auspex.models.common import utc_now
from auspex.models.conversation import ConversationState, RetrievalPlan
from auspex.providers.openai_provider import AzureOpenAIClient

logger = logging.getLogger(__name__)

RetrievalDataClass = Literal[
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
]
FIXED_DATA_CLASSES = frozenset(get_args(RetrievalDataClass))


class PlannerDateRange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    start: str | None
    end: str | None


class PlannerFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    item: str | None


class PlannerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    securities: list[str]
    date_range: PlannerDateRange | None
    data_classes: list[RetrievalDataClass]
    structured_filters: PlannerFilters
    needs_verbatim: bool


class RetrievalPlanner:
    prompt_version = "planner-v1"

    def __init__(self, *, openai_client: AzureOpenAIClient, deployment: str, system_prompt: str) -> None:
        self._openai = openai_client
        self._deployment = deployment
        self._system_prompt = system_prompt

    def build_user_content(self, question: str, state: ConversationState, universe_tickers: list[str]) -> str:
        payload = {
            "question": question,
            "current_date": utc_now().date().isoformat(),
            "conversation_state": state.model_dump(mode="json"),
            "universe": universe_tickers,
        }
        return json.dumps(payload, ensure_ascii=False)

    def parse_response(self, raw_json: str) -> RetrievalPlan:
        data = PlannerResponse.model_validate_json(raw_json)
        plan = RetrievalPlan(
            securities=data.securities,
            date_range_start=data.date_range.start if data.date_range else None,
            date_range_end=data.date_range.end if data.date_range else None,
            data_classes=data.data_classes,
            structured_filters=(
                {"item": data.structured_filters.item}
                if data.structured_filters.item is not None
                else {}
            ),
            needs_verbatim=data.needs_verbatim,
        )
        if (
            plan.date_range_start is not None
            and plan.date_range_end is not None
            and plan.date_range_start > plan.date_range_end
        ):
            raise ValueError("retrieval date range is reversed")
        return plan

    async def plan(self, question: str, state: ConversationState, universe_tickers: list[str]) -> RetrievalPlan:
        user_content = self.build_user_content(question, state, universe_tickers)
        schema = PlannerResponse.model_json_schema()
        schema["properties"]["securities"]["items"]["enum"] = universe_tickers
        for attempt in range(2):
            try:
                raw_json = await self._openai.complete_json(
                    deployment=self._deployment,
                    system_prompt=self._system_prompt,
                    user_content=user_content,
                    json_schema=schema,
                )
                plan = self.parse_response(raw_json)
                if set(plan.securities) - set(universe_tickers):
                    raise ValueError("retrieval plan contains an unknown security")
                return plan
            except ValueError as exc:
                logger.warning(
                    "Invalid retrieval plan (%s), attempt %d/2",
                    type(exc).__name__,
                    attempt + 1,
                )
                if attempt == 1:
                    raise ValueError("The planner could not produce a valid retrieval plan.") from exc
                payload = json.loads(user_content)
                payload["correction"] = (
                    "The previous plan failed validation. Follow the supplied JSON schema exactly, "
                    "use only the supplied universe tickers and supported filters, "
                    "and use null for an unspecified date or document item."
                )
                user_content = json.dumps(payload, ensure_ascii=False)
        raise AssertionError("unreachable")
