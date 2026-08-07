"""SEC N-PORT connector for point-in-time thematic ETF holdings."""
import hashlib
import json
import os
import re
import threading
import time
from datetime import date, timedelta
from typing import Optional
from urllib.parse import quote
import xml.etree.ElementTree as ET

from shared.base_connector import BaseConnector
from shared.models import Batch, Watermark
from shared.retry import http_get


_SUBMISSIONS_ROOT = "https://data.sec.gov/submissions"
_ARCHIVES_ROOT = "https://www.sec.gov/Archives/edgar/data"
_FORMS = frozenset({"NPORT-P", "NPORT-P/A"})
_DEFAULT_REQUESTS_PER_MINUTE = 60
_MAX_REQUESTS_PER_MINUTE = 60
_MAX_ATTEMPTS = 6
_TIMEOUT_SECONDS = 60.0
_DEFAULT_MAX_PRIMARY_XML_BYTES = 64 * 1024 * 1024
_HISTORICAL_FILE_RE = re.compile(r"CIK\d{10}-submissions-\d{3}\.json")
_REQUEST_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0


def _pace_request(min_interval_s: float) -> None:
    global _NEXT_REQUEST_AT
    with _REQUEST_LOCK:
        now = time.monotonic()
        request_at = max(now, _NEXT_REQUEST_AT)
        _NEXT_REQUEST_AT = request_at + min_interval_s
    delay = request_at - now
    if delay > 0:
        time.sleep(delay)


class SecNportConnector(BaseConnector):
    source_id = "sec_nport"
    schema_version = 1

    def __init__(
        self,
        cp,
        bw,
        etf_series: list[dict] = None,
        source_config: Optional[dict] = None,
        since_date: str = None,
        to_date: str = None,
        filing_offset: int = None,
        filing_limit: int = None,
    ) -> None:
        super().__init__(cp, bw, source_config=source_config)
        config = source_config or {}
        self._etf_series = self._normalize_mappings(
            etf_series if etf_series is not None else config.get("etf_series")
        )
        self._since_date = since_date or config.get("since_date")
        self._to_date = to_date or config.get("to_date")
        effective_offset = filing_offset if filing_offset is not None else config.get("filing_offset")
        self._filing_offset = max(0, int(effective_offset or 0))
        effective_limit = filing_limit if filing_limit is not None else config.get("filing_limit")
        self._filing_limit = max(1, int(effective_limit)) if effective_limit is not None else None
        self._user_agent = os.environ["EDGAR_USER_AGENT"]
        configured_rpm = self._requests_per_minute(_DEFAULT_REQUESTS_PER_MINUTE)
        max_rpm = int(os.environ.get("SEC_NPORT_MAX_RPM", str(_MAX_REQUESTS_PER_MINUTE)))
        self._min_interval_s = 60 / min(configured_rpm, max_rpm)
        self._before_request = lambda: _pace_request(self._min_interval_s)
        self._max_primary_xml_bytes = int(
            config.get("max_primary_xml_bytes") or _DEFAULT_MAX_PRIMARY_XML_BYTES
        )

    def fetch(self, since: Optional[Watermark]) -> Batch:
        start_date, end_date = self._date_range(since)
        mapping_fingerprint = hashlib.sha256(
            json.dumps(self._etf_series, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        limit_token = str(self._filing_limit) if self._filing_limit is not None else "all"
        window = (
            f"{start_date}-to-{end_date}-series-{mapping_fingerprint}"
            f"-offset-{self._filing_offset}-limit-{limit_token}"
        )
        if start_date > end_date:
            return Batch(
                records=[],
                new_wm=Watermark(
                    source_id=self.source_id,
                    last_event_ts=end_date,
                    last_cursor=end_date,
                ),
                window=window,
                partition_date=end_date,
                watermark_from=start_date,
                has_more=False,
            )

        headers = {
            "User-Agent": self._user_agent,
            "Accept": "application/json",
        }
        mappings_by_cik = self._mappings_by_cik()
        filings = []
        for cik in sorted(mappings_by_cik):
            filings.extend(self._filings_for_cik(cik, start_date, end_date, headers))
        filings = self._dedupe_filings(filings)

        total_filings = len(filings)
        if self._filing_limit is None:
            selected_filings = filings[self._filing_offset:]
            has_more = False
        else:
            page_end = self._filing_offset + self._filing_limit
            selected_filings = filings[self._filing_offset:page_end]
            has_more = page_end < total_filings

        records = []
        xml_headers = {
            "User-Agent": self._user_agent,
            "Accept": "application/xml,text/xml;q=0.9,text/plain;q=0.8",
            "Accept-Encoding": "identity",
        }
        for filing in selected_filings:
            primary_url = self._primary_document_url(filing)
            primary_xml = self._fetch_primary_xml(primary_url, xml_headers)
            root = ET.fromstring(primary_xml)
            mappings = mappings_by_cik[filing["cik"]]
            filed_series_ids = self._series_ids(root)
            matched = [mapping for mapping in mappings if mapping["series_id"] in filed_series_ids]
            if not filed_series_ids and len(mappings) == 1:
                matched = mappings
            if not matched:
                records.append(self._record(
                    filing,
                    None,
                    root,
                    primary_url,
                    primary_xml,
                    status="unmatched_series",
                    filed_series_ids=filed_series_ids,
                ))
                continue
            if len(matched) != 1:
                raise RuntimeError(
                    f"N-PORT filing {filing['accession_no']} matched multiple configured series"
                )
            records.append(self._record(
                filing,
                matched[0],
                root,
                primary_url,
                primary_xml,
                status="matched",
                filed_series_ids=filed_series_ids,
            ))

        records.sort(
            key=lambda record: (
                record["filing_date"],
                record["acceptance_datetime"],
                record["cik"],
                record["accession_no"],
            )
        )
        return Batch(
            records=records,
            new_wm=Watermark(
                source_id=self.source_id,
                last_event_ts=end_date,
                last_cursor=end_date,
            ),
            window=window,
            partition_date=end_date,
            watermark_from=start_date,
            has_more=has_more,
        )

    def _date_range(self, since: Optional[Watermark]) -> tuple[str, str]:
        if self._since_date:
            start = date.fromisoformat(self._since_date)
        elif since and since.last_cursor:
            start = date.fromisoformat(since.last_cursor[:10]) + timedelta(days=1)
        elif since and since.last_event_ts:
            start = date.fromisoformat(since.last_event_ts[:10]) + timedelta(days=1)
        else:
            raise ValueError("sec_nport requires since_date or an existing watermark")

        end = date.fromisoformat(self._to_date) if self._to_date else date.today()
        if end > date.today():
            raise ValueError("sec_nport to_date cannot be in the future")
        return start.isoformat(), end.isoformat()

    def _normalize_mappings(self, mappings: object) -> list[dict]:
        if not isinstance(mappings, list) or not mappings:
            raise ValueError("sec_nport requires non-empty etf_series mappings")
        normalized = []
        seen = set()
        for mapping in mappings:
            if not isinstance(mapping, dict):
                raise ValueError("each sec_nport etf_series mapping must be an object")
            missing = [
                field for field in ("symbol", "cik", "series_id", "class_id")
                if not str(mapping.get(field) or "").strip()
            ]
            if missing:
                raise ValueError(
                    "sec_nport etf_series mapping is missing: " + ",".join(missing)
                )
            cik_digits = re.sub(r"[^0-9]", "", str(mapping["cik"]))
            if not cik_digits or len(cik_digits) > 10:
                raise ValueError(f"invalid sec_nport CIK: {mapping['cik']}")
            item = {
                "symbol": str(mapping["symbol"]).strip().upper(),
                "cik": cik_digits.zfill(10),
                "series_id": str(mapping["series_id"]).strip(),
                "class_id": str(mapping["class_id"]).strip(),
            }
            key = (item["cik"], item["series_id"])
            if key in seen:
                raise ValueError(
                    f"duplicate sec_nport etf_series mapping for {item['cik']} {item['series_id']}"
                )
            seen.add(key)
            normalized.append(item)
        return sorted(
            normalized,
            key=lambda item: (item["cik"], item["series_id"], item["class_id"], item["symbol"]),
        )

    def _mappings_by_cik(self) -> dict[str, list[dict]]:
        grouped: dict[str, list[dict]] = {}
        for mapping in self._etf_series:
            grouped.setdefault(mapping["cik"], []).append(mapping)
        return grouped

    def _filings_for_cik(
        self,
        cik: str,
        start_date: str,
        end_date: str,
        headers: dict,
    ) -> list[dict]:
        root_name = f"CIK{cik}.json"
        root = self._get_json(f"{_SUBMISSIONS_ROOT}/{root_name}", headers)
        filings = self._submission_rows(
            (root.get("filings") or {}).get("recent") or {}, cik, root_name
        )
        for descriptor in (root.get("filings") or {}).get("files") or []:
            if not self._descriptor_overlaps(descriptor, start_date, end_date):
                continue
            name = str(descriptor.get("name") or "")
            if not _HISTORICAL_FILE_RE.fullmatch(name):
                raise RuntimeError(f"invalid SEC historical submissions filename: {name}")
            history = self._get_json(f"{_SUBMISSIONS_ROOT}/{name}", headers)
            filings.extend(self._submission_rows(history, cik, name))
        return [
            filing for filing in filings
            if filing["form"] in _FORMS
            and start_date <= filing["filing_date"] <= end_date
        ]

    def _get_json(self, url: str, headers: dict) -> dict:
        response = http_get(
            url,
            headers=headers,
            max_attempts=_MAX_ATTEMPTS,
            timeout=_TIMEOUT_SECONDS,
            before_attempt=self._before_request,
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"SEC submissions response is not an object: {url}")
        return payload

    def _submission_rows(self, columns: dict, cik: str, source_file: str) -> list[dict]:
        accessions = columns.get("accessionNumber") or []
        rows = []
        for index, accession in enumerate(accessions):
            accession_no = str(accession or "").strip()
            if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession_no):
                continue
            rows.append({
                "cik": cik,
                "form": self._column(columns, "form", index),
                "report_date": self._column(columns, "reportDate", index),
                "filing_date": self._column(columns, "filingDate", index),
                "acceptance_datetime": self._column(columns, "acceptanceDateTime", index),
                "accession_no": accession_no,
                "primary_document": self._column(columns, "primaryDocument", index),
                "submissions_file": source_file,
            })
        return rows

    def _column(self, columns: dict, name: str, index: int) -> str:
        values = columns.get(name) or []
        return str(values[index] or "").strip() if index < len(values) else ""

    def _descriptor_overlaps(self, descriptor: dict, start_date: str, end_date: str) -> bool:
        filing_from = str(descriptor.get("filingFrom") or "")[:10]
        filing_to = str(descriptor.get("filingTo") or "")[:10]
        if not filing_from or not filing_to:
            return True
        return filing_from <= end_date and filing_to >= start_date

    def _dedupe_filings(self, filings: list[dict]) -> list[dict]:
        deduped = {}
        for filing in filings:
            required = (
                "form",
                "report_date",
                "filing_date",
                "acceptance_datetime",
                "accession_no",
                "primary_document",
            )
            missing = [field for field in required if not filing.get(field)]
            if missing and filing.get("form") in _FORMS:
                raise RuntimeError(
                    f"SEC N-PORT filing {filing.get('accession_no')} is missing: {','.join(missing)}"
                )
            key = (filing["cik"], filing["accession_no"])
            existing = deduped.get(key)
            if existing is not None and existing != filing:
                comparable_fields = required + ("cik",)
                if any(existing.get(field) != filing.get(field) for field in comparable_fields):
                    raise RuntimeError(
                        f"conflicting SEC submissions metadata for {filing['accession_no']}"
                    )
                if existing["submissions_file"] > filing["submissions_file"]:
                    deduped[key] = filing
            else:
                deduped[key] = filing
        return sorted(
            deduped.values(),
            key=lambda filing: (
                filing["filing_date"],
                filing["acceptance_datetime"],
                filing["cik"],
                filing["accession_no"],
            ),
        )

    def _primary_document_url(self, filing: dict) -> str:
        path = str(filing["primary_document"]).strip().replace("\\", "/").lstrip("/")
        segments = path.split("/")
        if segments and segments[0].lower().startswith("xslform"):
            segments = segments[1:]
        if not segments or any(segment in {"", ".", ".."} for segment in segments):
            raise RuntimeError(
                f"invalid SEC primary document path for {filing['accession_no']}: {path}"
            )
        encoded_path = "/".join(quote(segment, safe="._-()") for segment in segments)
        accession_path = filing["accession_no"].replace("-", "")
        archive_cik = str(int(filing["cik"]))
        return f"{_ARCHIVES_ROOT}/{archive_cik}/{accession_path}/{encoded_path}"

    def _fetch_primary_xml(self, url: str, headers: dict) -> str:
        response = http_get(
            url,
            headers=headers,
            max_attempts=_MAX_ATTEMPTS,
            timeout=_TIMEOUT_SECONDS,
            max_response_bytes=self._max_primary_xml_bytes,
            before_attempt=self._before_request,
        )
        if bool(getattr(response, "extensions", {}).get("auspex_truncated")):
            raise RuntimeError(
                f"SEC N-PORT primary XML exceeds {self._max_primary_xml_bytes} bytes: {url}"
            )
        return response.text

    def _series_ids(self, root: ET.Element) -> set[str]:
        series_ids = set()
        for element in root.iter():
            if self._local_name(element.tag) != "seriesClassInfo":
                continue
            value = self._child_text(element, "seriesId")
            if value:
                series_ids.add(value)
        return series_ids

    def _record(
        self,
        filing: dict,
        mapping: Optional[dict],
        root: ET.Element,
        primary_url: str,
        primary_xml: str,
        status: str = "matched",
        filed_series_ids: Optional[set[str]] = None,
    ) -> dict:
        holdings = [self._holding(element) for element in root.iter() if self._local_name(element.tag) == "invstOrSec"]
        holdings.sort(
            key=lambda holding: (
                str(holding.get("cusip") or ""),
                str(holding.get("name") or ""),
                str(holding.get("title") or ""),
                json.dumps(holding.get("identifiers") or [], sort_keys=True),
            )
        )
        filed_class_ids = sorted({
            class_id
            for element in root.iter()
            if self._local_name(element.tag) == "seriesClassInfo"
            for class_id in [self._child_text(element, "classId")]
            if class_id
        })
        return {
            "status": status,
            "symbol": mapping["symbol"] if mapping else None,
            "cik": mapping["cik"] if mapping else filing["cik"],
            "series_id": mapping["series_id"] if mapping else None,
            "class_id": mapping["class_id"] if mapping else None,
            "filed_series_ids": sorted(
                filed_series_ids if filed_series_ids is not None else self._series_ids(root)
            ),
            "filed_class_ids": filed_class_ids,
            "form": filing["form"],
            "report_date": filing["report_date"],
            "event_date": filing["report_date"],
            "filing_date": filing["filing_date"],
            "acceptance_datetime": filing["acceptance_datetime"],
            "knowledge_date": filing["acceptance_datetime"],
            "accession_no": filing["accession_no"],
            "primary_document": filing["primary_document"],
            "primary_document_url": primary_url,
            "submissions_file": filing["submissions_file"],
            "holdings": holdings,
            "primary_xml": primary_xml,
        }

    def _holding(self, element: ET.Element) -> dict:
        holding = {
            "name": self._child_text(element, "name"),
            "title": self._child_text(element, "title"),
            "cusip": self._child_text(element, "cusip"),
            "identifiers": self._holding_identifiers(element),
            "balance": self._child_text(element, "balance"),
            "units": self._child_text(element, "units"),
            "currency": self._child_text(element, "curCd"),
            "value_usd": self._child_text(element, "valUSD"),
            "percentage": self._child_text(element, "pctVal"),
        }
        optional_fields = {
            "lei": "lei",
            "payoff_profile": "payoffProfile",
            "asset_category": "assetCat",
            "issuer_category": "issuerCat",
            "investment_country": "invCountry",
            "is_restricted_security": "isRestrictedSec",
            "fair_value_level": "fairValLevel",
        }
        for output_name, xml_name in optional_fields.items():
            value = self._child_text(element, xml_name)
            if value is not None:
                holding[output_name] = value
        return holding

    def _holding_identifiers(self, element: ET.Element) -> list[dict]:
        identifiers_element = self._child(element, "identifiers")
        if identifiers_element is None:
            return []
        identifiers = []
        for identifier in identifiers_element:
            value = str(identifier.attrib.get("value") or identifier.text or "").strip()
            if not value:
                continue
            item = {
                "type": self._local_name(identifier.tag),
                "value": value,
            }
            description = str(identifier.attrib.get("desc") or "").strip()
            if description:
                item["description"] = description
            identifiers.append(item)
        return sorted(
            identifiers,
            key=lambda item: (item["type"], item.get("description", ""), item["value"]),
        )

    def _child(self, element: ET.Element, name: str) -> Optional[ET.Element]:
        return next(
            (child for child in element if self._local_name(child.tag) == name),
            None,
        )

    def _child_text(self, element: ET.Element, name: str) -> Optional[str]:
        child = self._child(element, name)
        if child is None or child.text is None:
            return None
        value = child.text.strip()
        return value or None

    def _local_name(self, tag: str) -> str:
        return tag.rsplit("}", 1)[-1]