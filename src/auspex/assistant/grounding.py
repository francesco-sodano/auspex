"""Grounding constraints (arc42 §5.10 "Grounding constraints").

The answer LLM may not state a number absent from retrieved context, cite an
unretrieved document, suggest an action not present in the retrieved
``recommendations``, or extrapolate beyond evidence. This module provides
the deterministic, code-side checks that keep those constraints enforceable
rather than merely requested in a prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from auspex.assistant.retrieval import RetrievedItem

CITATION_PATTERN = re.compile(r"\[cite:([^\]]+)\]")


@dataclass(frozen=True)
class GroundingViolation:
    kind: str
    detail: str


def retrieved_document_ids(items: list[RetrievedItem]) -> set[str]:
    return {item.document_id for item in items if item.document_id is not None}


def find_citation_markers(answer_text: str) -> list[str]:
    return CITATION_PATTERN.findall(answer_text)


def check_citations_resolve(answer_text: str, items: list[RetrievedItem]) -> list[GroundingViolation]:
    """Every ``[cite:doc_id]`` marker must resolve to a retrieved document_id."""

    valid_ids = retrieved_document_ids(items)
    violations = []
    for marker in find_citation_markers(answer_text):
        if marker not in valid_ids:
            violations.append(GroundingViolation("unresolved_citation", marker))
    return violations


def check_citations_present(answer_text: str, items: list[RetrievedItem]) -> list[GroundingViolation]:
    """A factual answer backed by retrieved items must cite at least one of them."""

    if not items or find_citation_markers(answer_text):
        return []
    return [GroundingViolation("missing_citation", "retrieved facts were used without a citation marker")]


def check_truncation_disclosed(answer_text: str, truncated: bool) -> list[GroundingViolation]:
    """If retrieval was truncated, the answer must say so (arc42: never silent)."""

    if not truncated:
        return []
    disclosure_markers = ("truncat", "narrowed", "did not have the full", "only read")
    if any(marker in answer_text.lower() for marker in disclosure_markers):
        return []
    return [GroundingViolation("undisclosed_truncation", "retrieval was truncated but the answer did not say so")]
