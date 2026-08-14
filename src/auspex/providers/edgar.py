"""SEC EDGAR client (arc42 §3.1, §5.3).

No API key required; a descriptive ``User-Agent`` is mandatory and calls are
rate-limited at 8 req/s (below EDGAR's 10 req/s ceiling) with exponential
backoff on HTTP 429.
"""

from __future__ import annotations

from datetime import date

import httpx

from auspex.providers.rate_limit import TokenBucket, backoff_sleep

MAX_RETRIES = 5


def latest_accession_for_forms(
    submissions: dict,
    forms: set[str] | frozenset[str],
    *,
    filed_before: date | None = None,
) -> str | None:
    """Latest accession for ``forms``, optionally strictly before a cutoff."""

    recent = submissions.get("filings", {}).get("recent", {})
    candidates = [
        accession
        for form, accession, filed in zip(
            recent.get("form", []),
            recent.get("accessionNumber", []),
            recent.get("filingDate", []),
            strict=False,
        )
        if form in forms
        and (filed_before is None or date.fromisoformat(filed) < filed_before)
    ]
    return max(candidates, default=None)


def latest_filing_date_for_forms(
    submissions: dict,
    forms: set[str] | frozenset[str],
    *,
    filed_before: date | None = None,
) -> date | None:
    recent = submissions.get("filings", {}).get("recent", {})
    candidates = [
        date.fromisoformat(filed)
        for form, filed in zip(
            recent.get("form", []),
            recent.get("filingDate", []),
            strict=False,
        )
        if form in forms
        and (filed_before is None or date.fromisoformat(filed) < filed_before)
    ]
    return max(candidates, default=None)


class EdgarClient:
    def __init__(
        self,
        *,
        base_url: str,
        www_base_url: str,
        user_agent: str,
        rate_limit_per_second: float = 8.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._www_base_url = www_base_url.rstrip("/")
        self._headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        self._bucket = TokenBucket(rate_limit_per_second)
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get(self, url: str) -> httpx.Response:
        for attempt in range(MAX_RETRIES):
            await self._bucket.acquire()
            response = await self._client.get(url, headers=self._headers)
            if response.status_code == 429:
                await backoff_sleep(attempt)
                continue
            response.raise_for_status()
            return response
        response.raise_for_status()
        return response

    async def get_company_tickers(self) -> dict:
        response = await self._get(f"{self._www_base_url}/files/company_tickers.json")
        return response.json()

    async def get_submissions(self, cik: str) -> dict:
        """Filing index for a CIK, including `formType` history used to verify
        `filer_profile` at bootstrap (arc42 §5.2, §6.3 step 2)."""

        response = await self._get(f"{self._base_url}/submissions/CIK{cik}.json")
        return response.json()

    async def get_company_facts(self, cik: str) -> dict:
        """XBRL companyfacts — point-in-time `filed` dates on every fact."""

        response = await self._get(f"{self._base_url}/api/xbrl/companyfacts/CIK{cik}.json")
        return response.json()

    async def get_filing_document(self, cik: str, accession_no_dashes: str, filename: str) -> str:
        """Fetch a specific filing document (accession without dashes, e.g. 000104581026000001)."""

        url = f"{self._www_base_url}/Archives/edgar/data/{int(cik)}/{accession_no_dashes}/{filename}"
        response = await self._get(url)
        return response.text

    async def get_form4_xml(self, cik: str, accession_no_dashes: str, filename: str) -> str:
        # SEC submissions often expose an XSL-rendered path such as
        # `xslF345X06/ownership.xml`. That URL returns HTML, not parseable XML.
        # The raw ownership document is stored beside it under the basename.
        raw_filename = filename.rsplit("/", 1)[-1]
        return await self.get_filing_document(cik, accession_no_dashes, raw_filename)
