"""Shared pipeline fixtures for integration tests (arc42 §6.1)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from auspex.config import (
    load_cohorts,
    load_fees,
    load_label_mappings,
    load_policy,
    load_taxonomy,
    load_weights,
    load_xbrl_concepts,
)
from auspex.config.loader import load_universe
from auspex.models.common import content_hash, new_id, utc_now
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
from auspex.persistence.memory import (
    InMemoryBlobSink,
    InMemoryDocumentSink,
    InMemoryFundamentalSink,
    InMemoryFxSink,
    InMemoryPriceSink,
    InMemoryRepository,
    InMemoryWatermarkStore,
)
from auspex.pipeline.context import PipelineRepos


class InMemoryChannelASink:
    def __init__(self) -> None:
        self._items: dict[str, ChannelAExtraction] = {}

    async def find_by_cache_key(self, cache_key: str):
        for item in self._items.values():
            if item.cache_key == cache_key:
                return item
        return None

    async def upsert(self, extraction: ChannelAExtraction) -> None:
        self._items[extraction.id] = extraction

    def all(self):
        return list(self._items.values())


@pytest.fixture
def universe():
    return load_universe()


@pytest.fixture
def config_bundle():
    return {
        "weights": load_weights(),
        "policy": load_policy(),
        "xbrl_concepts": load_xbrl_concepts(),
        "label_mappings": load_label_mappings(),
        "cohorts": load_cohorts(),
        "taxonomy": load_taxonomy(),
        "fees": load_fees(),
    }


def build_repos() -> PipelineRepos:
    return PipelineRepos(
        document_sink=InMemoryDocumentSink(),
        price_sink=InMemoryPriceSink(),
        fx_sink=InMemoryFxSink(),
        fundamental_sink=InMemoryFundamentalSink(),
        blob_sink=InMemoryBlobSink(),
        watermarks=InMemoryWatermarkStore(),
        channel_a_sink=InMemoryChannelASink(),
        score_repo=InMemoryRepository(),
        leg_change_repo=InMemoryRepository(),
        recommendation_repo=InMemoryRepository(),
        run_repo=InMemoryRepository(),
        portfolio_projection_repo=InMemoryRepository(),
    )


def seed_fundamentals(repos: PipelineRepos, security_id: str, as_of_date: date, variant: str = "a") -> None:
    """Five trailing quarters of plausible XBRL facts for the fundamental-health leg.

    ``variant`` selects between two genuinely different fact sets so callers
    can seed a second cohort member with different (not identical) raw
    values — a cross-sectional z-score needs real variation within the
    cohort; a cohort where every member has the exact same raw value has
    zero variance and is (correctly) non-computable.
    """

    datasets = {
        "a": {
            "revenues": (
                Decimal("850"),
                Decimal("900"),
                Decimal("950"),
                Decimal("1000"),
                Decimal("1100"),
            ),
            "gross_profits": (
                Decimal("470"),
                Decimal("500"),
                Decimal("530"),
                Decimal("560"),
                Decimal("620"),
            ),
            "cfo": "300",
            "capex": "80",
            "cash": "500",
            "st_inv": "100",
            "debt": "50",
            "assets": "2000",
            "op_income": "250",
            "equity": "1200",
            "shares": "24500000",
        },
        "b": {
            "revenues": (
                Decimal("590"),
                Decimal("600"),
                Decimal("610"),
                Decimal("615"),
                Decimal("618"),
            ),
            "gross_profits": (
                Decimal("242"),
                Decimal("240"),
                Decimal("238"),
                Decimal("235"),
                Decimal("230"),
            ),
            "cfo": "90",
            "capex": "60",
            "cash": "120",
            "st_inv": "20",
            "debt": "300",
            "assets": "1500",
            "op_income": "40",
            "equity": "600",
            "shares": "1600000",
        },
    }
    d = datasets[variant]

    quarters = [as_of_date - timedelta(days=90 * i) for i in range(5, 0, -1)]
    revenues = list(d["revenues"])
    gross_profits = list(d["gross_profits"])

    facts: list[XbrlFact] = []
    for i, (q_end, revenue, gross_profit) in enumerate(zip(quarters, revenues, gross_profits, strict=True)):
        filed = q_end + timedelta(days=30)
        facts.append(
            XbrlFact(
                concept="Revenues",
                value=str(revenue),
                accn=f"acc-{i}",
                fy=2025,
                fp=f"Q{i + 1}",
                form="10-Q",
                end=q_end,
                filed=filed,
            )
        )
        facts.append(
            XbrlFact(
                concept="GrossProfit",
                value=str(gross_profit),
                accn=f"acc-{i}",
                fy=2025,
                fp=f"Q{i + 1}",
                form="10-Q",
                end=q_end,
                filed=filed,
            )
        )

    latest_filed = quarters[-1] + timedelta(days=30)
    facts.append(
        XbrlFact(
            concept="NetCashProvidedByUsedInOperatingActivities",
            value=d["cfo"],
            accn="acc-3",
            fy=2025,
            fp="Q4",
            form="10-Q",
            end=quarters[-1],
            filed=latest_filed,
        )
    )
    facts.append(
        XbrlFact(
            concept="PaymentsToAcquirePropertyPlantAndEquipment",
            value=d["capex"],
            accn="acc-3",
            fy=2025,
            fp="Q4",
            form="10-Q",
            end=quarters[-1],
            filed=latest_filed,
        )
    )
    facts.append(
        XbrlFact(
            concept="CashAndCashEquivalentsAtCarryingValue",
            value=d["cash"],
            accn="acc-3",
            fy=2025,
            fp="Q4",
            form="10-Q",
            end=quarters[-1],
            filed=latest_filed,
        )
    )
    facts.append(
        XbrlFact(
            concept="ShortTermInvestments",
            value=d["st_inv"],
            accn="acc-3",
            fy=2025,
            fp="Q4",
            form="10-Q",
            end=quarters[-1],
            filed=latest_filed,
        )
    )
    facts.append(
        XbrlFact(
            concept="DebtCurrent",
            value=d["debt"],
            accn="acc-3",
            fy=2025,
            fp="Q4",
            form="10-Q",
            end=quarters[-1],
            filed=latest_filed,
        )
    )
    facts.append(
        XbrlFact(
            concept="Assets",
            value=d["assets"],
            accn="acc-3",
            fy=2025,
            fp="Q4",
            form="10-Q",
            end=quarters[-1],
            filed=latest_filed,
        )
    )
    facts.append(
        XbrlFact(
            concept="OperatingIncomeLoss",
            value=d["op_income"],
            accn="acc-3",
            fy=2025,
            fp="Q4",
            form="10-Q",
            end=quarters[-1],
            filed=latest_filed,
        )
    )
    facts.append(
        XbrlFact(
            concept="StockholdersEquity",
            value=d["equity"],
            accn="acc-3",
            fy=2025,
            fp="Q4",
            form="10-Q",
            end=quarters[-1],
            filed=latest_filed,
        )
    )
    facts.append(
        XbrlFact(
            concept="CommonStockSharesOutstanding",
            unit="shares",
            value=d["shares"],
            accn="acc-3",
            fy=2025,
            fp="Q4",
            form="10-Q",
            end=quarters[-1],
            filed=latest_filed,
        )
    )

    snapshot = FundamentalSnapshot(
        id=f"{security_id}:acc-3",
        security_id=security_id,
        accn="acc-3",
        form="10-Q",
        fy=2025,
        fp="Q4",
        filed=latest_filed,
        facts=facts,
    )
    repos.fundamental_sink._snapshots[snapshot.id] = snapshot  # type: ignore[attr-defined]


def seed_channel_a_extraction(repos: PipelineRepos, security_id: str, as_of_date: date, variant: str = "a") -> str:
    """A single recent 10-K-like document with a theme claim.

    ``variant`` varies theme strength/materiality so a second seeded cohort
    member produces a genuinely different raw thesis_linkage/attention value
    (a two-point cross-section with identical raw values has zero variance
    and is, correctly, non-computable).
    """

    strength = ThemeStrength.STRONG if variant == "a" else ThemeStrength.WEAK
    materiality = Materiality.HIGH if variant == "a" else Materiality.LOW

    doc_id = new_id()
    doc = Document(
        id=doc_id,
        security_id=security_id,
        source="edgar",
        source_record_id="0000000000-26-000001",
        document_type=DocumentType.FORM_10K,
        form_type="10-K",
        accession_number="0000000000-26-000001",
        filed_date=as_of_date - timedelta(days=5),
        content_hash=content_hash(f"doc-body-{security_id}"),
        retrieved_at=utc_now(),
        knowledge_date=as_of_date - timedelta(days=5),
    )
    repos.document_sink._docs[doc.id] = doc  # type: ignore[attr-defined]

    extraction = ChannelAExtraction(
        id=new_id(),
        security_id=security_id,
        document_id=doc_id,
        content_hash=doc.content_hash,
        model_version="gpt-4.1-mini-test",
        taxonomy_version="themes-2026-08",
        materiality=materiality,
        sentiment=Sentiment.POSITIVE,
        guidance_direction=GuidanceDirection.RAISED,
        novelty=Novelty.NEW_INFORMATION,
        theme_claims=[
            ThemeClaim(
                theme_id="ai-datacenter-infrastructure",
                strength=strength,
                evidence_excerpt="Demand for our data center accelerators remains very strong.",
                location_hint="Item 7 MD&A",
            )
        ],
        extraction_confidence=ExtractionConfidence.HIGH,
    )
    repos.channel_a_sink._items[extraction.id] = extraction  # type: ignore[attr-defined]
    return doc_id


def seed_insider_form4(repos: PipelineRepos, security_id: str, as_of_date: date) -> None:
    doc = Document(
        id=new_id(),
        security_id=security_id,
        source="edgar",
        source_record_id="0000000000-26-000099",
        document_type=DocumentType.FORM_4,
        form_type="4",
        accession_number="0000000000-26-000099",
        filed_date=as_of_date - timedelta(days=10),
        content_hash=content_hash(f"form4-{security_id}"),
        retrieved_at=utc_now(),
        knowledge_date=as_of_date - timedelta(days=10),
        insider_transactions=[
            InsiderTransaction(
                owner_name="Jane CEO",
                is_officer=True,
                is_director=True,
                is_ten_percent_owner=False,
                transaction_code=Form4TransactionCode.P,
                transaction_date=as_of_date - timedelta(days=10),
                shares="1000",
                price_per_share="120",
            )
        ],
    )
    repos.document_sink._docs[doc.id] = doc  # type: ignore[attr-defined]


def seed_prices(repos: PipelineRepos, security_id: str, as_of_date: date, close: str) -> None:
    from auspex.models.market import PriceBar

    bar = PriceBar(
        id=f"{security_id}:{as_of_date.isoformat()}",
        security_id=security_id,
        session_date=as_of_date,
        open_raw=close,
        high_raw=close,
        low_raw=close,
        close_raw=close,
        volume=1_000_000,
        close_adjusted=close,
    )
    repos.price_sink._bars[bar.id] = bar  # type: ignore[attr-defined]


def seed_universe_prices(repos: PipelineRepos, universe, as_of_date: date) -> None:
    """One current bar for every universe member, as a live night always has.

    arc42 §5.5's staleness rule excludes a security whose latest observed price
    is more than two trading sessions old, and a security with no observed bar
    at all on a day the market demonstrably traded is the limiting case of
    that. Production collects prices for the whole universe every night, so a
    fixture that priced two names and left 102 unpriced was not a thin day — it
    was a shape production never takes. Seeding the universe keeps these tests
    exercising the scoring path rather than the exclusion path.

    Prices vary per member so no cross-section is accidentally constant.
    """

    for index, security in enumerate(universe.securities):
        seed_prices(repos, security.id, as_of_date, str(50 + index))


def seed_fx(repos: PipelineRepos, as_of_date: date, rate: str = "0.88") -> None:
    from auspex.models.market import FxRate

    fx = FxRate(id=f"USDCHF:{as_of_date.isoformat()}", pair="USDCHF", session_date=as_of_date, close_rate=rate)
    repos.fx_sink._rates[fx.id] = fx  # type: ignore[attr-defined]
