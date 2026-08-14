"""`FilingCollector` — EDGAR submissions delta (arc42 §5.3).

Dedup key: ``source + source_record_id + content_hash``. Raw filing content
is stored to Blob for later section targeting and extraction (steps 8-9 of
the nightly pipeline); this collector only ingests.
"""

from __future__ import annotations

from datetime import date

from auspex.collectors.base import BlobSink, CollectorResult, DocumentSink, WatermarkStore, watermark_key
from auspex.models.common import content_hash, new_id, utc_now
from auspex.models.document import Document
from auspex.models.enums import DocumentType
from auspex.providers.edgar import EdgarClient

COLLECTOR_NAME = "filing"

FORM_TO_DOCUMENT_TYPE: dict[str, DocumentType] = {
    "10-K": DocumentType.FORM_10K,
    "10-Q": DocumentType.FORM_10Q,
    "8-K": DocumentType.FORM_8K,
    "20-F": DocumentType.FORM_20F,
    "6-K": DocumentType.FORM_6K,
    "S-1": DocumentType.FORM_S1,
}

INTERESTING_FORMS = frozenset(FORM_TO_DOCUMENT_TYPE)


class FilingCollector:
    def __init__(
        self, edgar: EdgarClient, document_sink: DocumentSink, blob_sink: BlobSink, watermarks: WatermarkStore
    ) -> None:
        self._edgar = edgar
        self._document_sink = document_sink
        self._blob_sink = blob_sink
        self._watermarks = watermarks

    async def collect(self, security_id: str, cik: str) -> CollectorResult:
        key = watermark_key(COLLECTOR_NAME, security_id)
        last_accession = await self._watermarks.get_watermark(key)

        result = CollectorResult(collector=COLLECTOR_NAME, security_id=security_id)
        try:
            submissions = await self._edgar.get_submissions(cik)
        except Exception as exc:  # noqa: BLE001
            result.degraded = True
            result.error = str(exc)
            return result

        recent = submissions.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        accessions = recent.get("accessionNumber", [])
        filing_dates = recent.get("filingDate", [])
        primary_docs = recent.get("primaryDocument", [])

        candidates = []
        for i, form in enumerate(forms):
            if form not in INTERESTING_FORMS:
                continue
            accession = accessions[i]
            if last_accession is not None and accession <= last_accession:
                continue
            candidates.append((accession, form, filing_dates[i], primary_docs[i]))

        result.items_seen = len(candidates)
        max_accession = last_accession
        for accession, form, filed_str, primary_doc in candidates:
            accession_no_dashes = accession.replace("-", "")
            try:
                text = await self._edgar.get_filing_document(cik, accession_no_dashes, primary_doc)
            except Exception as exc:  # noqa: BLE001 - skip this filing, continue the rest
                result.degraded = True
                result.error = str(exc)
                continue

            filed_date = date.fromisoformat(filed_str)
            hash_value = content_hash(text)

            existing = await self._document_sink.find_by_content_hash(security_id, hash_value)
            if existing is not None:
                result.items_skipped_duplicate += 1
            else:
                document_id = new_id()
                ext = primary_doc.rsplit(".", 1)[-1] if "." in primary_doc else "htm"
                blob_path = await self._blob_sink.upload_document_blob(security_id, document_id, ext, text)
                doc = Document(
                    id=document_id,
                    security_id=security_id,
                    source="edgar",
                    source_record_id=accession,
                    document_type=FORM_TO_DOCUMENT_TYPE[form],
                    form_type=form,
                    accession_number=accession,
                    filed_date=filed_date,
                    blob_path=blob_path,
                    content_hash=hash_value,
                    retrieved_at=utc_now(),
                    knowledge_date=filed_date,
                )
                await self._document_sink.upsert_document(doc)
                result.items_written += 1
                result.new_document_ids.append(document_id)

            if max_accession is None or accession > max_accession:
                max_accession = accession

        if max_accession is not None:
            await self._watermarks.set_watermark(key, max_accession)
        return result
