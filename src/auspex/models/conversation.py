"""Conversation turns and retrieval plans (`conversations` container, arc42 §5.10, §6.2)."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import Field

from auspex.models.common import AuspexModel


class RetrievalPlan(AuspexModel):
    """Pass-1 planner output. Deterministic fetch executes exactly this plan."""

    securities: list[str] = Field(default_factory=list)
    date_range_start: date | None = None
    date_range_end: date | None = None
    data_classes: list[str] = Field(default_factory=list)
    structured_filters: dict[str, str] = Field(default_factory=dict)
    needs_verbatim: bool = False


class Citation(AuspexModel):
    document_id: str
    source_url: str | None = None
    retrieved_at: datetime


class ConversationState(AuspexModel):
    """Compact carried-forward state — not raw transcript (arc42 §5.10)."""

    resolved_securities: list[str] = Field(default_factory=list)
    active_date_range_start: date | None = None
    active_date_range_end: date | None = None
    securities_under_discussion: list[str] = Field(default_factory=list)


class ConversationTurn(AuspexModel):
    """`conversations` container row, partitioned by `/user_id`."""

    id: str = Field(description="turn_id")
    user_id: str
    conversation_id: str
    turn_index: int
    question: str
    plan: RetrievalPlan | None = None
    truncated: bool = False
    truncated_scope: str | None = None
    answer: str | None = None
    citations: list[Citation] = Field(default_factory=list)
    state_after: ConversationState | None = None
    created_at: datetime

    @property
    def partition_key(self) -> str:
        return self.user_id
