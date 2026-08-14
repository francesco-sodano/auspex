from __future__ import annotations

from datetime import date

import pytest

from auspex.providers.edgar import (
    latest_accession_for_forms,
    latest_filing_date_for_forms,
)
from auspex.providers.edgar_bulk import BulkEdgarSource

SUBMISSIONS = {
    "filings": {
        "recent": {
            "form": ["4", "10-Q", "4", "8-K"],
            "accessionNumber": [
                "0001-24-000001",
                "0001-25-000002",
                "0001-26-000003",
                "0001-26-000004",
            ],
            "filingDate": [
                "2024-01-10",
                "2025-04-10",
                "2026-02-10",
                "2026-03-10",
            ],
        }
    }
}


def test_latest_accession_filters_forms_and_cutoff() -> None:
    assert latest_accession_for_forms(SUBMISSIONS, {"4"}) == "0001-26-000003"
    assert latest_accession_for_forms(
        SUBMISSIONS,
        {"4"},
        filed_before=date(2025, 1, 1),
    ) == "0001-24-000001"
    assert latest_accession_for_forms(SUBMISSIONS, {"20-F"}) is None
    assert latest_filing_date_for_forms(SUBMISSIONS, {"4"}) == date(2026, 2, 10)


@pytest.mark.asyncio
async def test_bulk_source_latest_accession_uses_unfiltered_cache() -> None:
    source = BulkEdgarSource(
        delegate=object(),
        submissions_by_cik={"0000000001": SUBMISSIONS},
        companyfacts_by_cik={},
        floor_date=date(2026, 1, 1),
    )

    assert await source.latest_accession("0000000001", {"4"}) == "0001-26-000003"
    assert await source.latest_filing_date("0000000001", {"4"}) == date(
        2026, 2, 10
    )
