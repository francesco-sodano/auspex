"""Generic SEC EFTS search connector for filing feeds."""
import html
import hashlib
import os
import re
import threading
import time
from datetime import date, timedelta
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse
import xml.etree.ElementTree as ET

from .base_connector import BaseConnector
from .models import Batch, Watermark
from .retry import http_get

_EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
_BROWSE_EDGAR_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
_PAGE_SIZE = 100
_DEFAULT_LOOKBACK_DAYS = 7
_DEFAULT_REQUESTS_PER_MINUTE = 60
_WINDOW_DAYS = 1
_SEC_MAX_ATTEMPTS = 6
_SEC_TIMEOUT_SECONDS = 60.0
_SEC_MAX_REQUESTS_PER_MINUTE = 60
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
_SEC_ARCHIVES_HOSTS = {"sec.gov", "www.sec.gov"}
_INDEX_MAX_BYTES = 512 * 1024
_DOCUMENT_MAX_BYTES = 4 * 1024 * 1024
_SUBMISSION_HEADER_MAX_BYTES = 128 * 1024
_MISSING_DOCUMENT_TERMINAL_AGE_DAYS = 30
_MAX_INDEX_DOCUMENTS = 100
_TEXT_DOCUMENT_EXTENSIONS = {".htm", ".html", ".txt", ".xml", ".xhtml"}
_BINARY_DOCUMENT_EXTENSIONS = {
    ".avi", ".bmp", ".doc", ".docx", ".gif", ".jpeg", ".jpg", ".mp3",
    ".mp4", ".pdf", ".png", ".ppt", ".pptx", ".tif", ".tiff", ".xls",
    ".xlsx", ".zip",
}
_SEC_REQUEST_LOCK = threading.Lock()
_SEC_NEXT_REQUEST_AT = 0.0


def _pace_sec_request(min_interval_s: float) -> None:
    global _SEC_NEXT_REQUEST_AT
    with _SEC_REQUEST_LOCK:
        now = time.monotonic()
        request_at = max(now, _SEC_NEXT_REQUEST_AT)
        _SEC_NEXT_REQUEST_AT = request_at + min_interval_s
    delay = request_at - now
    if delay > 0:
        time.sleep(delay)


class _DocumentTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []
        self._in_document_table = False
        self._table_depth = 0
        self._row: list[dict] | None = None
        self._cell: dict | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attributes = dict(attrs)
        if tag == "table":
            if self._in_document_table:
                self._table_depth += 1
            elif "document format files" in (attributes.get("summary") or "").lower():
                self._in_document_table = True
                self._table_depth = 1
            return
        if not self._in_document_table:
            return
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = {"text": [], "href": None, "header": tag == "th"}
        elif tag == "a" and self._cell is not None:
            self._cell["href"] = attributes.get("href")

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._in_document_table:
            return
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._cell["text"] = " ".join("".join(self._cell["text"]).split())
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row and not all(cell["header"] for cell in self._row):
                values = self._row + [{"text": "", "href": None, "header": False}] * (5 - len(self._row))
                self.rows.append({
                    "sequence": values[0]["text"],
                    "description": values[1]["text"],
                    "name": values[2]["text"],
                    "href": values[2]["href"],
                    "document_type": values[3]["text"],
                    "size": values[4]["text"],
                })
            self._row = None
        elif tag == "table":
            self._table_depth -= 1
            if self._table_depth == 0:
                self._in_document_table = False


class SecEftsConnector(BaseConnector):
    source_id: str
    schema_version = 1
    forms: str
    archive_profile: Optional[str] = None
    window_days = _WINDOW_DAYS
    require_exhaustive_efts = False

    def __init__(
        self,
        cp,
        bw,
        source_config: Optional[dict] = None,
        since_date: str = None,
        to_date: str = None,
        filing_offset: int = 0,
        filing_limit: int = None,
        entity_ciks: list[str] = None,
        query_ciks: list[str] = None,
    ) -> None:
        super().__init__(cp, bw, source_config=source_config)
        self._user_agent = os.environ["EDGAR_USER_AGENT"]
        self._since_date = since_date
        self._to_date = to_date
        self._filing_offset = max(0, int(filing_offset or 0))
        self._filing_limit = (
            max(1, int(filing_limit))
            if self.archive_profile and filing_limit is not None
            else None
        )
        self._entity_ciks = {
            re.sub(r"[^0-9]", "", str(cik)).zfill(10)
            for cik in (entity_ciks or [])
            if re.sub(r"[^0-9]", "", str(cik))
        }
        self._query_ciks = sorted({
            re.sub(r"[^0-9]", "", str(cik)).zfill(10)
            for cik in (query_ciks or [])
            if re.sub(r"[^0-9]", "", str(cik))
        })
        self.query_total_filings = 0
        self.filtered_total_filings = 0
        self.query_audits: list[dict] = []
        configured_rpm = self._requests_per_minute(_DEFAULT_REQUESTS_PER_MINUTE)
        max_rpm = int(os.environ.get("SEC_EFTS_MAX_RPM", str(_SEC_MAX_REQUESTS_PER_MINUTE)))
        self._min_interval_s = 60 / min(configured_rpm, max_rpm)
        self._before_sec_request = lambda: _pace_sec_request(self._min_interval_s)

    def fetch(self, since: Optional[Watermark]) -> Batch:
        if self._since_date:
            start_date = self._since_date
        elif since and since.last_cursor:
            prior_cursor = date.fromisoformat(since.last_cursor[:10])
            start_date = (
                prior_cursor + timedelta(days=1) if self.archive_profile else prior_cursor
            ).isoformat()
        else:
            start_date = (date.today() - timedelta(days=_DEFAULT_LOOKBACK_DAYS)).isoformat()
        default_end = (
            date.today()
            if self._since_date or not self.archive_profile
            else date.today() - timedelta(days=1)
        )
        end_date = self._to_date or default_end.isoformat()
        headers = {"User-Agent": self._user_agent, "Accept": "application/json"}
        records = []

        for query_form, accepted_forms in self._query_plan():
            for window_start, window_end in self._date_windows(start_date, end_date):
                records.extend(self._fetch_window(query_form, accepted_forms, window_start, window_end, headers))

        records = self._dedupe_records(records)
        self.query_total_filings = len(records)
        if self._entity_ciks:
            records = [
                record for record in records
                if self._entity_ciks & {
                    re.sub(r"[^0-9]", "", str(cik)).zfill(10)
                    for cik in (record.get("ciks") or [])
                    if re.sub(r"[^0-9]", "", str(cik))
                }
            ]
        self.filtered_total_filings = len(records)
        total_filings = len(records)
        if self._filing_limit is None:
            page_end = total_filings
            has_more = False
        else:
            page_end = self._filing_offset + self._filing_limit
            records = records[self._filing_offset:page_end]
            has_more = page_end < total_filings
        if self.archive_profile:
            records = [self._enrich_filing(record, headers) for record in records]
            incomplete = [
                record.get("adsh") or record.get("accession_no")
                for record in records
                if (record.get("sec_archive") or {}).get("archive_status") == "retryable_incomplete"
            ]
            if incomplete:
                raise RuntimeError(
                    "SEC archive evidence incomplete; page not landed: "
                    + ",".join(str(accession) for accession in incomplete[:20])
                )

        new_wm = Watermark(source_id=self.source_id, last_event_ts=end_date, last_cursor=end_date)
        window = f"{start_date}-to-{end_date}-forms-{self.forms}"
        if self.archive_profile and self._filing_limit is not None:
            window += f"-offset-{self._filing_offset}-limit-{self._filing_limit}"
        return Batch(
            records=records,
            new_wm=new_wm,
            window=window,
            partition_date=end_date,
            watermark_from=start_date,
            has_more=has_more,
        )

    def _form_values(self) -> list[str]:
        return [form.strip() for form in self.forms.split(",") if form.strip()]

    def _query_plan(self) -> list[tuple[str, set[str]]]:
        planned_forms: dict[str, set[str]] = {}
        for form in self._form_values():
            query_form = self._root_form(form)
            planned_forms.setdefault(query_form, set()).add(form)
        return list(planned_forms.items())

    def _root_form(self, form: str) -> str:
        return form[:-2] if form.endswith("/A") else form

    def _date_windows(self, start_date: str, end_date: str):
        cursor = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        while cursor <= end:
            window_end = min(cursor + timedelta(days=self.window_days - 1), end)
            yield cursor.isoformat(), window_end.isoformat()
            cursor = window_end + timedelta(days=1)

    def _fetch_window(self, query_form: str, accepted_forms: set[str], start_date: str, end_date: str, headers: dict) -> list[dict]:
        records = []
        offset = 0
        total_hits = None
        fetched_hits = 0
        hit_identities: list[str] = []

        while True:
            try:
                resp = http_get(
                    _EFTS_URL,
                    params={
                        "forms": query_form,
                        "startdt": start_date,
                        "enddt": end_date,
                        "from": offset,
                        "size": _PAGE_SIZE,
                        **({"ciks": ",".join(self._query_ciks)} if self._query_ciks else {}),
                    },
                    headers=headers,
                    max_attempts=_SEC_MAX_ATTEMPTS,
                    timeout=_SEC_TIMEOUT_SECONDS,
                    before_attempt=self._before_sec_request,
                )
            except Exception as exc:
                if self.require_exhaustive_efts:
                    raise RuntimeError(
                        f"SEC EFTS exhaustive query failed for {query_form} {start_date}..{end_date}"
                    ) from exc
                fallback_records = self._fetch_browse_edgar(accepted_forms, start_date, end_date, headers, exc)
                return records + fallback_records
            payload = resp.json()
            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("hits"), dict)
                or not isinstance((payload.get("hits") or {}).get("hits"), list)
                or "error" in payload
                or "message" in payload
            ):
                if self.require_exhaustive_efts:
                    raise RuntimeError(
                        f"SEC EFTS exhaustive query returned an invalid payload for "
                        f"{query_form} {start_date}..{end_date}"
                    )
                fallback_records = self._fetch_browse_edgar(
                    accepted_forms,
                    start_date,
                    end_date,
                    headers,
                    RuntimeError(f"SEC EFTS returned error payload for {query_form} {start_date}..{end_date}: {payload}"),
                )
                return records + fallback_records
            hits = payload["hits"]["hits"]
            total_value = payload["hits"].get("total")
            page_total = (
                total_value.get("value") if isinstance(total_value, dict) else total_value
            )
            total_relation = (
                total_value.get("relation") if isinstance(total_value, dict) else "eq"
            )
            if self.require_exhaustive_efts and total_relation != "eq":
                raise RuntimeError(
                    f"SEC EFTS total is not exact for {query_form} {start_date}..{end_date}: "
                    f"relation={total_relation}"
                )
            if not isinstance(page_total, int) or page_total < 0:
                if self.require_exhaustive_efts:
                    raise RuntimeError(
                        f"SEC EFTS query has no valid total for {query_form} "
                        f"{start_date}..{end_date}"
                    )
                page_total = offset + len(hits)
            if total_hits is None:
                total_hits = page_total
            elif total_hits != page_total:
                raise RuntimeError(
                    f"SEC EFTS total changed during pagination for {query_form} "
                    f"{start_date}..{end_date}"
                )
            fetched_hits += len(hits)
            for hit in hits:
                source = hit.get("_source", {})
                hit_identities.append(str(
                    source.get("adsh") or source.get("accession_no") or ""
                ))
                record_form = source.get("form") or source.get("file_type") or query_form
                if record_form in accepted_forms:
                    records.append(source)
            if self.require_exhaustive_efts:
                if fetched_hits > (total_hits or 0):
                    raise RuntimeError(
                        f"SEC EFTS pagination overflow for {query_form} {start_date}..{end_date}"
                    )
                if fetched_hits == (total_hits or 0):
                    break
                expected_page_size = min(_PAGE_SIZE, (total_hits or 0) - (fetched_hits - len(hits)))
                if len(hits) < expected_page_size:
                    raise RuntimeError(
                        f"SEC EFTS pagination is incomplete for {query_form} "
                        f"{start_date}..{end_date}: expected={total_hits or 0} "
                        f"fetched={fetched_hits}"
                    )
            elif len(hits) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE

        if self.require_exhaustive_efts and fetched_hits != (total_hits or 0):
            raise RuntimeError(
                f"SEC EFTS pagination is incomplete for {query_form} {start_date}..{end_date}: "
                f"expected={total_hits or 0} fetched={fetched_hits}"
            )
        if self.require_exhaustive_efts:
            if (
                any(not identity for identity in hit_identities)
                or len(set(hit_identities)) != (total_hits or 0)
            ):
                raise RuntimeError(
                    f"SEC EFTS filing identities do not reconcile for {query_form} "
                    f"{start_date}..{end_date}"
                )
        self.query_audits.append({
            "query_form": query_form,
            "query_ciks": self._query_ciks,
            "start_date": start_date,
            "end_date": end_date,
            "total_hits": total_hits or 0,
            "fetched_hits": fetched_hits,
            "total_relation": "eq",
        })

        return records

    def _fetch_browse_edgar(self, accepted_forms: set[str], start_date: str, end_date: str, headers: dict, original_error: Exception) -> list[dict]:
        records = []
        browse_headers = dict(headers)
        browse_headers["Accept"] = "application/atom+xml,application/xml,text/xml"
        datea = start_date.replace("-", "")
        dateb = end_date.replace("-", "")

        try:
            for form in sorted(accepted_forms):
                offset = 0
                while True:
                    resp = http_get(
                        _BROWSE_EDGAR_URL,
                        params={
                            "action": "getcurrent",
                            "type": form,
                            "datea": datea,
                            "dateb": dateb,
                            "owner": "include",
                            "start": offset,
                            "count": _PAGE_SIZE,
                            "output": "atom",
                        },
                        headers=browse_headers,
                        max_attempts=_SEC_MAX_ATTEMPTS,
                        timeout=_SEC_TIMEOUT_SECONDS,
                        before_attempt=self._before_sec_request,
                    )
                    entries = self._parse_browse_edgar_entries(resp.text, form, start_date, end_date)
                    records.extend(entries)
                    if len(entries) < _PAGE_SIZE:
                        break
                    offset += _PAGE_SIZE
        except Exception as fallback_error:
            raise RuntimeError(
                f"SEC EFTS and browse-edgar fallback failed for {sorted(accepted_forms)} {start_date}..{end_date}"
            ) from fallback_error

        return records

    def _parse_browse_edgar_entries(self, atom_text: str, form: str, start_date: str, end_date: str) -> list[dict]:
        root = ET.fromstring(atom_text)
        records = []
        for entry in root.findall("atom:entry", _ATOM_NS):
            title = entry.findtext("atom:title", default="", namespaces=_ATOM_NS)
            summary = re.sub(r"<[^>]+>", "", html.unescape(entry.findtext("atom:summary", default="", namespaces=_ATOM_NS)))
            category = entry.find("atom:category", _ATOM_NS)
            link = entry.find("atom:link", _ATOM_NS)
            filed_match = re.search(r"Filed:\s*(\d{4}-\d{2}-\d{2})", summary)
            accession_match = re.search(r"AccNo:\s*([0-9-]+)", summary)
            filed_date = filed_match.group(1) if filed_match else None
            accession_no = accession_match.group(1) if accession_match else None
            if not accession_no or not filed_date or filed_date < start_date or filed_date > end_date:
                continue
            record_form = category.attrib.get("term") if category is not None else form
            if record_form != form:
                continue
            cik_match = re.search(r"\((\d{10})\)", title)
            filer_name = re.sub(rf"^{re.escape(record_form)}\s+-\s+", "", title)
            filer_name = re.sub(r"\s+\(\d{10}\).*", "", filer_name).strip()
            records.append({
                "adsh": accession_no,
                "file_date": filed_date,
                "period_ending": None,
                "display_names": [filer_name] if filer_name else [],
                "form": record_form,
                "file_type": record_form,
                "matched_forms": record_form,
                "ciks": [cik_match.group(1)] if cik_match else [],
                "filing_url": link.attrib.get("href") if link is not None else None,
                "sec_fallback": "browse-edgar",
                "raw_atom": ET.tostring(entry, encoding="unicode"),
            })
        return records

    def _enrich_filing(self, source: dict, headers: dict) -> dict:
        enriched = dict(source)
        enriched["efts_source"] = source
        accession_no = source.get("adsh") or source.get("accession_no")
        required_classes = self._required_document_classes()
        archive = {
            "accession_no": accession_no,
            "archive_status": "missing_filing_locator",
            "filing_index_url": None,
            "archive_owner_cik": None,
            "filer_cik": None,
            "registrant_cik": None,
            "subject_issuer": None,
            "submission_header": None,
            "reporting_owners": [],
            "item_codes": self._source_item_codes(source),
            "document_index": [],
            "primary_document": None,
            "information_table_xml": None,
            "missing_document_classes": list(required_classes),
        }
        enriched["sec_archive"] = archive
        if not accession_no:
            return enriched

        resolved = self._resolve_filing_index(source, accession_no, headers)
        if resolved is None:
            archive["archive_status"] = (
                "terminal_incomplete" if self._is_archive_mature(source) else "retryable_incomplete"
            )
            return enriched

        index_url, index_text, archive_owner_cik = resolved
        archive["filing_index_url"] = index_url
        archive["archive_owner_cik"] = archive_owner_cik
        archive["filer_cik"] = archive_owner_cik
        if self.archive_profile != "13dg":
            archive["registrant_cik"] = archive_owner_cik
        documents = self._parse_document_index(index_text, index_url)
        archive["document_index"] = documents

        primary = self._select_primary_document(documents, source)
        information_table = self._select_information_table(documents) if self.archive_profile == "13f" else None
        if primary:
            archive["primary_document"] = self._fetch_document(primary, headers)
        if information_table:
            archive["information_table_xml"] = self._fetch_document(information_table, headers)

        missing = []
        for document_class in required_classes:
            document = archive[document_class]
            if not document or document.get("fetch_status") != "ok":
                missing.append(document_class)
        archive["missing_document_classes"] = missing
        if not missing:
            archive["archive_status"] = "complete"
        elif any(
            document and document.get("fetch_status") in {"too_large", "truncated"}
            for document in (archive.get(document_class) for document_class in missing)
        ):
            archive["archive_status"] = "terminal_incomplete"
        elif (
            all(archive.get(document_class) is None for document_class in missing)
            and self._is_archive_mature(source)
        ):
            archive["archive_status"] = "terminal_incomplete"
        else:
            archive["archive_status"] = "retryable_incomplete"

        primary_content = (archive["primary_document"] or {}).get("content") or ""
        if self.archive_profile == "8k":
            archive["item_codes"] = sorted(set(archive["item_codes"] + self._extract_8k_item_codes(primary_content)))
        elif self.archive_profile == "13dg":
            subject_issuer, reporting_owners = self._extract_13dg_evidence(primary_content)
            if not (subject_issuer or {}).get("cik"):
                submission_url = self._submission_text_url(
                    (archive["primary_document"] or {}).get("url"), accession_no
                )
                if submission_url:
                    submission_content, _ = self._get_bounded_text(
                        submission_url, headers, _SUBMISSION_HEADER_MAX_BYTES
                    )
                    subject_block = self._submission_subject_block(submission_content)
                    archive["submission_header"] = {
                        "url": submission_url,
                        "fetch_status": "ok",
                        "content_sha256": hashlib.sha256(
                            submission_content.encode("utf-8")
                        ).hexdigest(),
                        "subject_company_block": subject_block,
                    }
                    header_subject = self._extract_submission_subject(subject_block)
                    subject_issuer = {
                        **(subject_issuer or {}),
                        **header_subject,
                    } or None
            archive["subject_issuer"] = subject_issuer
            archive["reporting_owners"] = reporting_owners

        return enriched

    def _is_archive_mature(self, source: dict) -> bool:
        filed_value = source.get("file_date") or source.get("filedAt")
        try:
            filed_date = date.fromisoformat(str(filed_value)[:10])
        except (TypeError, ValueError):
            return False
        return filed_date <= date.today() - timedelta(days=_MISSING_DOCUMENT_TERMINAL_AGE_DAYS)

    def _required_document_classes(self) -> tuple[str, ...]:
        if self.archive_profile == "13f":
            return ("primary_document", "information_table_xml")
        return ("primary_document",)

    def _resolve_filing_index(self, source: dict, accession_no: str, headers: dict) -> Optional[tuple[str, str, Optional[str]]]:
        for index_url, archive_owner_cik in self._filing_index_candidates(source, accession_no):
            try:
                index_text, truncated = self._get_bounded_text(index_url, headers, _INDEX_MAX_BYTES)
                if truncated:
                    raise RuntimeError(f"SEC filing index exceeds {_INDEX_MAX_BYTES} bytes: {index_url}")
            except Exception as exc:
                if self._is_terminal_missing(exc):
                    continue
                raise
            return index_url, index_text, archive_owner_cik
        return None

    def _filing_index_candidates(self, source: dict, accession_no: str) -> list[tuple[str, Optional[str]]]:
        candidates: list[tuple[str, Optional[str]]] = []
        seen = set()

        for field in ("filing_url", "linkToHtml", "link_to_html", "filingUrl"):
            filing_url = source.get(field)
            normalized = self._normalize_filing_index_url(filing_url, accession_no)
            if normalized and normalized not in seen:
                candidates.append((normalized, self._cik_from_archive_url(normalized)))
                seen.add(normalized)

        accession_path = re.sub(r"[^0-9]", "", accession_no)
        if not accession_path:
            return candidates
        ciks = source.get("ciks") or []
        if isinstance(ciks, str):
            ciks = [ciks]
        for cik in ciks:
            cik_digits = re.sub(r"[^0-9]", "", str(cik))
            if not cik_digits:
                continue
            archive_cik = str(int(cik_digits))
            index_url = (
                f"https://www.sec.gov/Archives/edgar/data/{archive_cik}/{accession_path}/"
                f"{accession_no}-index.html"
            )
            if index_url not in seen:
                candidates.append((index_url, cik_digits.zfill(10)))
                seen.add(index_url)
        return candidates

    def _normalize_filing_index_url(self, filing_url: object, accession_no: str) -> Optional[str]:
        if not isinstance(filing_url, str) or not filing_url.strip():
            return None
        parsed = urlparse(filing_url.strip())
        if parsed.scheme != "https" or parsed.hostname not in _SEC_ARCHIVES_HOSTS:
            return None

        path = parsed.path
        if path.startswith("/ixviewer/doc/action") or path == "/ix":
            path = unquote(parse_qs(parsed.query).get("doc", [""])[0])
        if not path.lower().startswith("/archives/edgar/data/"):
            return None
        accession_path = re.sub(r"[^0-9]", "", accession_no)
        if accession_path not in path:
            return None
        if path.lower().endswith(("-index.htm", "-index.html")):
            return f"https://www.sec.gov{path}"

        directory = path.rsplit("/", 1)[0]
        return f"https://www.sec.gov{directory}/{accession_no}-index.html"

    def _cik_from_archive_url(self, filing_url: str) -> Optional[str]:
        match = re.search(r"/Archives/edgar/data/(\d+)/", urlparse(filing_url).path, re.IGNORECASE)
        return match.group(1).zfill(10) if match else None

    def _get_bounded_text(self, url: str, headers: dict, max_bytes: int) -> tuple[str, bool]:
        request_headers = dict(headers)
        request_headers["Accept"] = "text/html,application/xhtml+xml,application/xml,text/xml,text/plain"
        request_headers["Accept-Encoding"] = "identity"
        request_headers["Range"] = f"bytes=0-{max_bytes - 1}"
        response = http_get(
            url,
            headers=request_headers,
            max_attempts=_SEC_MAX_ATTEMPTS,
            timeout=_SEC_TIMEOUT_SECONDS,
            max_response_bytes=max_bytes,
            before_attempt=self._before_sec_request,
        )
        truncated = bool(getattr(response, "extensions", {}).get("auspex_truncated"))
        content, content_truncated = self._truncate_utf8(response.text, max_bytes)
        return content, truncated or content_truncated

    def _parse_document_index(self, index_text: str, index_url: str) -> list[dict]:
        parser = _DocumentTableParser()
        parser.feed(index_text)
        documents = []
        for row in parser.rows[:_MAX_INDEX_DOCUMENTS]:
            document_url = self._archive_document_url(index_url, row["href"])
            documents.append({
                "sequence": row["sequence"] or None,
                "description": row["description"] or None,
                "name": row["name"] or None,
                "document_type": row["document_type"] or None,
                "size_bytes": self._parse_size_bytes(row["size"]),
                "url": document_url,
                "is_text": self._is_text_document(row["name"], document_url),
            })
        return documents

    def _archive_document_url(self, index_url: str, href: object) -> Optional[str]:
        if not isinstance(href, str) or not href.strip():
            return None
        document_url = urljoin(index_url, href.strip())
        parsed = urlparse(document_url)
        index_parsed = urlparse(index_url)
        if parsed.scheme != "https" or parsed.hostname not in _SEC_ARCHIVES_HOSTS:
            return None
        path = parsed.path
        if path.startswith("/ixviewer/doc/action") or path == "/ix":
            path = unquote(parse_qs(parsed.query).get("doc", [""])[0])
            document_url = f"https://www.sec.gov{path}"
            parsed = urlparse(document_url)
        index_accession = self._archive_accession(index_parsed.path)
        document_accession = self._archive_accession(parsed.path)
        if index_accession is None or document_accession != index_accession:
            return None
        return document_url

    def _archive_accession(self, path: str) -> Optional[str]:
        match = re.search(r"/Archives/edgar/data/\d+/(\d{18})(?:/|$)", path, flags=re.IGNORECASE)
        return match.group(1) if match else None

    def _select_primary_document(self, documents: list[dict], source: dict) -> Optional[dict]:
        filing_form = str(source.get("form") or source.get("file_type") or "").upper()
        root_form = self._root_form(filing_form)
        ordered_documents = sorted(documents, key=self._document_preference)
        for document in ordered_documents:
            document_type = str(document.get("document_type") or "").upper()
            if document["is_text"] and document["url"] and document_type == filing_form:
                return document
        for document in ordered_documents:
            document_type = str(document.get("document_type") or "").upper()
            if document["is_text"] and document["url"] and self._root_form(document_type) == root_form:
                return document
        return None

    def _select_information_table(self, documents: list[dict]) -> Optional[dict]:
        for document in sorted(documents, key=self._document_preference):
            label = " ".join(str(document.get(field) or "") for field in ("description", "document_type", "name")).upper()
            if document["is_text"] and document["url"] and "INFORMATION TABLE" in label:
                return document
        return None

    def _document_preference(self, document: dict) -> tuple:
        url_path = urlparse(str(document.get("url") or "")).path.lower()
        name = str(document.get("name") or "").lower()
        rendered_view = "/xsl" in url_path or name.endswith((".htm", ".html"))
        raw_xml = url_path.endswith(".xml") and not rendered_view
        return (
            0 if raw_xml else 1,
            0 if document.get("size_bytes") is not None else 1,
            str(document.get("sequence") or ""),
            url_path,
        )

    def _fetch_document(self, document: dict, headers: dict) -> dict:
        evidence = dict(document)
        size_bytes = document.get("size_bytes")
        if size_bytes is not None and size_bytes > _DOCUMENT_MAX_BYTES:
            evidence.update({"fetch_status": "too_large", "content": None, "content_truncated": True})
            return evidence
        try:
            content, truncated = self._get_bounded_text(document["url"], headers, _DOCUMENT_MAX_BYTES)
        except Exception as exc:
            if self._is_terminal_missing(exc):
                raise RuntimeError(f"SEC required document is not yet available: {document['url']}") from exc
            raise
        if truncated:
            evidence.update({
                "fetch_status": "truncated",
                "content": content,
                "content_truncated": True,
                "content_bytes": len(content.encode("utf-8")),
            })
            return evidence
        evidence.update({
            "fetch_status": "ok",
            "content": content,
            "content_truncated": truncated,
            "content_bytes": len(content.encode("utf-8")),
        })
        return evidence

    def _source_item_codes(self, source: dict) -> list[str]:
        items = source.get("items") or source.get("item_codes") or []
        if isinstance(items, str):
            items = re.split(r"[,;\s]+", items)
        return sorted({str(item).strip() for item in items if re.fullmatch(r"\d{1,2}\.\d{2}", str(item).strip())})

    def _extract_8k_item_codes(self, content: str) -> list[str]:
        text = self._plain_text(content)
        return sorted(set(re.findall(r"\bItem\s+(\d{1,2}\.\d{2})\b", text, flags=re.IGNORECASE)))

    def _extract_13dg_evidence(self, content: str) -> tuple[Optional[dict], list[dict]]:
        try:
            return self._extract_13dg_xml(content.lstrip("\ufeff\r\n\t "))
        except ET.ParseError:
            pass
        return self._extract_13dg_html(content)

    def _extract_13dg_xml(self, content: str) -> tuple[Optional[dict], list[dict]]:
        root = ET.fromstring(content)
        subject_element = self._first_element(root, {"subjectCompany", "subjectIssuer"})
        subject = None
        if subject_element is not None:
            subject = self._compact_dict({
                "cik": self._first_text(subject_element, {"issuerCik", "subjectCik", "cik"}),
                "name": self._first_text(subject_element, {"issuerName", "subjectCompanyName", "name"}),
                "class_title": self._first_text(subject_element, {"classTitle", "titleOfClass"}),
                "cusip": self._first_text(subject_element, {"cusip", "cusipNumber"}),
            }) or None
        if subject is None:
            subject = self._compact_dict({
                "cik": self._first_text(root, {"issuerCik"}),
                "name": self._first_text(root, {"issuerName"}),
                "class_title": self._first_text(root, {"classTitle", "titleOfClass"}),
                "cusip": self._first_text(root, {"issuerCusip", "cusip", "cusipNumber"}),
            }) or None

        owners = []
        owner_elements = self._elements(root, {"reportingPerson", "reportingOwner"})
        for owner_element in owner_elements:
            owner = self._compact_dict({
                "cik": self._first_text(owner_element, {"rptOwnerCik", "reportingOwnerCik", "cik"}),
                "name": self._first_text(owner_element, {"rptOwnerName", "reportingOwnerName", "name"}),
                "percent_owned": self._first_text(owner_element, {
                    "percentOfClassRepresentedByAmount", "percentOfClass", "percentOwned",
                }),
            })
            if owner and owner not in owners:
                owners.append(owner)
        return subject, owners

    def _submission_text_url(
        self, primary_url: object, accession_no: str
    ) -> Optional[str]:
        if not isinstance(primary_url, str):
            return None
        parsed = urlparse(primary_url)
        if parsed.scheme != "https" or parsed.hostname not in _SEC_ARCHIVES_HOSTS:
            return None
        if self._archive_accession(parsed.path) is None:
            return None
        directory = parsed.path.rsplit("/", 1)[0]
        return f"https://www.sec.gov{directory}/{accession_no}.txt"

    def _submission_subject_block(self, content: str) -> str:
        match = re.search(
            r"(?ms)^SUBJECT COMPANY:\s*(.*?)(?=^[A-Z][A-Z -]+:\s*$|\Z)",
            content,
        )
        return match.group(1).strip() if match else ""

    def _extract_submission_subject(self, subject_block: str) -> dict:
        return self._compact_dict({
            "cik": self._match_label(
                subject_block, r"CENTRAL INDEX KEY:\s*(\d{1,10})"
            ),
            "name": self._match_label(
                subject_block, r"COMPANY CONFORMED NAME:\s*([^\r\n]+)"
            ),
        })

    def _extract_13dg_html(self, content: str) -> tuple[Optional[dict], list[dict]]:
        text = self._plain_text(content)
        lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]
        subject = self._compact_dict({
            "cik": self._match_label(text, r"(?:Subject(?: Company)?|Issuer)\s+(?:CIK|Central Index Key)\s*[:#]?\s*(\d{1,10})"),
            "name": self._value_before_label(lines, "Name of Issuer"),
            "class_title": self._value_before_label(lines, "Title of Class of Securities"),
            "cusip": self._value_before_label(lines, "CUSIP Number"),
        }) or None
        owners = []
        owner_headings = list(re.finditer(r"(?im)^\s*(?:\(\s*1\s*\)\s*)?NAMES? OF REPORTING PERSONS?\s*:?\s*$", text))
        for index, heading in enumerate(owner_headings):
            block_end = owner_headings[index + 1].start() if index + 1 < len(owner_headings) else len(text)
            block = text[heading.end():block_end]
            owner_name = self._reporting_owner_name(block)
            percentage = self._match_label(
                block,
                r"Percent\s+of\s+Class\s+Represented\s+by\s+Amount\s+in\s+Row\s*\(?11\)?\s*:?\s*"
                r"([0-9]+(?:\.[0-9]+)?%?)",
            )
            owner = self._compact_dict({
                "name": owner_name,
                "percent_owned": percentage,
            })
            if owner_name and percentage and owner not in owners:
                owners.append(owner)
        return subject, owners

    def _value_before_label(self, lines: list[str], label: str) -> Optional[str]:
        normalized_label = re.sub(r"\s+", " ", label).lower()
        for index, line in enumerate(lines):
            if normalized_label in line.lower() and index > 0:
                value = lines[index - 1].strip(" ()")
                if index > 1 and re.fullmatch(
                    r"(?:INC\.?|INCORPORATED|CORP\.?|CORPORATION|LTD\.?|LIMITED|LLC|L\.L\.C\.?|"
                    r"LP|L\.P\.?|PLC|N\.V\.?|S\.A\.?)",
                    value,
                    flags=re.IGNORECASE,
                ):
                    value = f"{lines[index - 2].strip(' ()')} {value}".strip()
                if self._plausible_name(value) or normalized_label == "cusip number":
                    return value
        return None

    def _reporting_owner_name(self, block: str) -> Optional[str]:
        before_row_two = re.split(r"(?im)^\s*(?:\(\s*2\s*\)|2)\s*(?:CHECK|$)", block, maxsplit=1)[0]
        candidates = [re.sub(r"\s+", " ", line).strip(" ()") for line in before_row_two.splitlines()]
        candidates = [
            candidate for candidate in candidates
            if self._plausible_name(candidate)
            and not re.search(r"I\.R\.S\.|IDENTIFICATION|ENTITIES ONLY", candidate, flags=re.IGNORECASE)
        ]
        return candidates[-1] if candidates else None

    def _plausible_name(self, value: object) -> bool:
        text = str(value or "").strip()
        return len(text) >= 3 and len(re.findall(r"[A-Za-z]", text)) >= 2

    def _plain_text(self, content: str) -> str:
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", content)
        text = re.sub(r"<[^>]+>", " ", text)
        return html.unescape(text).replace("\r", "\n")

    def _first_element(self, root: ET.Element, names: set[str]) -> Optional[ET.Element]:
        return next(iter(self._elements(root, names)), None)

    def _elements(self, root: ET.Element, names: set[str]) -> list[ET.Element]:
        return [element for element in root.iter() if self._local_name(element.tag) in names]

    def _first_text(self, root: ET.Element, names: set[str]) -> Optional[str]:
        for element in root.iter():
            if self._local_name(element.tag) in names and element.text and element.text.strip():
                return element.text.strip()
        return None

    def _local_name(self, tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    def _compact_dict(self, values: dict) -> dict:
        return {key: value for key, value in values.items() if value not in (None, "")}

    def _match_label(self, text: str, pattern: str) -> Optional[str]:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        return match.group(1).strip() if match else None

    def _is_text_document(self, name: object, url: Optional[str]) -> bool:
        candidate = str(url or name or "")
        suffix = os.path.splitext(urlparse(candidate).path.lower())[1]
        if suffix in _BINARY_DOCUMENT_EXTENSIONS:
            return False
        return suffix in _TEXT_DOCUMENT_EXTENSIONS

    def _parse_size_bytes(self, value: object) -> Optional[int]:
        match = re.search(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*([KMGT]?B)?", str(value or ""), flags=re.IGNORECASE)
        if not match:
            return None
        number = float(match.group(1).replace(",", ""))
        multiplier = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}.get((match.group(2) or "").upper(), 1)
        return int(number * multiplier)

    def _truncate_utf8(self, value: str, max_bytes: int) -> tuple[str, bool]:
        encoded = value.encode("utf-8")
        if len(encoded) <= max_bytes:
            return value, False
        return encoded[:max_bytes].decode("utf-8", errors="ignore"), True

    def _is_terminal_missing(self, exc: Exception) -> bool:
        response = getattr(exc, "response", None)
        return getattr(response, "status_code", None) in {404, 410}

    def _dedupe_records(self, records: list[dict]) -> list[dict]:
        seen = set()
        deduped = []
        for record in records:
            key = record.get("adsh") or record.get("accession_no") or repr(sorted(record.items()))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(record)
        return sorted(
            deduped,
            key=lambda record: (
                str(record.get("file_date") or ""),
                str(record.get("form") or record.get("file_type") or ""),
                str(record.get("adsh") or record.get("accession_no") or ""),
            ),
        )
