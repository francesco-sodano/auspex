from __future__ import annotations

from auspex.collectors.insider_collector import InsiderCollector
from auspex.persistence.memory import InMemoryDocumentSink, InMemoryWatermarkStore


class FakeEdgar:
    def __init__(self, filing_date: str) -> None:
        self.filing_date = filing_date
        self.form4_calls = 0

    async def get_submissions(self, cik: str) -> dict:
        return {
            "filings": {
                "recent": {
                    "form": ["4"],
                    "accessionNumber": ["0001-26-000001"],
                    "filingDate": [self.filing_date],
                    "primaryDocument": ["form4.xml"],
                }
            }
        }

    async def get_form4_xml(
        self, cik: str, accession_no_dashes: str, filename: str
    ) -> str:
        self.form4_calls += 1
        return "<ownershipDocument />"


async def test_date_watermark_skips_already_processed_form4() -> None:
    watermarks = InMemoryWatermarkStore()
    await watermarks.set_watermark("insider:security-1", "2026-02-10")
    edgar = FakeEdgar("2026-02-10")
    collector = InsiderCollector(edgar, InMemoryDocumentSink(), watermarks)

    result = await collector.collect("security-1", "0000000001")

    assert result.items_seen == 0
    assert edgar.form4_calls == 0


async def test_successful_collection_advances_watermark_by_filing_date() -> None:
    watermarks = InMemoryWatermarkStore()
    edgar = FakeEdgar("2026-02-10")
    collector = InsiderCollector(edgar, InMemoryDocumentSink(), watermarks)

    result = await collector.collect("security-1", "0000000001")

    assert result.items_written == 1
    assert await watermarks.get_watermark("insider:security-1") == "2026-02-10"
