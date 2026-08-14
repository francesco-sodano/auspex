"""Unit tests for streamed EDGAR bulk archive extraction (arc42 §6.3 step 3).

Proves the central claim: extracting a handful of CIK entries from a large
remote ZIP archive transfers far fewer bytes than the archive's full size —
i.e. the archive is never downloaded in full to local ephemeral storage.
Uses ``httpx.MockTransport`` to simulate an HTTP Range-supporting server
entirely in-memory, so no real network access to sec.gov is required.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile

import httpx
import pytest

from auspex.providers.edgar_bulk import (
    RemoteRangeNotSupportedError,
    RemoteZipArchive,
    cik_entry_name,
    extract_universe_cik_documents,
    open_remote_bulk_zip,
)


def _pseudo_random_hex(seed: str, length: int) -> str:
    """Deterministic, low-compressibility filler content — plain repeated
    characters compress away to nearly nothing under DEFLATE and would
    understate how much of a *real* archive's bytes get transferred."""

    out = []
    counter = 0
    while sum(len(chunk) for chunk in out) < length:
        out.append(hashlib.sha256(f"{seed}:{counter}".encode()).hexdigest())
        counter += 1
    return "".join(out)[:length]


def build_fake_archive_bytes(
    target_ciks: dict[str, dict], noise_entries: int = 200, noise_padding_bytes: int = 2000
) -> bytes:
    """A synthetic ZIP archive with `noise_entries` unrelated CIK documents
    (standing in for the hundreds of thousands of other EDGAR filers) plus
    the specific CIK->document payloads under test."""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for i in range(noise_entries):
            noise_cik = f"{9000000000 + i:010d}"
            padding = _pseudo_random_hex(noise_cik, noise_padding_bytes)
            zf.writestr(cik_entry_name(noise_cik), json.dumps({"cik": noise_cik, "padding": padding}))
        for cik, doc in target_ciks.items():
            zf.writestr(cik_entry_name(cik), json.dumps(doc))
    return buffer.getvalue()


class RangeServingTransport:
    """A minimal in-memory stand-in for an HTTP server that honours Range
    requests against a fixed byte buffer — exactly the SEC static-archive
    hosting contract this module depends on."""

    def __init__(self, data: bytes, support_ranges: bool = True) -> None:
        self._data = data
        self._support_ranges = support_ranges
        self.range_request_count = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(self._data)), "accept-ranges": "bytes"})

        range_header = request.headers.get("range")
        if range_header is None:
            return httpx.Response(200, content=self._data)

        self.range_request_count += 1
        if not self._support_ranges:
            return httpx.Response(200, content=self._data)

        unit, _, spec = range_header.partition("=")
        start_s, _, end_s = spec.partition("-")
        start, end = int(start_s), int(end_s)
        end = min(end, len(self._data) - 1)
        chunk = self._data[start : end + 1]
        return httpx.Response(
            206,
            content=chunk,
            headers={
                "content-range": f"bytes {start}-{end}/{len(self._data)}",
                "content-length": str(len(chunk)),
            },
        )


def make_client(data: bytes, support_ranges: bool = True) -> tuple[httpx.Client, RangeServingTransport]:
    server = RangeServingTransport(data, support_ranges=support_ranges)
    transport = httpx.MockTransport(server.handler)
    client = httpx.Client(transport=transport, timeout=5.0)
    return client, server


class TestRemoteZipArchive:
    @pytest.mark.asyncio
    async def test_extract_cik_json_returns_correct_document(self):
        target = {"1234567890": {"cik": "1234567890", "name": "Test Co"}}
        data = build_fake_archive_bytes(target)
        client, _server = make_client(data)
        try:
            archive = await open_remote_bulk_zip(
                "https://example-sec.test/submissions.zip",
                user_agent="test-agent",
                rate_limit_per_second=0,  # no artificial delay in tests
                client=client,
            )
            doc = archive.extract_cik_json("1234567890")
            assert doc == {"cik": "1234567890", "name": "Test Co"}
        finally:
            client.close()

    @pytest.mark.asyncio
    async def test_missing_cik_returns_none(self):
        data = build_fake_archive_bytes({"1234567890": {"cik": "1234567890"}})
        client, _server = make_client(data)
        try:
            archive = await open_remote_bulk_zip(
                "https://example-sec.test/submissions.zip",
                user_agent="test-agent",
                rate_limit_per_second=0,
                client=client,
            )
            assert archive.extract_cik_json("0000000000") is None
        finally:
            client.close()

    @pytest.mark.asyncio
    async def test_list_available_ciks_includes_all_entries(self):
        target = {"1111111111": {"cik": "1111111111"}, "2222222222": {"cik": "2222222222"}}
        data = build_fake_archive_bytes(target, noise_entries=5)
        client, _server = make_client(data)
        try:
            archive = await open_remote_bulk_zip(
                "https://example-sec.test/companyfacts.zip",
                user_agent="test-agent",
                rate_limit_per_second=0,
                client=client,
            )
            ciks = archive.list_available_ciks()
            assert {"1111111111", "2222222222"}.issubset(ciks)
            assert len(ciks) == 5 + 2
        finally:
            client.close()

    @pytest.mark.asyncio
    async def test_extraction_transfers_far_fewer_bytes_than_full_archive(self):
        """The central claim: extracting one CIK out of thousands never
        transfers anywhere close to the full archive size. Uses a
        multi-megabyte, low-compressibility synthetic archive so the ratio
        is representative of a real multi-GB archive rather than dominated
        by the internal minimum-range-chunk floor."""

        target = {"1234567890": {"cik": "1234567890", "name": "Test Co"}}
        data = build_fake_archive_bytes(target, noise_entries=3000, noise_padding_bytes=4000)
        assert len(data) > 5 * 1024 * 1024  # sanity: comfortably bigger than the internal chunk floor
        client, _server = make_client(data)
        try:
            archive = await open_remote_bulk_zip(
                "https://example-sec.test/submissions.zip",
                user_agent="test-agent",
                rate_limit_per_second=0,
                client=client,
            )
            archive.extract_cik_json("1234567890")
            assert archive.bytes_fetched < len(data) * 0.1
        finally:
            client.close()

    @pytest.mark.asyncio
    async def test_extract_universe_cik_documents_batches_multiple_ciks(self):
        target = {
            "1111111111": {"cik": "1111111111", "field": "a"},
            "2222222222": {"cik": "2222222222", "field": "b"},
        }
        data = build_fake_archive_bytes(target, noise_entries=50)
        client, _server = make_client(data)
        try:
            archive = await open_remote_bulk_zip(
                "https://example-sec.test/submissions.zip",
                user_agent="test-agent",
                rate_limit_per_second=0,
                client=client,
            )
            result = await extract_universe_cik_documents(archive, ["1111111111", "2222222222", "0000000000"])
            assert result == target  # missing CIK simply absent, not an error
        finally:
            client.close()

    @pytest.mark.asyncio
    async def test_range_unsupported_host_raises_clear_error(self):
        data = build_fake_archive_bytes({"1234567890": {"cik": "1234567890"}})
        client, _server = make_client(data, support_ranges=False)
        try:
            with pytest.raises(RemoteRangeNotSupportedError):
                await open_remote_bulk_zip(
                    "https://example-sec.test/submissions.zip",
                    user_agent="test-agent",
                    rate_limit_per_second=0,
                    client=client,
                )
        finally:
            client.close()

    @pytest.mark.asyncio
    async def test_accept_encoding_identity_header_sent(self):
        """Range offsets are only meaningful against the exact on-disk bytes —
        a transparently re-compressed response would corrupt every offset."""

        seen_headers: list[dict] = []
        data = build_fake_archive_bytes({"1234567890": {"cik": "1234567890"}})

        def handler(request: httpx.Request) -> httpx.Response:
            seen_headers.append(dict(request.headers))
            server = RangeServingTransport(data)
            return server.handler(request)

        client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)
        try:
            await open_remote_bulk_zip(
                "https://example-sec.test/submissions.zip",
                user_agent="test-agent",
                rate_limit_per_second=0,
                client=client,
            )
            assert any(h.get("accept-encoding") == "identity" for h in seen_headers)
        finally:
            client.close()


class TestRemoteZipArchiveDirectConstruction:
    def test_direct_construction_synchronous(self):
        """RemoteZipArchive itself is a plain sync class (the async wrapper is
        only for keeping the blocking work off the event loop)."""

        data = build_fake_archive_bytes({"1234567890": {"cik": "1234567890", "x": 1}})
        client, _server = make_client(data)
        try:
            archive = RemoteZipArchive(client, "https://example-sec.test/submissions.zip", {}, rate_limit_per_second=0)
            assert archive.extract_cik_json("1234567890") == {"cik": "1234567890", "x": 1}
        finally:
            client.close()
