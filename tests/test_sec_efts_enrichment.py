import os
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

TESTS_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_ROOT))

from test_connectors import (
    FakeBronzeWriter,
    FakeControlPlane,
    FakeHttpResponse,
    RunContext,
    Sec13DgConnector,
    Sec13FConnector,
    Sec8KConnector,
    SecS1Connector,
    Watermark,
)


_INDEX_WITH_FILTERED_DOCUMENTS = """<html><body>
<table class="tableFile" summary="Document Format Files">
<tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
<tr><td>1</td><td>FORM 8-K</td><td><a href="/ix?doc=/Archives/edgar/data/2222222/000111111126000001/issuer-8k.htm">issuer-8k.htm iXBRL</a></td><td>8-K</td><td>12000</td></tr>
<tr><td>2</td><td>PRESS RELEASE</td><td><a href="release.pdf">release.pdf</a></td><td>EX-99.1</td><td>45000</td></tr>
<tr><td>3</td><td>GRAPHIC</td><td><a href="logo.jpg">logo.jpg</a></td><td>GRAPHIC</td><td>9000</td></tr>
</table>
</body></html>"""

_INDEX_WITH_13F_DOCUMENTS = """<html><body>
<table class="tableFile" summary="Document Format Files">
<tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
<tr><td>1</td><td>13F COVER PAGE</td><td><a href="xslForm13F_X02/primary_doc.xml">primary_doc.html</a></td><td>13F-HR</td><td></td></tr>
<tr><td>1</td><td>13F COVER PAGE</td><td><a href="primary_doc.xml">primary_doc.xml</a></td><td>13F-HR</td><td>8000</td></tr>
<tr><td>2</td><td>INFORMATION TABLE</td><td><a href="xslForm13F_X02/information_table.xml">information_table.html</a></td><td>INFORMATION TABLE</td><td></td></tr>
<tr><td>2</td><td>INFORMATION TABLE</td><td><a href="information_table.xml">information_table.xml</a></td><td>INFORMATION TABLE</td><td>24000</td></tr>
</table>
</body></html>"""


class SecEftsEnrichmentTests(unittest.TestCase):
    def setUp(self):
        os.environ["EDGAR_USER_AGENT"] = "Auspex test@example.com"
        self.today = date.today().isoformat()

    def test_schedule_query_requires_complete_efts_hit_count(self):
        connector = Sec13DgConnector(
            FakeControlPlane(), FakeBronzeWriter(),
            source_config={"rate_limit": {"requests_per_minute": 100000}},
            since_date=self.today, to_date=self.today,
        )
        payload = {
            "hits": {
                "total": {"value": 2, "relation": "eq"},
                "hits": [{"_source": {"form": "SCHEDULE 13D", "adsh": "a"}}],
            }
        }
        with patch(
            "shared.sec_efts_connector.http_get", return_value=FakeHttpResponse(payload)
        ):
            with self.assertRaisesRegex(RuntimeError, "pagination is incomplete"):
                connector._fetch_window(
                    "SCHEDULE 13D", {"SCHEDULE 13D", "SCHEDULE 13D/A"},
                    self.today, self.today, {"User-Agent": os.environ["EDGAR_USER_AGENT"]},
                )

    def test_schedule_query_records_exhaustive_window_audit(self):
        connector = Sec13DgConnector(
            FakeControlPlane(), FakeBronzeWriter(),
            source_config={"rate_limit": {"requests_per_minute": 100000}},
            since_date=self.today, to_date=self.today,
        )
        payload = {
            "hits": {
                "total": {"value": 1, "relation": "eq"},
                "hits": [{"_source": {"form": "SCHEDULE 13D", "adsh": "a"}}],
            }
        }
        with patch(
            "shared.sec_efts_connector.http_get", return_value=FakeHttpResponse(payload)
        ):
            rows = connector._fetch_window(
                "SCHEDULE 13D", {"SCHEDULE 13D", "SCHEDULE 13D/A"},
                self.today, self.today, {"User-Agent": os.environ["EDGAR_USER_AGENT"]},
            )

        self.assertEqual([row["adsh"] for row in rows], ["a"])
        self.assertEqual(connector.query_audits[0]["total_hits"], 1)
        self.assertEqual(connector.query_audits[0]["fetched_hits"], 1)

    def test_schedule_query_rejects_lower_bound_total(self):
        connector = Sec13DgConnector(
            FakeControlPlane(), FakeBronzeWriter(),
            source_config={"rate_limit": {"requests_per_minute": 100000}},
            since_date=self.today, to_date=self.today,
        )
        payload = {"hits": {"total": {"value": 10000, "relation": "gte"}, "hits": []}}
        with patch(
            "shared.sec_efts_connector.http_get", return_value=FakeHttpResponse(payload)
        ):
            with self.assertRaisesRegex(RuntimeError, "total is not exact"):
                connector._fetch_window(
                    "SCHEDULE 13D", {"SCHEDULE 13D", "SCHEDULE 13D/A"},
                    self.today, self.today, {"User-Agent": os.environ["EDGAR_USER_AGENT"]},
                )

    def test_schedule_query_rejects_duplicate_filing_identities(self):
        connector = Sec13DgConnector(
            FakeControlPlane(), FakeBronzeWriter(),
            source_config={"rate_limit": {"requests_per_minute": 100000}},
            since_date=self.today, to_date=self.today,
        )
        payload = {
            "hits": {
                "total": {"value": 2, "relation": "eq"},
                "hits": [
                    {"_source": {"form": "SCHEDULE 13D", "adsh": "a"}},
                    {"_source": {"form": "SCHEDULE 13D", "adsh": "a"}},
                ],
            }
        }
        with patch(
            "shared.sec_efts_connector.http_get", return_value=FakeHttpResponse(payload)
        ):
            with self.assertRaisesRegex(RuntimeError, "identities do not reconcile"):
                connector._fetch_window(
                    "SCHEDULE 13D", {"SCHEDULE 13D", "SCHEDULE 13D/A"},
                    self.today, self.today, {"User-Agent": os.environ["EDGAR_USER_AGENT"]},
                )

    def test_archive_url_uses_efts_cik_not_accession_prefix(self):
        requested_urls = []
        source = {
            "adsh": "0001111111-26-000001",
            "ciks": ["0002222222"],
            "file_date": self.today,
            "form": "8-K",
        }

        def fake_http_get(url, params=None, headers=None, **kwargs):
            requested_urls.append(url)
            self.assertEqual(headers["User-Agent"], "Auspex test@example.com")
            if "Archives/edgar/data/" in url:
                self.assertEqual(headers["Accept-Encoding"], "identity")
            if "search-index" in url:
                return FakeHttpResponse({
                    "hits": {
                        "total": {"value": 1, "relation": "eq"},
                        "hits": [{"_source": source}],
                    }
                })
            if url.endswith("-index.html"):
                return FakeHttpResponse({}, text=_INDEX_WITH_FILTERED_DOCUMENTS)
            if url.endswith("issuer-8k.htm"):
                return FakeHttpResponse({}, text="<html><body>Item 1.01 Entry into an Agreement</body></html>")
            self.fail(f"Unexpected SEC URL: {url}")

        with patch("shared.sec_efts_connector.http_get", side_effect=fake_http_get):
            batch = Sec8KConnector(
                FakeControlPlane(),
                FakeBronzeWriter(),
                source_config={"rate_limit": {"requests_per_minute": 100000}},
                since_date=self.today,
                to_date=self.today,
            ).fetch(None)

        self.assertIn("/data/2222222/000111111126000001/", requested_urls[1])
        self.assertNotIn("/data/1111111/", requested_urls[1])
        self.assertEqual(batch.records[0]["sec_archive"]["registrant_cik"], "0002222222")
        self.assertEqual(batch.records[0]["efts_source"], source)

    def test_official_filing_url_precedes_efts_cik_candidates(self):
        requested_urls = []
        source = {
            "adsh": "0001111111-26-000001",
            "ciks": ["0009999999"],
            "file_date": self.today,
            "form": "S-1",
            "filing_url": "https://www.sec.gov/Archives/edgar/data/2222222/000111111126000001/issuer-s1.htm",
        }

        def fake_http_get(url, params=None, headers=None, **kwargs):
            requested_urls.append(url)
            if "search-index" in url:
                return FakeHttpResponse({"hits": {"hits": [{"_source": source}]}})
            if url.endswith("-index.html"):
                return FakeHttpResponse({}, text=_INDEX_WITH_FILTERED_DOCUMENTS.replace("FORM 8-K", "FORM S-1").replace(">8-K<", ">S-1<"))
            if url.endswith("issuer-8k.htm"):
                return FakeHttpResponse({}, text="<html><body>Registration statement</body></html>")
            self.fail(f"Unexpected SEC URL: {url}")

        with patch("shared.sec_efts_connector.http_get", side_effect=fake_http_get):
            batch = SecS1Connector(
                FakeControlPlane(),
                FakeBronzeWriter(),
                source_config={"rate_limit": {"requests_per_minute": 100000}},
                since_date=self.today,
                to_date=self.today,
            ).fetch(None)

        archive_index_url = next(url for url in requested_urls if url.endswith("-index.html"))
        self.assertIn("/data/2222222/000111111126000001/", archive_index_url)
        self.assertNotIn("/data/9999999/", " ".join(requested_urls))
        self.assertEqual(batch.records[0]["sec_archive"]["registrant_cik"], "0002222222")

    def test_document_filtering_downloads_only_primary_text_document(self):
        requested_urls = []
        source = {
            "adsh": "0001111111-26-000001",
            "ciks": ["0002222222"],
            "file_date": self.today,
            "form": "8-K",
        }

        def fake_http_get(url, params=None, headers=None, **kwargs):
            requested_urls.append(url)
            if "search-index" in url:
                return FakeHttpResponse({"hits": {"hits": [{"_source": source}]}})
            if url.endswith("-index.html"):
                return FakeHttpResponse({}, text=_INDEX_WITH_FILTERED_DOCUMENTS)
            if url.endswith("issuer-8k.htm"):
                return FakeHttpResponse({}, text="<html><body>Item 2.02 Results of Operations</body></html>")
            self.fail(f"Binary or unselected document was requested: {url}")

        with patch("shared.sec_efts_connector.http_get", side_effect=fake_http_get):
            batch = Sec8KConnector(
                FakeControlPlane(),
                FakeBronzeWriter(),
                source_config={"rate_limit": {"requests_per_minute": 100000}},
                since_date=self.today,
                to_date=self.today,
            ).fetch(None)

        archive = batch.records[0]["sec_archive"]
        self.assertEqual([url for url in requested_urls if url.endswith((".htm", ".pdf", ".jpg"))], [archive["primary_document"]["url"]])
        self.assertNotIn("/ix?doc=", archive["primary_document"]["url"])
        self.assertEqual(archive["item_codes"], ["2.02"])
        self.assertNotIn("release.pdf", " ".join(requested_urls))
        self.assertNotIn("logo.jpg", " ".join(requested_urls))

    def test_archive_document_url_allows_cross_cik_same_accession_only(self):
        connector = Sec8KConnector(
            FakeControlPlane(),
            FakeBronzeWriter(),
            source_config={"rate_limit": {"requests_per_minute": 100000}},
            since_date=self.today,
            to_date=self.today,
        )
        index_url = "https://www.sec.gov/Archives/edgar/data/928139/000119312524044545/0001193125-24-044545-index.html"

        self.assertEqual(
            connector._archive_document_url(
                index_url,
                "/Archives/edgar/data/1629222/000119312524044545/d748411dsc13d.htm",
            ),
            "https://www.sec.gov/Archives/edgar/data/1629222/000119312524044545/d748411dsc13d.htm",
        )
        self.assertIsNone(
            connector._archive_document_url(
                index_url,
                "/Archives/edgar/data/1629222/000119312524044546/different-filing.htm",
            )
        )

    def test_13f_information_table_xml_is_landed_unchanged(self):
        fixture = (TESTS_ROOT / "fixtures" / "sec_13f_information_table.xml").read_text(encoding="utf-8")
        source = {
            "adsh": "0003333333-26-000007",
            "ciks": ["0004444444"],
            "file_date": self.today,
            "form": "13F-HR",
        }

        def fake_http_get(url, params=None, headers=None, **kwargs):
            if "search-index" in url:
                return FakeHttpResponse({"hits": {"hits": [{"_source": source}]}})
            if url.endswith("-index.html"):
                return FakeHttpResponse({}, text=_INDEX_WITH_13F_DOCUMENTS)
            if url.endswith("primary_doc.xml"):
                return FakeHttpResponse({}, text="<edgarSubmission><submissionType>13F-HR</submissionType></edgarSubmission>")
            if url.endswith("information_table.xml"):
                return FakeHttpResponse({}, text=fixture)
            self.fail(f"Unexpected SEC URL: {url}")

        with patch("shared.sec_efts_connector.http_get", side_effect=fake_http_get):
            batch = Sec13FConnector(
                FakeControlPlane(),
                FakeBronzeWriter(),
                source_config={"rate_limit": {"requests_per_minute": 100000}},
                since_date=self.today,
                to_date=self.today,
            ).fetch(None)

        archive = batch.records[0]["sec_archive"]
        self.assertEqual(archive["information_table_xml"]["content"], fixture)
        self.assertEqual(archive["information_table_xml"]["document_type"], "INFORMATION TABLE")
        self.assertTrue(archive["information_table_xml"]["url"].endswith("/information_table.xml"))
        self.assertNotIn("/xsl", archive["information_table_xml"]["url"])
        self.assertEqual(archive["missing_document_classes"], [])

    def test_13dg_xml_exposes_subject_reporting_owner_and_percentage(self):
        source = {
            "adsh": "0005555555-26-000003",
            "ciks": ["0006666666"],
            "file_date": self.today,
            "form": "SC 13G",
        }
        ownership_xml = """<?xml version="1.0"?>
<ownershipDocument>
  <subjectCompany><issuerCik>0007777777</issuerCik><issuerName>SUBJECT CORP</issuerName><classTitle>Common Stock</classTitle><cusip>123456789</cusip></subjectCompany>
  <reportingOwner><reportingOwnerId><rptOwnerCik>0006666666</rptOwnerCik><rptOwnerName>REPORTING FUND LP</rptOwnerName></reportingOwnerId><percentOfClassRepresentedByAmount>7.4</percentOfClassRepresentedByAmount></reportingOwner>
</ownershipDocument>"""
        index_html = (
            _INDEX_WITH_FILTERED_DOCUMENTS
            .replace("FORM 8-K", "SCHEDULE 13G")
            .replace(">8-K<", ">SC 13G<")
            .replace("/data/2222222/000111111126000001/issuer-8k.htm", "/data/6666666/000555555526000003/schedule13g.xml")
            .replace("issuer-8k.htm iXBRL", "schedule13g.xml")
        )

        def fake_http_get(url, params=None, headers=None, **kwargs):
            if "search-index" in url:
                return FakeHttpResponse({
                    "hits": {
                        "total": {"value": 1, "relation": "eq"},
                        "hits": [{"_source": source}],
                    }
                })
            if url.endswith("-index.html"):
                return FakeHttpResponse({}, text=index_html)
            if url.endswith("schedule13g.xml"):
                return FakeHttpResponse({}, text=ownership_xml)
            self.fail(f"Unexpected SEC URL: {url}")

        with patch("shared.sec_efts_connector.http_get", side_effect=fake_http_get):
            batch = Sec13DgConnector(
                FakeControlPlane(),
                FakeBronzeWriter(),
                source_config={"rate_limit": {"requests_per_minute": 100000}},
                since_date=self.today,
                to_date=self.today,
            ).fetch(None)

        archive = batch.records[0]["sec_archive"]
        self.assertEqual(archive["subject_issuer"]["cik"], "0007777777")
        self.assertEqual(archive["subject_issuer"]["name"], "SUBJECT CORP")
        self.assertEqual(archive["filer_cik"], "0006666666")
        self.assertIsNone(archive["registrant_cik"])
        self.assertEqual(archive["reporting_owners"], [{
            "cik": "0006666666",
            "name": "REPORTING FUND LP",
            "percent_owned": "7.4",
        }])

    def test_13dg_html_pairs_cover_page_and_reporting_owner_values(self):
        connector = Sec13DgConnector(
            FakeControlPlane(),
            FakeBronzeWriter(),
            source_config={"rate_limit": {"requests_per_minute": 100000}},
            since_date=self.today,
            to_date=self.today,
        )
        content = """<html><body>
<div>GrafTech International Ltd.</div><div>(Name of Issuer)</div>
<div>Common Stock, $0.01 par value per share</div><div>(Title of Class of Securities)</div>
<div>384313508</div><div>(CUSIP Number)</div>
<div>1</div><div>NAME OF REPORTING PERSON</div>
<div>I.R.S. IDENTIFICATION NO. OF ABOVE PERSON (ENTITIES ONLY)</div><div>Nilesh Undavia</div>
<div>2</div><div>CHECK THE APPROPRIATE BOX IF A MEMBER OF A GROUP</div>
<div>13</div><div>PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW (11)</div><div>5.74%</div>
</body></html>"""

        subject, owners = connector._extract_13dg_html(content)

        self.assertEqual(subject, {
            "name": "GrafTech International Ltd.",
            "class_title": "Common Stock, $0.01 par value per share",
            "cusip": "384313508",
        })
        self.assertEqual(owners, [{"name": "Nilesh Undavia", "percent_owned": "5.74%"}])

    def test_13dg_current_xml_reads_submission_level_issuer(self):
        connector = Sec13DgConnector(
            FakeControlPlane(), FakeBronzeWriter(),
            source_config={"rate_limit": {"requests_per_minute": 100000}},
            since_date=self.today, to_date=self.today,
        )
        subject, _ = connector._extract_13dg_xml("""
            <edgarSubmission xmlns="http://www.sec.gov/edgar/schedule13g">
              <headerData><filerInfo><filer><credentials><cik>0001390777</cik></credentials></filer></filerInfo></headerData>
              <formData><coverPageHeader><issuerCik>0000048898</issuerCik>
                <issuerName>HUBBELL INC</issuerName><issuerCusip>443510607</issuerCusip>
              </coverPageHeader></formData>
            </edgarSubmission>
        """)
        self.assertEqual(subject, {
            "cik": "0000048898", "name": "HUBBELL INC", "cusip": "443510607",
        })

    def test_13dg_submission_header_extracts_exact_subject(self):
        connector = Sec13DgConnector(
            FakeControlPlane(), FakeBronzeWriter(),
            source_config={"rate_limit": {"requests_per_minute": 100000}},
            since_date=self.today, to_date=self.today,
        )
        content = """SUBJECT COMPANY:\n\n\tCOMPANY DATA:\n\t\tCOMPANY CONFORMED NAME:\t\t\tMonopar Therapeutics\n\t\tCENTRAL INDEX KEY:\t\t\t0001645469\n\nFILED BY:\n"""
        block = connector._submission_subject_block(content)
        self.assertEqual(connector._extract_submission_subject(block), {
            "cik": "0001645469", "name": "Monopar Therapeutics",
        })

    def test_13dg_enrichment_uses_exact_submission_subject_header(self):
        connector = Sec13DgConnector(
            FakeControlPlane(), FakeBronzeWriter(),
            source_config={"rate_limit": {"requests_per_minute": 100000}},
            since_date=self.today, to_date=self.today,
        )
        source = {
            "adsh": "0005555555-26-000003",
            "ciks": ["0006666666"],
            "file_date": self.today,
            "form": "SCHEDULE 13G",
        }
        index_html = (
            _INDEX_WITH_FILTERED_DOCUMENTS
            .replace("FORM 8-K", "SCHEDULE 13G")
            .replace(">8-K<", ">SCHEDULE 13G<")
            .replace(
                "/data/2222222/000111111126000001/issuer-8k.htm",
                "/data/6666666/000555555526000003/schedule13g.htm",
            )
            .replace("issuer-8k.htm iXBRL", "schedule13g.htm")
        )
        primary_html = """<html><body>
<div>Monopar Therapeutics</div><div>(Name of Issuer)</div>
<div>Common Stock</div><div>(Title of Class of Securities)</div>
<div>61023L108</div><div>(CUSIP Number)</div>
</body></html>"""
        submission_text = """SUBJECT COMPANY:

	COMPANY DATA:
		COMPANY CONFORMED NAME:\t\t\tMonopar Therapeutics
		CENTRAL INDEX KEY:\t\t\t0001645469

FILED BY:
"""

        def fake_http_get(url, **_kwargs):
            if url.endswith("-index.html"):
                return FakeHttpResponse({}, text=index_html)
            if url.endswith("schedule13g.htm"):
                return FakeHttpResponse({}, text=primary_html)
            if url.endswith("0005555555-26-000003.txt"):
                return FakeHttpResponse({}, text=submission_text)
            self.fail(f"Unexpected SEC URL: {url}")

        with patch("shared.sec_efts_connector.http_get", side_effect=fake_http_get):
            enriched = connector._enrich_filing(
                source, {"User-Agent": os.environ["EDGAR_USER_AGENT"]}
            )

        archive = enriched["sec_archive"]
        self.assertEqual(archive["subject_issuer"]["cik"], "0001645469")
        self.assertEqual(archive["subject_issuer"]["name"], "Monopar Therapeutics")
        self.assertEqual(archive["submission_header"]["fetch_status"], "ok")
        self.assertEqual(len(archive["submission_header"]["content_sha256"]), 64)

    def test_13dg_html_handles_split_issuer_and_percentage_heading(self):
        connector = Sec13DgConnector(
            FakeControlPlane(),
            FakeBronzeWriter(),
            source_config={"rate_limit": {"requests_per_minute": 100000}},
            since_date=self.today,
            to_date=self.today,
        )
        content = """<html><body>
<div>Heliogen,</div><div>Inc.</div><div>(Name of Issuer)</div>
<div>Common Stock</div><div>(Title of Class of Securities)</div>
<div>42329E105</div><div>(CUSIP Number)</div>
<div>NAMES OF REPORTING PERSONS</div><div>Cambridge Equities, LP</div>
<div>(2)</div><div>CHECK THE APPROPRIATE BOX IF A MEMBER OF A GROUP</div>
<div>(13)</div><div>PERCENT OF CLASS REPRESENTED BY</div><div>AMOUNT IN ROW (11)</div><div>7.49%</div>
</body></html>"""

        subject, owners = connector._extract_13dg_html(content)

        self.assertEqual(subject["name"], "Heliogen, Inc.")
        self.assertEqual(owners, [{"name": "Cambridge Equities, LP", "percent_owned": "7.49%"}])

    def test_missing_primary_document_lands_terminal_quarantine_evidence(self):
        source = {
            "adsh": "0001111111-26-000001",
            "ciks": ["0002222222"],
            "file_date": (date.today() - timedelta(days=31)).isoformat(),
            "form": "8-K",
        }
        index_without_primary = _INDEX_WITH_FILTERED_DOCUMENTS.replace(">8-K<", ">EX-99.1<")

        def fake_http_get(url, params=None, headers=None, **kwargs):
            if "search-index" in url:
                return FakeHttpResponse({"hits": {"hits": [{"_source": source}]}})
            if url.endswith("-index.html"):
                return FakeHttpResponse({}, text=index_without_primary)
            self.fail(f"No document should be downloaded: {url}")

        with patch("shared.sec_efts_connector.http_get", side_effect=fake_http_get):
            batch = Sec8KConnector(
                FakeControlPlane(),
                FakeBronzeWriter(),
                source_config={"rate_limit": {"requests_per_minute": 100000}},
                since_date=self.today,
                to_date=self.today,
            ).fetch(None)

        archive = batch.records[0]["sec_archive"]
        self.assertEqual(archive["archive_status"], "terminal_incomplete")
        self.assertEqual(archive["missing_document_classes"], ["primary_document"])

    def test_recent_missing_primary_document_remains_retryable(self):
        source = {
            "adsh": "0001111111-26-000001",
            "ciks": ["0002222222"],
            "file_date": self.today,
            "form": "8-K",
        }
        index_without_primary = _INDEX_WITH_FILTERED_DOCUMENTS.replace(">8-K<", ">EX-99.1<")

        def fake_http_get(url, params=None, headers=None, **kwargs):
            if "search-index" in url:
                return FakeHttpResponse({"hits": {"hits": [{"_source": source}]}})
            if url.endswith("-index.html"):
                return FakeHttpResponse({}, text=index_without_primary)
            self.fail(f"Unexpected SEC URL: {url}")

        with patch("shared.sec_efts_connector.http_get", side_effect=fake_http_get):
            with self.assertRaisesRegex(RuntimeError, "SEC archive evidence incomplete"):
                Sec8KConnector(
                    FakeControlPlane(),
                    FakeBronzeWriter(),
                    source_config={"rate_limit": {"requests_per_minute": 100000}},
                    since_date=self.today,
                    to_date=self.today,
                ).fetch(None)

    def test_recent_missing_filing_index_remains_retryable(self):
        source = {
            "adsh": "0001111111-26-000001",
            "file_date": self.today,
            "form": "8-K",
        }

        def fake_http_get(url, params=None, headers=None, **kwargs):
            if "search-index" in url:
                return FakeHttpResponse({"hits": {"hits": [{"_source": source}]}})
            self.fail(f"No archive candidate should be requested: {url}")

        with patch("shared.sec_efts_connector.http_get", side_effect=fake_http_get):
            with self.assertRaisesRegex(RuntimeError, "SEC archive evidence incomplete"):
                Sec8KConnector(
                    FakeControlPlane(),
                    FakeBronzeWriter(),
                    source_config={"rate_limit": {"requests_per_minute": 100000}},
                    since_date=self.today,
                    to_date=self.today,
                ).fetch(None)

    def test_mature_missing_filing_index_lands_terminal_evidence(self):
        source = {
            "adsh": "0001111111-26-000001",
            "file_date": (date.today() - timedelta(days=31)).isoformat(),
            "form": "8-K",
        }

        def fake_http_get(url, params=None, headers=None, **kwargs):
            if "search-index" in url:
                return FakeHttpResponse({"hits": {"hits": [{"_source": source}]}})
            self.fail(f"No archive candidate should be requested: {url}")

        with patch("shared.sec_efts_connector.http_get", side_effect=fake_http_get):
            batch = Sec8KConnector(
                FakeControlPlane(),
                FakeBronzeWriter(),
                source_config={"rate_limit": {"requests_per_minute": 100000}},
                since_date=self.today,
                to_date=self.today,
            ).fetch(None)

        self.assertEqual(batch.records[0]["sec_archive"]["archive_status"], "terminal_incomplete")

    def test_primary_document_404_is_retryable_page_failure(self):
        source = {
            "adsh": "0001111111-26-000001",
            "ciks": ["0002222222"],
            "file_date": self.today,
            "form": "8-K",
        }

        class MissingDocumentError(RuntimeError):
            def __init__(self):
                super().__init__("SEC document returned 404")
                self.response = type("Response", (), {"status_code": 404})()

        def fake_http_get(url, params=None, headers=None, **kwargs):
            if "search-index" in url:
                return FakeHttpResponse({"hits": {"hits": [{"_source": source}]}})
            if url.endswith("-index.html"):
                return FakeHttpResponse({}, text=_INDEX_WITH_FILTERED_DOCUMENTS)
            if url.endswith("issuer-8k.htm"):
                raise MissingDocumentError()
            self.fail(f"Unexpected SEC URL: {url}")

        control_plane = FakeControlPlane()
        bronze_writer = FakeBronzeWriter()
        with patch("shared.sec_efts_connector.http_get", side_effect=fake_http_get):
            result = Sec8KConnector(
                control_plane,
                bronze_writer,
                source_config={"rate_limit": {"requests_per_minute": 100000}},
                since_date=self.today,
                to_date=self.today,
            ).run(RunContext(run_id="run-missing-document", source_id="sec_8k"))

        self.assertEqual(result.status, "failed")
        self.assertIn("not yet available", result.error)
        self.assertEqual(bronze_writer.writes, [])
        self.assertIsNone(control_plane.watermark)

    def test_filing_pages_are_deterministic_and_hold_watermark_until_final_page(self):
        sources = [
            {
                "adsh": f"0001111111-26-{index:06d}",
                "ciks": ["0002222222"],
                "file_date": self.today,
                "form": "8-K",
            }
            for index in range(1, 4)
        ]

        def fake_http_get(url, params=None, headers=None, **kwargs):
            if "search-index" in url:
                return FakeHttpResponse({"hits": {"hits": [{"_source": source} for source in reversed(sources)]}})
            if url.endswith("-index.html"):
                accession = url.rsplit("/", 2)[-2]
                return FakeHttpResponse(
                    {},
                    text=_INDEX_WITH_FILTERED_DOCUMENTS.replace(
                        "000111111126000001", accession,
                    ),
                )
            if url.endswith("issuer-8k.htm"):
                return FakeHttpResponse({}, text="<html><body>Item 1.01 Agreement</body></html>")
            self.fail(f"Unexpected SEC URL: {url}")

        with patch("shared.sec_efts_connector.http_get", side_effect=fake_http_get):
            first_page = Sec8KConnector(
                FakeControlPlane(), FakeBronzeWriter(),
                source_config={"rate_limit": {"requests_per_minute": 100000}},
                since_date=self.today, to_date=self.today,
                filing_offset=0, filing_limit=2,
            ).fetch(None)
            final_page = Sec8KConnector(
                FakeControlPlane(),
                FakeBronzeWriter(),
                source_config={"rate_limit": {"requests_per_minute": 100000}},
                since_date=self.today,
                to_date=self.today,
                filing_offset=2,
                filing_limit=2,
            ).fetch(None)

        self.assertEqual([record["adsh"] for record in first_page.records], [
            "0001111111-26-000001", "0001111111-26-000002",
        ])
        self.assertTrue(first_page.has_more)
        self.assertEqual([record["adsh"] for record in final_page.records], ["0001111111-26-000003"])
        self.assertFalse(final_page.has_more)
        self.assertNotIn("filings-", first_page.window)
        self.assertIn("offset-0-limit-2", first_page.window)
        self.assertIn("offset-2-limit-2", final_page.window)

    def test_page_window_is_stable_when_provider_result_count_changes(self):
        connector = Sec8KConnector(
            FakeControlPlane(), FakeBronzeWriter(),
            source_config={"rate_limit": {"requests_per_minute": 100000}},
            since_date=self.today, to_date=self.today,
            filing_offset=0, filing_limit=2,
        )
        with patch.object(connector, "_fetch_window", return_value=[]):
            empty_window = connector.fetch(None).window
        source = {
            "adsh": "0001111111-26-000001", "ciks": ["0002222222"],
            "file_date": self.today, "form": "8-K",
        }
        with patch.object(connector, "_fetch_window", return_value=[source]), patch.object(
            connector,
            "_enrich_filing",
            return_value={**source, "sec_archive": {"archive_status": "complete"}},
        ):
            changed_window = connector.fetch(None).window
        self.assertEqual(empty_window, changed_window)

    def test_malformed_efts_success_payload_falls_back_instead_of_becoming_empty(self):
        connector = Sec8KConnector(
            FakeControlPlane(), FakeBronzeWriter(),
            source_config={"rate_limit": {"requests_per_minute": 100000}},
            since_date=self.today, to_date=self.today,
        )
        with patch("shared.sec_efts_connector.http_get", return_value=FakeHttpResponse({"unexpected": []})), patch.object(
            connector, "_fetch_browse_edgar", side_effect=RuntimeError("fallback invoked"),
        ):
            with self.assertRaisesRegex(RuntimeError, "fallback invoked"):
                connector.fetch(None)

    def test_enriched_connector_schema_versions_are_two(self):
        self.assertEqual({
            Sec13FConnector.schema_version,
            Sec13DgConnector.schema_version,
            Sec8KConnector.schema_version,
            SecS1Connector.schema_version,
        }, {2})

    def test_transient_archive_failure_does_not_write_or_advance_watermark(self):
        control_plane = FakeControlPlane()
        control_plane.watermark = Watermark(
            source_id="sec_8k",
            last_event_ts=self.today,
            last_cursor=self.today,
        )
        bronze_writer = FakeBronzeWriter()
        source = {
            "adsh": "0001111111-26-000001",
            "ciks": ["0002222222"],
            "file_date": self.today,
            "form": "8-K",
        }

        def fake_http_get(url, params=None, headers=None, **kwargs):
            if "search-index" in url:
                return FakeHttpResponse({"hits": {"hits": [{"_source": source}]}})
            raise RuntimeError("SEC archive 503 after retries")

        with patch("shared.sec_efts_connector.http_get", side_effect=fake_http_get):
            result = Sec8KConnector(
                control_plane,
                bronze_writer,
                source_config={"rate_limit": {"requests_per_minute": 100000}},
                since_date=self.today,
                to_date=self.today,
            ).run(RunContext(run_id="run-archive-failure", source_id="sec_8k"))

        self.assertEqual(result.status, "failed")
        self.assertIn("503", result.error)
        self.assertEqual(bronze_writer.writes, [])
        self.assertEqual(control_plane.watermark.last_cursor, self.today)


if __name__ == "__main__":
    unittest.main()