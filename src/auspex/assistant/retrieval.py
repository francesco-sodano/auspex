"""Deterministic, budget-enforced retrieval — Pass 1 fetch (arc42 §5.10).

Extraction already does the semantic work at write time (structured fields
on Channel A/B output), so "semantic-looking" questions become plain
``WHERE`` clauses over Cosmos containers rather than a vector search. This
module executes exactly the plan the Pass-1 planner produced, scoped to
``user_id``, and enforces the retrieval budget with an explicit truncation
flag rather than silently dropping evidence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from auspex.models.conversation import RetrievalPlan

MAX_BUDGET_TOKENS = 20_000
MAX_VERBATIM_SECTIONS = 3


def estimate_tokens(payload: object) -> int:
    """Rough token estimate (~4 characters/token) — good enough for budget enforcement."""

    text = json.dumps(payload, default=str, ensure_ascii=False)
    return max(1, len(text) // 4)


@dataclass(frozen=True)
class RetrievedItem:
    data_class: str
    content: dict
    security_id: str | None = None
    document_id: str | None = None
    source_url: str | None = None
    retrieved_at: datetime | None = None
    relevance_rank: int = 0  # lower is more relevant; used when truncating


@dataclass
class RetrievalResult:
    items: list[RetrievedItem] = field(default_factory=list)
    truncated: bool = False
    truncated_scope: str | None = None
    verbatim_sections_used: int = 0


class DataClassRepos:
    """Duck-typed bag of per-data-class fetch callables.

    Each attribute is an optional ``async def fetch(plan, user_id) -> list[RetrievedItem]``.
    Missing attributes simply contribute no items for that data class — this
    lets tests wire only the data classes exercised by a given scenario.
    """

    def __init__(self, **fetchers) -> None:
        self._fetchers = fetchers

    async def fetch(self, data_class: str, plan: RetrievalPlan, user_id: str) -> list[RetrievedItem]:
        fn = self._fetchers.get(data_class)
        if fn is None:
            return []
        return await fn(plan, user_id)


class RetrievalFetcher:
    def __init__(self, repos: DataClassRepos) -> None:
        self._repos = repos

    async def fetch(self, plan: RetrievalPlan, user_id: str) -> RetrievalResult:
        all_items: list[RetrievedItem] = []
        for data_class in plan.data_classes:
            if data_class == "document_section" and not plan.needs_verbatim:
                continue
            items = await self._repos.fetch(data_class, plan, user_id)
            all_items.extend(items)

        all_items.sort(key=lambda item: item.relevance_rank)

        budget = MAX_BUDGET_TOKENS
        kept: list[RetrievedItem] = []
        truncated = False
        verbatim_used = 0
        for item in all_items:
            if item.data_class == "document_section":
                if verbatim_used >= MAX_VERBATIM_SECTIONS:
                    truncated = True
                    continue
                verbatim_used += 1
            cost = estimate_tokens(item.content)
            if cost > budget:
                truncated = True
                continue
            budget -= cost
            kept.append(item)

        return RetrievalResult(
            items=kept,
            truncated=truncated,
            truncated_scope="lowest-relevance items dropped to fit the 20,000 token budget" if truncated else None,
            verbatim_sections_used=verbatim_used,
        )
