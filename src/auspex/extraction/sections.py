"""Filing section targeting (arc42 §5.4 "Section targeting").

Removes roughly 80% of filing tokens while keeping effectively all of the
scoring/evidence signal. Operates on plain text (HTML already stripped by
the caller) using regex-located standard "Item" headings. Raw section text
is returned keyed by a stable item label so the caller can store it to Blob
at ``sections/{security_id}/{document_id}/{item}.txt``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

# Each entry: item label -> list of regex patterns that may introduce that
# section's heading in the filing text (case-insensitive, matched at line start).
SECTION_PATTERNS: dict[str, list[str]] = {
    "10-K": {
        "item_1_business": [r"item\s*1\.?\s*business"],
        "item_1a_risk_factors": [r"item\s*1a\.?\s*risk\s*factors"],
        "item_7_mda": [r"item\s*7\.?\s*management.?s\s*discussion"],
        "item_7a_market_risk": [r"item\s*7a\.?\s*quantitative\s*and\s*qualitative"],
    },
    "10-Q": {
        "mda": [r"item\s*2\.?\s*management.?s\s*discussion", r"management.?s\s*discussion\s*and\s*analysis"],
        "results_of_operations": [r"results\s*of\s*operations"],
        "item_1a_updates": [r"item\s*1a\.?\s*risk\s*factors"],
    },
    "20-F": {
        "item_3d_risk_factors": [r"item\s*3\s*\.?\s*d\.?\s*risk\s*factors", r"d\.\s*risk\s*factors"],
        "item_4_business": [r"item\s*4\.?\s*information\s*on\s*the\s*company"],
        "item_5_operating_financial_review": [r"item\s*5\.?\s*operating\s*and\s*financial\s*review"],
    },
    "S-1": {
        "business": [r"^\s*business\s*$"],
        "risk_factors": [r"risk\s*factors"],
        "use_of_proceeds": [r"use\s*of\s*proceeds"],
    },
}

# 8-K and 6-K submit the entire document — no section targeting applied.
WHOLE_DOCUMENT_FORMS = frozenset({"8-K", "6-K"})
MAX_EXTRACTION_CHARS = 300_000
_BLOCK_TAGS = frozenset(
    {
        "article",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "p",
        "section",
        "table",
        "td",
        "th",
        "tr",
    }
)


@dataclass(frozen=True)
class Section:
    item: str
    text: str


class _FilingHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        del attrs
        if tag in {"script", "style"}:
            self.ignored_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def filing_html_to_text(value: str) -> str:
    if not re.search(r"<[a-zA-Z][^>]*>", value):
        return value
    parser = _FilingHtmlParser()
    parser.feed(value)
    lines = [
        re.sub(r"[^\S\r\n]+", " ", line).strip()
        for line in "".join(parser.parts).splitlines()
    ]
    return "\n".join(line for line in lines if line)


def bound_sections(sections: list[Section], max_chars: int = MAX_EXTRACTION_CHARS) -> list[Section]:
    remaining = max_chars
    bounded = []
    for section in sections:
        if remaining <= 0:
            break
        text = section.text[:remaining]
        if text:
            bounded.append(Section(item=section.item, text=text))
            remaining -= len(text)
    return bounded


def _find_heading_positions(text: str, patterns: dict[str, list[str]]) -> list[tuple[int, str]]:
    positions: list[tuple[int, str]] = []
    for item, pats in patterns.items():
        for pat in pats:
            for m in re.finditer(pat, text, flags=re.IGNORECASE | re.MULTILINE):
                positions.append((m.start(), item))
    positions.sort(key=lambda p: p[0])
    return positions


def target_sections(form_type: str, text: str) -> list[Section]:
    """Extract the targeted sections for ``form_type`` from plain-text filing content.

    Returns an empty list if ``form_type`` is not one of the five forms that
    receive section targeting (8-K/6-K submit the whole document — the
    caller should treat that case separately via :data:`WHOLE_DOCUMENT_FORMS`).
    """

    patterns = SECTION_PATTERNS.get(form_type)
    if not patterns:
        return []
    text = filing_html_to_text(text)

    all_headings = _find_heading_positions(text, patterns)
    if not all_headings:
        return []

    # Also detect *any* "Item N" heading so an unrelated section correctly
    # terminates the previous targeted section, even if it isn't itself targeted.
    generic_item_positions = [
        m.start() for m in re.finditer(r"^\s*item\s*\d+[a-z]?\.?", text, flags=re.IGNORECASE | re.MULTILINE)
    ]
    boundary_positions = sorted(set(p for p, _ in all_headings) | set(generic_item_positions) | {len(text)})

    candidates: dict[str, list[tuple[int, Section]]] = {}
    for start, item in all_headings:
        end_candidates = [b for b in boundary_positions if b > start]
        end = min(end_candidates) if end_candidates else len(text)
        section = Section(item=item, text=text[start:end].strip())
        candidates.setdefault(item, []).append((start, section))

    # Inline filing tables of contents often repeat every target heading before
    # the substantive section. The longest bounded occurrence reliably rejects
    # those one-line references without issuer-specific rules.
    selected = [
        max(item_candidates, key=lambda candidate: len(candidate[1].text))
        for item_candidates in candidates.values()
    ]
    return [section for _, section in sorted(selected, key=lambda candidate: candidate[0])]
