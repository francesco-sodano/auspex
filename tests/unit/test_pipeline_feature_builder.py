from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from auspex.collectors.fundamental_collector import build_snapshots_by_accession
from auspex.models.document import Document, InsiderTransaction
from auspex.models.enums import (
    DocumentType,
    ExtractionConfidence,
    Form4TransactionCode,
    GuidanceDirection,
    Materiality,
    Novelty,
    Sentiment,
    ThemeStrength,
)
from auspex.models.extraction import ChannelAExtraction, ThemeClaim
from auspex.models.fundamentals import FundamentalSnapshot, XbrlFact
from auspex.pipeline.feature_builder import (
    WeightsConfig,
    build_attention_events,
    build_fundamental_health_inputs,
    build_insider_events,
    build_thesis_linkage_events,
    build_valuation_metrics,
)

AS_OF = date(2026, 8, 10)
WEIGHTS = WeightsConfig(
    document_authority={
        "10-K": Decimal("1"),
        "10-Q": Decimal("0.9"),
        "8-K": Decimal("0.7"),
        "20-F": Decimal("1"),
        "6-K": Decimal("0.7"),
        "S-1": Decimal("0.8"),
        "news": Decimal("0.4"),
    },
    theme_strength={
        "STRONG": Decimal("1"),
        "MODERATE": Decimal("0.6"),
        "WEAK": Decimal("0.25"),
    },
    materiality_weight={
        "HIGH": Decimal("1"),
        "MEDIUM": Decimal("0.5"),
        "LOW": Decimal("0.2"),
        "NONE": Decimal("0"),
    },
    recency_half_life_days=Decimal(90),
    roic_tax_rate=Decimal("0.21"),
)


def _document(document_id: str, knowledge_date: date) -> Document:
    return Document(
        id=document_id,
        security_id="security-1",
        source="edgar",
        source_record_id=document_id,
        document_type=DocumentType.FORM_10K,
        form_type="10-K",
        filed_date=knowledge_date,
        content_hash=f"hash-{document_id}",
        retrieved_at=datetime.now(UTC),
        knowledge_date=knowledge_date,
    )


def _extraction(document: Document) -> ChannelAExtraction:
    return ChannelAExtraction(
        id=f"extraction-{document.id}",
        security_id=document.security_id,
        document_id=document.id,
        content_hash=document.content_hash,
        model_version="test",
        taxonomy_version="test",
        materiality=Materiality.HIGH,
        sentiment=Sentiment.POSITIVE,
        guidance_direction=GuidanceDirection.RAISED,
        novelty=Novelty.NEW_INFORMATION,
        theme_claims=[
            ThemeClaim(
                theme_id="test-theme",
                strength=ThemeStrength.STRONG,
                evidence_excerpt="Evidence",
            )
        ],
        extraction_confidence=ExtractionConfidence.HIGH,
    )


def test_attention_includes_filing_count_and_materiality() -> None:
    document = _document("past", AS_OF - timedelta(days=5))

    events = build_attention_events(
        [_extraction(document)], {document.id: document}, WEIGHTS, AS_OF
    )

    assert len(events) == 2
    assert [event.materiality_weight for event in events] == [
        Decimal(1),
        Decimal(1),
    ]


def test_feature_events_exclude_future_knowledge() -> None:
    future_document = _document("future", AS_OF + timedelta(days=1))
    extraction = _extraction(future_document)

    assert (
        build_attention_events(
            [extraction], {future_document.id: future_document}, WEIGHTS, AS_OF
        )
        == []
    )
    assert (
        build_thesis_linkage_events(
            [extraction], {future_document.id: future_document}, WEIGHTS, AS_OF
        )
        == []
    )


def test_insider_events_exclude_future_transactions() -> None:
    document = _document("form-4", AS_OF)
    document.document_type = DocumentType.FORM_4
    document.insider_transactions = [
        InsiderTransaction(
            owner_name="Owner",
            is_officer=True,
            transaction_code=Form4TransactionCode.P,
            transaction_date=AS_OF + timedelta(days=1),
            shares="10",
            price_per_share="20",
        )
    ]

    assert build_insider_events([document], AS_OF) == []


def test_ifrs_and_dei_company_facts_are_collected() -> None:
    company_facts = {
        "facts": {
            "ifrs-full": {
                "Revenue": {
                    "units": {
                        "EUR": [
                            {
                                "val": 100,
                                "accn": "accession-1",
                                "fy": 2025,
                                "fp": "FY",
                                "form": "20-F",
                                "end": "2025-12-31",
                                "filed": "2026-02-26",
                            }
                        ]
                    }
                }
            },
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {
                        "shares": [
                            {
                                "val": 10,
                                "accn": "accession-1",
                                "fy": 2025,
                                "fp": "FY",
                                "form": "20-F",
                                "end": "2025-12-31",
                                "filed": "2026-02-26",
                            }
                        ]
                    }
                }
            },
        }
    }

    snapshot = build_snapshots_by_accession(company_facts)["accession-1"]

    assert {(fact.taxonomy, fact.concept, fact.unit) for fact in snapshot.facts} == {
        ("ifrs-full", "Revenue", "EUR"),
        ("dei", "EntityCommonStockSharesOutstanding", "shares"),
    }


def test_ifrs_health_uses_native_currency_without_cross_currency_valuation() -> None:
    facts = []
    for year, revenue, gross_profit in (
        (2021, "100", "50"),
        (2022, "110", "56"),
        (2023, "120", "62"),
        (2024, "130", "68"),
        (2025, "150", "81"),
    ):
        filed = date(year + 1, 2, 26)
        facts.extend(
            [
                XbrlFact(
                    taxonomy="ifrs-full",
                    concept="Revenue",
                    unit="EUR",
                    value=revenue,
                    accn=str(year),
                    fy=year,
                    fp="FY",
                    form="20-F",
                    end=date(year, 12, 31),
                    filed=filed,
                ),
                XbrlFact(
                    taxonomy="ifrs-full",
                    concept="GrossProfit",
                    unit="EUR",
                    value=gross_profit,
                    accn=str(year),
                    fy=year,
                    fp="FY",
                    form="20-F",
                    end=date(year, 12, 31),
                    filed=filed,
                ),
            ]
        )
    facts.append(
        XbrlFact(
            taxonomy="ifrs-full",
            concept="Revenue",
            unit="USD",
            value="90",
            accn="2017",
            fy=2017,
            fp="FY",
            form="20-F",
            end=date(2017, 12, 31),
            filed=date(2018, 2, 28),
        )
    )
    snapshot = FundamentalSnapshot(
        id="security:2025",
        security_id="security",
        accn="2025",
        form="20-F",
        fy=2025,
        fp="FY",
        filed=date(2026, 2, 26),
        facts=facts,
    )
    concepts = {
        "concepts": {
            "revenues": ["Revenue"],
            "gross_profit": ["GrossProfit"],
            "net_cash_from_operations": [],
            "capex": [],
            "cash_and_equivalents": [],
            "short_term_investments": [],
            "total_debt": [],
            "total_assets": [],
            "operating_income": [],
            "stockholders_equity": [],
            "ebitda_operating_income": [],
            "depreciation_amortization": [],
        }
    }

    health = build_fundamental_health_inputs(
        [snapshot],
        concepts,
        Decimal("0.21"),
        AS_OF,
    )
    valuation = build_valuation_metrics(
        Decimal("1000"),
        [snapshot],
        concepts,
        AS_OF,
    )

    assert health.revenue_growth_yoy == Decimal("0.5")
    assert health.gross_margin_trend_slope is not None
    assert valuation.ev_sales is None
    assert valuation.ev_ebitda is None
    assert valuation.fcf_yield is None
