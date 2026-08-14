"""EDGAR bulk archive extraction (arc42 §6.3 step 3) — streamed via HTTP Range requests.

``submissions.zip`` and ``companyfacts.zip`` are each several GB and hold one
JSON document per CIK (``CIK{10-digit}.json``) across the *entire* EDGAR
filer population, while Auspex only needs the configured securities in
``config/universe.yaml``. Rather than downloading either archive in full to
local ephemeral storage, :class:`RemoteZipArchive` reads only the bytes it
actually needs — the End-Of-Central-Directory record, the central
directory, and the local header + compressed data of each requested member
— via HTTP Range requests against SEC's static archive hosting.

All ZIP-format parsing (including the Zip64 extensions these multi-hundred-
thousand-entry archives require) is delegated to the standard library's
``zipfile``, which already implements it correctly; the only new code here
is the small, independently-testable HTTP-range file adapter
(:class:`_HttpRangeFile`) that ``zipfile.ZipFile`` reads through. The full
archive body is never fetched, buffered, or written to disk — only the
selected universe records are ever materialised, matching arc42 §6.3 step 3
("persist only selected universe records/raw artefacts").

``extract_cik_json``/``list_available_ciks`` remain as synchronous helpers
over an already-local zip file (e.g. for tests, or an environment where the
archive is already on disk for some other reason); they were already
selective (``zf.open`` one member, never ``extractall``) and are unchanged.
"""

from __future__ import annotations

import asyncio
import io
import json
import time
import zipfile
from collections.abc import Iterable
from datetime import date
from pathlib import Path

import httpx

from auspex.providers.edgar import (
    latest_accession_for_forms,
    latest_filing_date_for_forms,
)

SUBMISSIONS_BULK_URL = "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip"
COMPANYFACTS_BULK_URL = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"

# Batches many small zipfile-internal reads (the EOCD record, walking the central
# directory, each member's local header) into a handful of HTTP Range requests
# instead of one tiny request per read() call.
_MIN_RANGE_CHUNK_BYTES = 512 * 1024


def cik_entry_name(cik: str) -> str:
    return f"CIK{cik}.json"


class RemoteRangeNotSupportedError(RuntimeError):
    """The archive host did not honour an HTTP Range request (no 206 response).

    Without Range support there is no way to read a subset of a multi-GB
    archive without downloading it in full — surfaced loudly rather than
    silently falling back to buffering gigabytes in memory or on disk.
    """


# --- local-file helpers (unchanged: already selective, not a full expansion) -----------


def extract_cik_json(zip_path: str | Path, cik: str) -> dict | None:
    """Read one CIK's JSON document out of a local bulk zip archive without
    extracting the whole archive to disk. Returns ``None`` if that CIK has no
    entry (e.g. never filed, or filed under a different CIK)."""

    name = cik_entry_name(cik)
    with zipfile.ZipFile(zip_path) as zf:
        if name not in zf.namelist():
            return None
        with zf.open(name) as f:
            return json.load(f)


def list_available_ciks(zip_path: str | Path) -> set[str]:
    """All CIKs present in a local bulk zip archive, e.g. to report coverage
    before extraction."""

    with zipfile.ZipFile(zip_path) as zf:
        ciks = set()
        for name in zf.namelist():
            if name.startswith("CIK") and name.endswith(".json"):
                ciks.add(name[3:-5])
        return ciks


# --- remote, range-request-backed streaming (arc42 §6.3 step 3) -----------------------


class _HttpRangeFile(io.RawIOBase):
    """A read-only, seekable file-like object backed by HTTP Range requests.

    This is the one piece of new, hand-written logic: everything ZIP-format
    related (including Zip64) is left to the standard library's ``zipfile``,
    which reads through this object exactly as it would a local file —
    ``seek``/``tell``/``readinto`` — with no knowledge that the bytes are
    actually arriving over the network a chunk at a time.
    """

    def __init__(self, *, client: httpx.Client, url: str, headers: dict[str, str], size: int, min_delay: float) -> None:
        super().__init__()
        self._client = client
        self._url = url
        self._headers = headers
        self._size = size
        self._min_delay = min_delay
        self._pos = 0
        self._cache_start = -1
        self._cache_bytes = b""
        self._last_request_at = 0.0
        self.bytes_fetched = 0  # total bytes actually transferred — for logging/tests

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self._pos = offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        elif whence == io.SEEK_END:
            self._pos = self._size + offset
        else:
            raise ValueError(f"unsupported whence: {whence}")
        return self._pos

    def tell(self) -> int:
        return self._pos

    def readinto(self, b) -> int:  # noqa: ANN001 - matches io.RawIOBase.readinto's buffer-protocol signature
        want = len(b)
        if want == 0 or self._pos >= self._size:
            return 0
        data = self._read_range(self._pos, want)
        n = len(data)
        b[:n] = data
        self._pos += n
        return n

    def _read_range(self, start: int, want: int) -> bytes:
        end_exclusive = min(self._size, start + want)
        if (
            self._cache_start != -1
            and start >= self._cache_start
            and end_exclusive <= self._cache_start + len(self._cache_bytes)
        ):
            offset = start - self._cache_start
            return self._cache_bytes[offset : offset + (end_exclusive - start)]

        fetch_len = max(want, _MIN_RANGE_CHUNK_BYTES)
        fetch_end_exclusive = min(self._size, start + fetch_len)
        self._throttle()
        headers = {**self._headers, "Range": f"bytes={start}-{fetch_end_exclusive - 1}"}
        response = self._client.get(self._url, headers=headers)
        response.raise_for_status()
        if response.status_code != 206:
            raise RemoteRangeNotSupportedError(
                f"{self._url} did not honour an HTTP Range request (got status {response.status_code}); "
                "cannot read a subset of this archive without downloading it in full"
            )
        self.bytes_fetched += len(response.content)
        self._cache_start = start
        self._cache_bytes = response.content
        return self._cache_bytes[: end_exclusive - start]

    def _throttle(self) -> None:
        if self._min_delay <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        remaining = self._min_delay - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_at = time.monotonic()


class RemoteZipArchive:
    """Random-access view over a remote ZIP archive, opened via ``zipfile``
    against an HTTP-Range-backed file object — no local copy, ever.

    Construction issues a ``HEAD`` request for ``Content-Length`` and then
    opens ``zipfile.ZipFile`` against the range-backed file, which itself
    triggers a small number of Range reads to parse the End-Of-Central-
    Directory record and central directory. No archive member is read until
    :meth:`extract_cik_json` is called for it.
    """

    def __init__(self, client: httpx.Client, url: str, headers: dict[str, str], rate_limit_per_second: float) -> None:
        head = client.head(url, headers=headers)
        head.raise_for_status()
        size = int(head.headers["content-length"])
        min_delay = 1.0 / rate_limit_per_second if rate_limit_per_second > 0 else 0.0
        self._range_file = _HttpRangeFile(client=client, url=url, headers=headers, size=size, min_delay=min_delay)
        self._zip = zipfile.ZipFile(self._range_file)

    def list_available_ciks(self) -> set[str]:
        ciks = set()
        for name in self._zip.namelist():
            if name.startswith("CIK") and name.endswith(".json"):
                ciks.add(name[3:-5])
        return ciks

    def extract_cik_json(self, cik: str) -> dict | None:
        name = cik_entry_name(cik)
        if name not in self._zip.namelist():
            return None
        with self._zip.open(name) as f:
            return json.load(f)

    @property
    def bytes_fetched(self) -> int:
        """Total bytes actually transferred over HTTP so far — always far below
        the full archive size, since only requested members are ever read."""

        return self._range_file.bytes_fetched

    def close(self) -> None:
        self._zip.close()


async def open_remote_bulk_zip(
    url: str,
    *,
    user_agent: str,
    rate_limit_per_second: float = 8.0,
    client: httpx.Client | None = None,
) -> RemoteZipArchive:
    """Open a remote bulk archive for selective, range-based reads (arc42 §6.3 step 3).

    ``Accept-Encoding: identity`` is mandatory here: Range offsets are only
    meaningful against the exact on-disk (compressed-as-a-zip) byte layout,
    so the response must not be transparently gzip-recompressed on top of
    that. The blocking ``HEAD`` + ``zipfile.ZipFile`` open (which itself
    reads the EOCD/central directory) runs off the event loop via
    ``asyncio.to_thread``.
    """

    owns_client = client is None
    sync_client = client or httpx.Client(timeout=30.0)
    headers = {"User-Agent": user_agent, "Accept-Encoding": "identity"}
    try:
        return await asyncio.to_thread(RemoteZipArchive, sync_client, url, headers, rate_limit_per_second)
    except Exception:
        if owns_client:
            sync_client.close()
        raise


async def extract_universe_cik_documents(archive: RemoteZipArchive, ciks: Iterable[str]) -> dict[str, dict]:
    """Fetch only the requested CIKs' JSON documents from an already-open
    remote archive (arc42 §6.3 step 3: "persist only selected universe
    records"). A CIK with no entry in the archive is simply absent from the
    result rather than raising."""

    def _extract_all() -> dict[str, dict]:
        result: dict[str, dict] = {}
        for cik in ciks:
            doc = archive.extract_cik_json(cik)
            if doc is not None:
                result[cik] = doc
        return result

    return await asyncio.to_thread(_extract_all)


def _filter_submissions_by_floor(submissions: dict, floor_date: date) -> dict:
    """Truncate ``submissions["filings"]["recent"]``'s parallel lists to
    entries with ``filingDate >= floor_date`` (arc42 §6.3: "two windows, not
    one" — bulk collection is bounded to the 36-month raw backfill window).

    ``submissions.json``'s ``filings.recent`` block is a set of *parallel*
    lists (``form``, ``filingDate``, ``accessionNumber``, ...) all indexed by
    the same position; every list-valued key must be re-indexed together to
    keep them aligned. Non-list keys (e.g. ``cik``, ``name``) and any other
    top-level key (e.g. ``filings.files``, which the collectors never read)
    are passed through unchanged — this is a read-only, non-destructive
    filter over a shallow copy, never a mutation of the cached bulk document.
    """

    filings = submissions.get("filings")
    if not isinstance(filings, dict):
        return submissions
    recent = filings.get("recent")
    if not isinstance(recent, dict):
        return submissions
    filing_dates = recent.get("filingDate", [])
    if not filing_dates:
        return submissions

    floor_str = floor_date.isoformat()
    keep_indices = [i for i, d in enumerate(filing_dates) if d >= floor_str]
    if len(keep_indices) == len(filing_dates):
        return submissions  # nothing to truncate

    filtered_recent = {}
    for key, value in recent.items():
        if isinstance(value, list) and len(value) == len(filing_dates):
            filtered_recent[key] = [value[i] for i in keep_indices]
        else:
            filtered_recent[key] = value

    filtered_filings = {**filings, "recent": filtered_recent}
    return {**submissions, "filings": filtered_filings}


class BulkEdgarSource:
    """Wraps an :class:`~auspex.providers.edgar.EdgarClient` so bootstrap's
    step 2 (filer_profile verification) and steps 4-9 (via the shared
    nightly collectors) read ``submissions.json``/``companyfacts.json``
    bodies already streamed from the bulk archives (arc42 §6.3 step 3)
    instead of re-fetching one incremental HTTP call per security.

    ``get_submissions``/``get_company_facts`` are served from the in-memory
    dict extracted by :func:`extract_universe_cik_documents` when present,
    falling back to the wrapped client's per-CIK endpoint only for a CIK
    that had no entry in the bulk archive (e.g. added to the universe after
    the most recent daily archive publish). Every other method — filing
    document/Form 4 XML download, ``company_tickers.json``, ``aclose`` —
    always delegates, since those are not covered by either archive.

    ``floor_date``, when set, bounds ``get_submissions()`` to the 36-month
    raw backfill window (arc42 §6.3 steps 5 and 8: bulk filing collection
    and Form 4/insider collection must not walk EDGAR's full filing history
    for a security that has been public for decades). It applies to both the
    cached bulk document and any per-CIK fallback fetch, so the window is
    respected regardless of which path served the submissions body.
    """

    def __init__(
        self,
        delegate,
        *,
        submissions_by_cik: dict[str, dict],
        companyfacts_by_cik: dict[str, dict],
        floor_date: date | None = None,
    ) -> None:
        self._delegate = delegate
        self._submissions_by_cik = submissions_by_cik
        self._companyfacts_by_cik = companyfacts_by_cik
        self.floor_date = floor_date

    async def get_company_tickers(self) -> dict:
        return await self._delegate.get_company_tickers()

    async def get_submissions(self, cik: str) -> dict:
        cached = self._submissions_by_cik.get(cik)
        submissions = cached if cached is not None else await self._delegate.get_submissions(cik)
        if self.floor_date is not None:
            submissions = _filter_submissions_by_floor(submissions, self.floor_date)
        return submissions

    async def latest_accession(
        self, cik: str, forms: set[str] | frozenset[str]
    ) -> str | None:
        cached = self._submissions_by_cik.get(cik)
        submissions = (
            cached if cached is not None else await self._delegate.get_submissions(cik)
        )
        return latest_accession_for_forms(submissions, forms)

    async def latest_filing_date(
        self, cik: str, forms: set[str] | frozenset[str]
    ) -> date | None:
        cached = self._submissions_by_cik.get(cik)
        submissions = (
            cached if cached is not None else await self._delegate.get_submissions(cik)
        )
        return latest_filing_date_for_forms(submissions, forms)

    async def get_company_facts(self, cik: str) -> dict:
        cached = self._companyfacts_by_cik.get(cik)
        if cached is not None:
            return cached
        return await self._delegate.get_company_facts(cik)

    async def get_filing_document(self, cik: str, accession_no_dashes: str, filename: str) -> str:
        return await self._delegate.get_filing_document(cik, accession_no_dashes, filename)

    async def get_form4_xml(self, cik: str, accession_no_dashes: str, filename: str) -> str:
        return await self._delegate.get_form4_xml(cik, accession_no_dashes, filename)

    async def aclose(self) -> None:
        await self._delegate.aclose()
