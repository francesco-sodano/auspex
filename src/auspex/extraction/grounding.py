"""Source-span checks shared by both extraction channels."""

from __future__ import annotations


def normalise_source_text(value: str) -> str:
    return " ".join(value.split())


def source_contains(source: str, excerpt: str) -> bool:
    normalized = normalise_source_text(excerpt)
    return bool(normalized and normalized in normalise_source_text(source))


def verified_excerpt(source: str, excerpt: str) -> str | None:
    if source_contains(source, excerpt):
        return excerpt
    # The model schema's length guard may have clipped an otherwise exact span.
    if excerpt.endswith("...") and source_contains(source, excerpt[:-3]):
        return excerpt[:-3].rstrip()
    return None
