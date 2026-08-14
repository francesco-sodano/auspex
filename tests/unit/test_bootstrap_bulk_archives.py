"""Unit tests for `BootstrapRunner.fetch_bulk_archives` (arc42 §6.3 step 3).

Verifies bootstrap's use of the streamed EDGAR bulk archive reader: only the
universe securities' CIK entries are extracted (keyed by security_id, not
CIK), and only those extracted records are persisted as raw artefacts —
matching "persist only selected universe records/raw artefacts". No local
zip file is ever created; the transport is an in-memory `httpx.MockTransport`
simulating an HTTP Range-supporting host.
"""

from __future__ import annotations

import json

import httpx
import pytest

from auspex.cli.bootstrap import BootstrapRunner
from auspex.config.loader import Universe
from auspex.models.enums import FilerProfile
from auspex.models.security import Security
from auspex.persistence.memory import InMemoryBlobSink
from auspex.providers.edgar_bulk import SUBMISSIONS_BULK_URL
from tests.unit.test_edgar_bulk import build_fake_archive_bytes


def make_universe() -> Universe:
    securities = [
        Security(
            id="sec-nvda",
            ticker="NVDA",
            cik="0001045810",
            name="NVIDIA",
            cohort="semi-compute",
            filer_profile=FilerProfile.DOMESTIC,
            investable=True,
        ),
        Security(
            id="sec-amd",
            ticker="AMD",
            cik="0000002488",
            name="AMD",
            cohort="semi-compute",
            filer_profile=FilerProfile.DOMESTIC,
            investable=True,
        ),
    ]
    return Universe(securities=securities)


def make_dual_archive_client(submissions_data: bytes, companyfacts_data: bytes) -> httpx.Client:
    """A single mock client that serves two distinct archives depending on
    which bulk URL is requested — mirroring the two real SEC endpoints."""

    def handler(request: httpx.Request) -> httpx.Response:
        data = submissions_data if str(request.url) == SUBMISSIONS_BULK_URL else companyfacts_data

        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(data)), "accept-ranges": "bytes"})

        range_header = request.headers.get("range")
        if range_header is None:
            return httpx.Response(200, content=data)

        _, _, spec = range_header.partition("=")
        start_s, _, end_s = spec.partition("-")
        start, end = int(start_s), min(int(end_s), len(data) - 1)
        chunk = data[start : end + 1]
        return httpx.Response(
            206,
            content=chunk,
            headers={"content-range": f"bytes {start}-{end}/{len(data)}", "content-length": str(len(chunk))},
        )

    return httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)


class TestFetchBulkArchives:
    @pytest.mark.asyncio
    async def test_extracts_only_universe_securities_keyed_by_security_id(self):
        universe = make_universe()
        submissions_data = build_fake_archive_bytes(
            {"0001045810": {"cik": "0001045810", "kind": "submissions"}}, noise_entries=20
        )
        companyfacts_data = build_fake_archive_bytes(
            {"0001045810": {"cik": "0001045810", "kind": "companyfacts"}}, noise_entries=20
        )
        client = make_dual_archive_client(submissions_data, companyfacts_data)
        try:
            runner = BootstrapRunner(universe=universe, context_factory=lambda d: None)
            result = await runner.fetch_bulk_archives(user_agent="test-agent", rate_limit_per_second=0, client=client)
        finally:
            client.close()

        # NVDA had entries in both archives; AMD had neither (never filed under
        # that CIK in this synthetic archive) — absent, not an error.
        assert result.submissions_by_security == {"sec-nvda": {"cik": "0001045810", "kind": "submissions"}}
        assert result.companyfacts_by_security == {"sec-nvda": {"cik": "0001045810", "kind": "companyfacts"}}
        assert "sec-amd" not in result.submissions_by_security
        assert "sec-amd" not in result.companyfacts_by_security

    @pytest.mark.asyncio
    async def test_persists_only_extracted_records_as_raw_artefacts(self):
        universe = make_universe()
        submissions_data = build_fake_archive_bytes(
            {"0001045810": {"cik": "0001045810", "kind": "submissions"}}, noise_entries=20
        )
        companyfacts_data = build_fake_archive_bytes({}, noise_entries=20)  # NVDA absent here
        client = make_dual_archive_client(submissions_data, companyfacts_data)
        blob_sink = InMemoryBlobSink()
        try:
            runner = BootstrapRunner(universe=universe, context_factory=lambda d: None)
            await runner.fetch_bulk_archives(
                user_agent="test-agent", rate_limit_per_second=0, blob_sink=blob_sink, client=client
            )
        finally:
            client.close()

        # Only one raw artefact persisted (submissions for NVDA) — no companyfacts
        # artefact (absent from the archive) and nothing at all for AMD.
        assert list(blob_sink.documents.keys()) == ["documents/sec-nvda/bulk-submissions.json"]
        persisted = json.loads(blob_sink.documents["documents/sec-nvda/bulk-submissions.json"])
        assert persisted == {"cik": "0001045810", "kind": "submissions"}

    @pytest.mark.asyncio
    async def test_bytes_transferred_reported_and_small_relative_to_archives(self):
        universe = make_universe()
        submissions_data = build_fake_archive_bytes(
            {"0001045810": {"cik": "0001045810"}}, noise_entries=200, noise_padding_bytes=3000
        )
        companyfacts_data = build_fake_archive_bytes(
            {"0001045810": {"cik": "0001045810"}}, noise_entries=200, noise_padding_bytes=3000
        )
        client = make_dual_archive_client(submissions_data, companyfacts_data)
        try:
            runner = BootstrapRunner(universe=universe, context_factory=lambda d: None)
            result = await runner.fetch_bulk_archives(user_agent="test-agent", rate_limit_per_second=0, client=client)
        finally:
            client.close()

        assert result.bytes_transferred > 0
        assert result.bytes_transferred < (len(submissions_data) + len(companyfacts_data))

    @pytest.mark.asyncio
    async def test_no_blob_sink_still_returns_extracted_records(self):
        universe = make_universe()
        data = build_fake_archive_bytes({"0001045810": {"cik": "0001045810"}}, noise_entries=10)
        client = make_dual_archive_client(data, data)
        try:
            runner = BootstrapRunner(universe=universe, context_factory=lambda d: None)
            result = await runner.fetch_bulk_archives(user_agent="test-agent", rate_limit_per_second=0, client=client)
        finally:
            client.close()

        assert "sec-nvda" in result.submissions_by_security
