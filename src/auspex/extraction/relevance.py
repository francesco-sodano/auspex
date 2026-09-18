"""Deterministic issuer matching for provider-supplied news."""

from __future__ import annotations

import re

from auspex.models.document import Document
from auspex.models.security import Security

_AMBIGUOUS_TICKERS = frozenset({"AI", "APP", "ARM", "BE", "FORM", "META", "NOW", "ON", "ONTO", "PATH", "SNOW"})
_COMPANY_SUFFIXES = r"\b(incorporated|inc|corporation|corp|limited|ltd|plc|holdings|holding|company|co)\b\.?"


def news_is_relevant(document: Document, security: Security) -> bool:
    title = document.title or ""
    ticker = re.escape(security.ticker)
    if security.ticker in _AMBIGUOUS_TICKERS:
        ticker_pattern = rf"(?:\$|\b(?:NASDAQ|NYSE)\s*:\s*){ticker}\b"
    else:
        ticker_pattern = rf"\b{ticker}\b"
    if re.search(ticker_pattern, title, flags=re.IGNORECASE):
        return True
    company = re.sub(_COMPANY_SUFFIXES, "", security.name, flags=re.IGNORECASE)
    company = re.sub(r"[^a-z0-9]+", " ", company.lower()).strip()
    normalized_title = re.sub(r"[^a-z0-9]+", " ", title.lower())
    return bool(company and re.search(rf"\b{re.escape(company)}\b", normalized_title))
