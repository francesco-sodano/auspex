"""Classify unclassified portfolio securities from SEC 10-K/20-F business text."""

import hashlib
from html.parser import HTMLParser
import os
import re
from datetime import date, datetime, timezone

from azure.identity import DefaultAzureCredential

from search.clients import AzureOpenAIChat
from search.theme_classification import ThemeClassificationService
from shared.base_connector import BaseConnector
from shared.models import Batch, Watermark
from shared.retry import http_get


_SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_SEC_ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
_THEMES = {
    "ai_compute_semiconductors": "AI Compute & Semiconductors",
    "data_center_buildout": "Data Center Buildout",
    "energy_security_producers": "Energy Security & Producers",
    "enterprise_technology": "Enterprise Technology",
    "healthcare": "Healthcare",
    "quantum_computing": "Quantum Computing",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"script", "style"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data):
        if not self._ignored_depth:
            self.parts.append(data)


def extract_business_section(document: str, filing_type: str) -> str:
    parser = _TextExtractor()
    parser.feed(document)
    text = " ".join(" ".join(parser.parts).split())
    patterns = (
        (
            r"(?i)\bitem\s+1[\.:\-\s]+business\b",
            r"(?i)\bitem\s+1a[\.:\-\s]+risk\s+factors\b",
        ),
        (
            r"(?i)\bitem\s+4[\.:\-\s]+information\s+on\s+the\s+company\b",
            r"(?i)\bitem\s+5[\.:\-\s]+operating\s+and\s+financial\s+review\b",
        ),
    )
    for start_pattern, end_pattern in patterns:
        starts = list(re.finditer(start_pattern, text))
        for start in starts:
            end = re.search(end_pattern, text[start.end():])
            if end and end.start() >= 500:
                section = text[start.end():start.end() + end.start()].strip()
                if len(section) >= 200:
                    return section[:30000]
    raise ValueError(f"Could not extract business section from {filing_type}")


class ThemeClassifierConnector(BaseConnector):
    source_id = "theme_classifier"
    schema_version = 1

    def __init__(self, cp, bw, source_config=None, chat=None) -> None:
        super().__init__(cp, bw, source_config=source_config)
        self._user_agent = os.environ["EDGAR_USER_AGENT"]
        self._headers = {"User-Agent": self._user_agent, "Accept": "application/json"}
        if chat is None:
            endpoint = os.environ["AZURE_OPENAI_ENDPOINT"]
            deployment = os.environ.get("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4o")
            chat = AzureOpenAIChat(endpoint, deployment, credential=DefaultAzureCredential())
        self._classifier = ThemeClassificationService(chat, _THEMES)

    def fetch(self, since: Watermark | None) -> Batch:
        today = date.today().isoformat()
        securities = []
        for ticker in self._bw.read_portfolio_universe():
            security = self._cp.get_security_by_ticker(ticker)
            if security is None:
                continue
            classification_id = f"classification:security:{int(security['security_sk'])}"
            if self._cp.get_market_data(classification_id) is not None:
                continue
            securities.append(security)

        records = []
        ticker_map = self._ticker_map() if securities else {}
        for security in securities:
            ticker = str(security["ticker"]).upper()
            classified_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                cik = ticker_map.get(ticker)
                if not cik:
                    raise ValueError("SEC CIK was not found")
                filing = self._latest_annual_filing(cik)
                if filing is None:
                    raise ValueError("Annual filing was not found")
                business_description = self._filing_business_description(cik, filing)
                result = self._classifier.classify(
                    ticker=ticker,
                    company_name=str(security["company_name"]),
                    filing_type=filing["form"],
                    business_description=business_description,
                )
                quote = self._cp.get_market_data(f"quote:security:{int(security['security_sk'])}") or {}
                records.append({
                    "classification_status": "classified",
                    "classification_id": hashlib.sha256(
                        f"llm_v1|{security['security_sk']}|{result.theme_id}|{filing['accessionNumber']}".encode("utf-8")
                    ).hexdigest(),
                    "security_sk": int(security["security_sk"]),
                    "ticker": ticker,
                    "company_name": str(security["company_name"]),
                    "theme_id": result.theme_id,
                    "provenance": result.provenance,
                    "confidence": result.confidence,
                    "rationale": result.rationale,
                    "effective_from": str(quote.get("as_of") or today),
                    "classification_version": "llm_v1",
                    "model_version": os.environ.get("AZURE_OPENAI_CHAT_MODEL_VERSION", "gpt-4o:2024-11-20"),
                    "filing_type": filing["form"],
                    "filing_accession": filing["accessionNumber"],
                    "filing_date": filing["filingDate"],
                    "description_sha256": hashlib.sha256(business_description.encode("utf-8")).hexdigest(),
                    "classified_at": classified_at,
                })
            except Exception as exc:
                records.append({
                    "classification_status": "withheld",
                    "security_sk": int(security["security_sk"]),
                    "ticker": ticker,
                    "company_name": str(security["company_name"]),
                    "reason": " ".join(str(exc).split())[:500],
                    "classification_version": "llm_v1",
                    "classified_at": classified_at,
                })
        return Batch(
            records=records,
            new_wm=Watermark(source_id=self.source_id, last_event_ts=today, last_cursor=today),
            window=today,
            partition_date=today,
            watermark_from=since.last_event_ts if since else None,
        )

    def after_bronze_write(self, batch: Batch) -> None:
        for record in batch.records:
            if record.get("classification_status") != "classified":
                continue
            self._cp.upsert_market_data({
                "id": f"classification:security:{record['security_sk']}",
                "kind": "theme_classification",
                "security_sk": record["security_sk"],
                "ticker": record["ticker"],
                "theme_id": record["theme_id"],
                "provenance": record["provenance"],
                "confidence": str(record["confidence"]),
                "rationale": record["rationale"],
                "classification_version": record["classification_version"],
                "as_of": record["effective_from"],
                "source_id": self.source_id,
            })

    def _ticker_map(self) -> dict[str, str]:
        payload = http_get(_SEC_TICKERS_URL, headers=self._headers).json()
        return {
            str(row["ticker"]).upper(): str(row["cik_str"]).zfill(10)
            for row in payload.values()
        }

    def _latest_annual_filing(self, cik: str) -> dict | None:
        payload = http_get(
            _SEC_SUBMISSIONS_URL.format(cik=cik),
            headers=self._headers,
        ).json()
        recent = (payload.get("filings") or {}).get("recent") or {}
        keys = ("form", "accessionNumber", "primaryDocument", "filingDate")
        rows = [dict(zip(keys, values)) for values in zip(*(recent.get(key) or [] for key in keys))]
        return next((row for row in rows if row["form"] in {"10-K", "20-F"}), None)

    def _filing_business_description(self, cik: str, filing: dict) -> str:
        response = http_get(
            _SEC_ARCHIVE_URL.format(
                cik=int(cik),
                accession=filing["accessionNumber"].replace("-", ""),
                document=filing["primaryDocument"],
            ),
            headers=self._headers,
            max_response_bytes=8 * 1024 * 1024,
        )
        return extract_business_section(response.text, filing["form"])