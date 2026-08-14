"""`InsiderCollector` — EDGAR Form 4 XML delta (arc42 §5.3).

Parses the standard ``ownershipDocument`` Form 4 XML into
:class:`~auspex.models.document.InsiderTransaction` rows attached to a
``Document`` of type ``4``. Only non-derivative transactions are parsed;
derivative transactions do not feed the smart-money leg (arc42 §5.5 leg 4).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

from auspex.collectors.base import CollectorResult, DocumentSink, WatermarkStore, watermark_key
from auspex.models.common import content_hash, new_id, utc_now
from auspex.models.document import Document, InsiderTransaction
from auspex.models.enums import DocumentType, Form4TransactionCode
from auspex.providers.edgar import EdgarClient

COLLECTOR_NAME = "insider"


def _text(node: ET.Element | None, path: str) -> str | None:
    if node is None:
        return None
    found = node.find(path)
    return found.text.strip() if found is not None and found.text else None


def parse_form4_xml(xml_text: str) -> list[InsiderTransaction]:
    root = ET.fromstring(xml_text)  # noqa: S314 - EDGAR-served XML, not user-supplied

    owner = root.find("reportingOwner")
    owner_name = _text(owner, "reportingOwnerId/rptOwnerName") or "UNKNOWN"
    relationship = owner.find("reportingOwnerRelationship") if owner is not None else None
    is_officer = (_text(relationship, "isOfficer") or "0") == "1"
    is_director = (_text(relationship, "isDirector") or "0") == "1"
    is_ten_pct = (_text(relationship, "isTenPercentOwner") or "0") == "1"

    transactions: list[InsiderTransaction] = []
    table = root.find("nonDerivativeTable")
    if table is None:
        return transactions

    for txn in table.findall("nonDerivativeTransaction"):
        code = _text(txn, "transactionCoding/transactionCode")
        if code is None or code not in Form4TransactionCode.__members__:
            continue
        txn_date_str = _text(txn, "transactionDate/value")
        shares_str = _text(txn, "transactionAmounts/transactionShares/value")
        price_str = _text(txn, "transactionAmounts/transactionPricePerShare/value")
        if not (txn_date_str and shares_str):
            continue
        transactions.append(
            InsiderTransaction(
                owner_name=owner_name,
                is_officer=is_officer,
                is_director=is_director,
                is_ten_percent_owner=is_ten_pct,
                transaction_code=Form4TransactionCode(code),
                transaction_date=date.fromisoformat(txn_date_str),
                shares=shares_str,
                price_per_share=price_str or "0",
            )
        )
    return transactions


class InsiderCollector:
    def __init__(self, edgar: EdgarClient, document_sink: DocumentSink, watermarks: WatermarkStore) -> None:
        self._edgar = edgar
        self._document_sink = document_sink
        self._watermarks = watermarks

    async def collect(self, security_id: str, cik: str) -> CollectorResult:
        key = watermark_key(COLLECTOR_NAME, security_id)
        watermark = await self._watermarks.get_watermark(key)
        last_filed_date = None
        if watermark is not None:
            try:
                last_filed_date = date.fromisoformat(watermark)
            except ValueError:
                pass

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
            if form != "4":
                continue
            accession = accessions[i]
            filed_date = date.fromisoformat(filing_dates[i])
            if last_filed_date is not None and filed_date <= last_filed_date:
                continue
            candidates.append((accession, filing_dates[i], primary_docs[i]))

        result.items_seen = len(candidates)
        max_filed_date = last_filed_date
        for accession, filed_str, primary_doc in candidates:
            accession_no_dashes = accession.replace("-", "")
            try:
                xml_text = await self._edgar.get_form4_xml(cik, accession_no_dashes, primary_doc)
                insider_txns = parse_form4_xml(xml_text)
            except Exception as exc:  # noqa: BLE001
                result.degraded = True
                result.error = str(exc)
                continue

            filed_date = date.fromisoformat(filed_str)
            hash_value = content_hash(xml_text)
            existing = await self._document_sink.find_by_content_hash(security_id, hash_value)
            if existing is not None:
                result.items_skipped_duplicate += 1
            else:
                document_id = new_id()
                doc = Document(
                    id=document_id,
                    security_id=security_id,
                    source="edgar",
                    source_record_id=accession,
                    document_type=DocumentType.FORM_4,
                    form_type="4",
                    accession_number=accession,
                    filed_date=filed_date,
                    content_hash=hash_value,
                    insider_transactions=insider_txns,
                    retrieved_at=utc_now(),
                    knowledge_date=filed_date,
                )
                await self._document_sink.upsert_document(doc)
                result.items_written += 1
                result.new_document_ids.append(document_id)

            if max_filed_date is None or filed_date > max_filed_date:
                max_filed_date = filed_date

        if max_filed_date is not None:
            await self._watermarks.set_watermark(key, max_filed_date.isoformat())
        return result
