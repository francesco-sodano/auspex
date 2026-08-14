"""`FundamentalCollector` — XBRL companyfacts on new 10-K/10-Q/20-F (arc42 §5.3).

Every fact carries ``accn``, ``fy``, ``fp``, ``form``, ``end``, and ``filed``
— the property that makes point-in-time reconstruction honest (arc42 §5.3
"Point-in-time fundamentals").
"""

from __future__ import annotations

from datetime import date

from auspex.collectors.base import CollectorResult, FundamentalSink, WatermarkStore, watermark_key
from auspex.models.fundamentals import FundamentalSnapshot, XbrlFact
from auspex.providers.edgar import EdgarClient

COLLECTOR_NAME = "fundamental"


def _iter_facts(company_facts: dict):
    taxonomies = company_facts.get("facts", {})
    for taxonomy in ("us-gaap", "ifrs-full", "dei"):
        for concept, concept_data in taxonomies.get(taxonomy, {}).items():
            for unit, rows in concept_data.get("units", {}).items():
                for row in rows:
                    yield taxonomy, concept, unit, row


def build_snapshots_by_accession(
    company_facts: dict, accessions: set[str] | None = None
) -> dict[str, FundamentalSnapshot]:
    """Group every XBRL fact by its reporting accession number."""

    by_accn: dict[str, list[XbrlFact]] = {}
    meta: dict[str, tuple[str, int, str, str]] = {}  # accn -> (form, fy, fp, filed)

    for taxonomy, concept, unit, row in _iter_facts(company_facts):
        accn = row.get("accn")
        if accn is None:
            continue
        if accessions is not None and accn not in accessions:
            continue
        fact = XbrlFact(
            taxonomy=taxonomy,
            concept=concept,
            unit=unit,
            value=str(row["val"]),
            accn=accn,
            fy=row["fy"],
            fp=row["fp"],
            form=row["form"],
            start=date.fromisoformat(row["start"]) if row.get("start") else None,
            end=date.fromisoformat(row["end"]),
            filed=date.fromisoformat(row["filed"]),
        )
        by_accn.setdefault(accn, []).append(fact)
        meta[accn] = (row["form"], row["fy"], row["fp"], row["filed"])

    snapshots: dict[str, FundamentalSnapshot] = {}
    for accn, facts in by_accn.items():
        form, fy, fp, filed = meta[accn]
        snapshots[accn] = FundamentalSnapshot(
            id=f"placeholder:{accn}",  # security_id prefixed by caller
            security_id="",
            accn=accn,
            form=form,
            fy=fy,
            fp=fp,
            filed=date.fromisoformat(filed),
            facts=facts,
        )
    return snapshots


class FundamentalCollector:
    def __init__(self, edgar: EdgarClient, sink: FundamentalSink, watermarks: WatermarkStore) -> None:
        self._edgar = edgar
        self._sink = sink
        self._watermarks = watermarks

    async def collect(self, security_id: str, cik: str, trigger_accessions: set[str]) -> CollectorResult:
        key = watermark_key(COLLECTOR_NAME, security_id)
        result = CollectorResult(collector=COLLECTOR_NAME, security_id=security_id)
        if not trigger_accessions:
            return result

        last_processed_raw = await self._watermarks.get_watermark(key)
        already_processed = set(last_processed_raw.split(",")) if last_processed_raw else set()
        pending = trigger_accessions - already_processed
        if not pending:
            return result

        try:
            company_facts = await self._edgar.get_company_facts(cik)
        except Exception as exc:  # noqa: BLE001
            result.degraded = True
            result.error = str(exc)
            return result

        snapshots = build_snapshots_by_accession(company_facts, pending)
        result.items_seen = len(pending)
        for accn, snapshot in snapshots.items():
            snapshot = snapshot.model_copy(update={"id": f"{security_id}:{accn}", "security_id": security_id})
            await self._sink.upsert_fundamental_snapshot(snapshot)
            result.items_written += 1
            already_processed.add(accn)

        await self._watermarks.set_watermark(key, ",".join(sorted(already_processed)))
        return result
