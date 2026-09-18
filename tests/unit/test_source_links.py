from datetime import date

import pytest

from auspex.api.routes.securities import _source_url
from auspex.models.common import utc_now
from auspex.models.document import Document
from auspex.models.security import Security
from auspex.source_links import document_source_url, sec_filing_url

SECURITY = Security(
    id="issuer", ticker="TEST", name="Example issuer", cik="0001045810",
    cohort="test", filer_profile="DOMESTIC",
)
ACCESSION = "0001045810-26-000001"
EXPECTED = (
    "https://www.sec.gov/Archives/edgar/data/1045810/"
    "000104581026000001/0001045810-26-000001-index.html"
)


def _document(form):
    return Document(
        id="document", security_id=SECURITY.id, source="edgar", source_record_id=ACCESSION,
        document_type=form, form_type=form, accession_number=ACCESSION,
        knowledge_date=date(2026, 9, 18), content_hash="source", retrieved_at=utc_now(),
    )


@pytest.mark.parametrize("form", ["10-K", "10-Q", "8-K", "20-F", "6-K", "S-1", "4"])
def test_all_filing_types_have_a_real_accession_index_link_without_a_primary_url(form):
    document = _document(form)
    assert document.url is None
    assert document_source_url(document, SECURITY) == EXPECTED
    assert _source_url(document, SECURITY) == EXPECTED


def test_an_existing_https_source_is_preserved():
    document = _document("10-Q").model_copy(update={"url": "https://www.sec.gov/example.htm"})
    assert document_source_url(document, SECURITY) == document.url


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://bad host/path", "http://["])
def test_invalid_source_urls_do_not_become_clickable_links(url, caplog):
    document = _document("10-Q").model_copy(update={"url": url})
    assert document_source_url(document, SECURITY) == EXPECTED
    assert "source URL" in caplog.text


def test_invalid_coordinates_are_visible_and_never_invent_a_source(caplog):
    assert sec_filing_url("not-a-cik", ACCESSION) is None
    assert sec_filing_url(SECURITY.cik, "not-an-accession") is None
    assert "invalid or missing filing coordinates" in caplog.text


def test_source_issuer_must_match():
    with pytest.raises(ValueError, match="does not belong"):
        document_source_url(_document("10-Q").model_copy(update={"security_id": "another"}), SECURITY)
