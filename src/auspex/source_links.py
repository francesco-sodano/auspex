"""Canonical links to stored source evidence, without guessing filing filenames."""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

from auspex.models.document import Document
from auspex.models.security import Security

logger = logging.getLogger(__name__)
_ACCESSION = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}")


def sec_filing_url(cik: str, accession: str | None) -> str | None:
    if (
        not cik.isascii()
        or not cik.isdigit()
        or not 1 <= len(cik) <= 10
        or int(cik) <= 0
        or accession is None
        or _ACCESSION.fullmatch(accession) is None
    ):
        logger.warning("SEC source link unavailable: invalid or missing filing coordinates")
        return None
    return (
        "https://www.sec.gov/Archives/edgar/data/"
        f"{int(cik)}/{accession.replace('-', '')}/{accession}-index.html"
    )


def document_source_url(document: Document, security: Security | None) -> str | None:
    if security is not None and document.security_id != security.id:
        raise ValueError("The source document does not belong to this issuer.")
    if document.url:
        try:
            parsed = urlsplit(document.url)
            if parsed.scheme.lower() in {"http", "https"} and parsed.hostname and not any(
                character.isspace() for character in document.url
            ):
                return document.url
        except ValueError:
            logger.warning("Could not parse the source URL for document %s", document.id)
        logger.warning("Ignored an invalid source URL for document %s", document.id)
    if document.source == "edgar" and security is not None:
        return sec_filing_url(security.cik, document.accession_number)
    return None
