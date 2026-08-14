"""Two-pass conversational assistant (arc42 §5.10, §6.2).

Pass 1: planner LLM -> deterministic Cosmos fetch (budget-enforced,
user_id-scoped) [+ Blob if verbatim needed]. Pass 2: answer LLM, streaming,
with inline citation markers.
"""

from __future__ import annotations

from auspex.assistant.answer import AnswerGenerator
from auspex.assistant.grounding import (
    GroundingViolation,
    check_citations_present,
    check_citations_resolve,
    check_truncation_disclosed,
    find_citation_markers,
    retrieved_document_ids,
)
from auspex.assistant.planner import FIXED_DATA_CLASSES, RetrievalPlanner
from auspex.assistant.retrieval import (
    MAX_BUDGET_TOKENS,
    MAX_VERBATIM_SECTIONS,
    DataClassRepos,
    RetrievalFetcher,
    RetrievalResult,
    RetrievedItem,
    estimate_tokens,
)

__all__ = [
    "AnswerGenerator",
    "GroundingViolation",
    "check_citations_present",
    "check_citations_resolve",
    "check_truncation_disclosed",
    "find_citation_markers",
    "retrieved_document_ids",
    "FIXED_DATA_CLASSES",
    "RetrievalPlanner",
    "MAX_BUDGET_TOKENS",
    "MAX_VERBATIM_SECTIONS",
    "DataClassRepos",
    "RetrievalFetcher",
    "RetrievalResult",
    "RetrievedItem",
    "estimate_tokens",
]
