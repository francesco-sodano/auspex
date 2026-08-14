"""Maps raw collected/extracted data into the six legs' pure-function inputs.

This is the bridge between ingestion/extraction (documents, XBRL facts, Form 4
transactions, Channel A extractions) and the deterministic scoring functions
in :mod:`auspex.scoring.legs`, which never perform I/O themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from auspex.models.document import Document
from auspex.models.enums import DocumentType
from auspex.models.extraction import ChannelAExtraction
from auspex.models.fundamentals import FundamentalSnapshot
from auspex.scoring.legs import (
    AttentionEvent,
    FundamentalHealthInputs,
    InsiderTxnEvent,
    NarrativeClaimEvent,
    ThemeClaimEvent,
    ValuationMetrics,
    fcf_margin,
    gross_margin_trend_slope,
    net_cash_ratio,
    revenue_growth_yoy,
    roic,
)

_DOCUMENT_TYPE_TO_AUTHORITY_KEY = {
    DocumentType.FORM_10K: "10-K",
    DocumentType.FORM_10Q: "10-Q",
    DocumentType.FORM_8K: "8-K",
    DocumentType.FORM_20F: "20-F",
    DocumentType.FORM_6K: "6-K",
    DocumentType.FORM_S1: "S-1",
    DocumentType.NEWS: "news",
}
_FILING_DOCUMENT_TYPES = frozenset(_DOCUMENT_TYPE_TO_AUTHORITY_KEY) - {
    DocumentType.NEWS
}


@dataclass(frozen=True)
class WeightsConfig:
    document_authority: dict[str, Decimal]
    theme_strength: dict[str, Decimal]
    materiality_weight: dict[str, Decimal]
    recency_half_life_days: Decimal
    roic_tax_rate: Decimal

    @classmethod
    def from_yaml(cls, weights_yaml: dict) -> WeightsConfig:
        return cls(
            document_authority={k: Decimal(v) for k, v in weights_yaml["document_authority"].items()},
            theme_strength={k: Decimal(v) for k, v in weights_yaml["theme_strength"].items()},
            materiality_weight={k: Decimal(v) for k, v in weights_yaml["materiality_weight"].items()},
            recency_half_life_days=Decimal(weights_yaml["recency_half_life_days"]),
            roic_tax_rate=Decimal(weights_yaml["roic_tax_rate"]),
        )

    def authority_for(self, document: Document) -> Decimal:
        key = _DOCUMENT_TYPE_TO_AUTHORITY_KEY.get(document.document_type, "news")
        return self.document_authority.get(key, Decimal("0.4"))


def _age_days(knowledge_date: date, as_of_date: date) -> int:
    return (as_of_date - knowledge_date).days


def build_thesis_linkage_events(
    extractions: list[ChannelAExtraction],
    documents_by_id: dict[str, Document],
    weights: WeightsConfig,
    as_of_date: date,
    trailing_days: int = 180,
) -> list[ThemeClaimEvent]:
    events: list[ThemeClaimEvent] = []
    for ext in extractions:
        doc = documents_by_id.get(ext.document_id)
        if doc is None:
            continue
        age = _age_days(doc.knowledge_date, as_of_date)
        if age < 0 or age > trailing_days:
            continue
        authority = weights.authority_for(doc)
        for claim in ext.theme_claims:
            strength_value = weights.theme_strength[claim.strength.value]
            events.append(
                ThemeClaimEvent(theme_strength_value=strength_value, document_authority=authority, age_days=age)
            )
    return events


def build_attention_events(
    extractions: list[ChannelAExtraction],
    documents_by_id: dict[str, Document],
    weights: WeightsConfig,
    as_of_date: date,
    trailing_days: int = 60,
) -> list[AttentionEvent]:
    events: list[AttentionEvent] = []
    for doc in documents_by_id.values():
        if doc.document_type not in _FILING_DOCUMENT_TYPES:
            continue
        age = _age_days(doc.knowledge_date, as_of_date)
        if age < 0 or age > trailing_days:
            continue
        events.append(
            AttentionEvent(
                materiality_weight=Decimal(1),
                document_authority=weights.authority_for(doc),
                days_ago=age,
            )
        )

    for ext in extractions:
        doc = documents_by_id.get(ext.document_id)
        if doc is None:
            continue
        age = _age_days(doc.knowledge_date, as_of_date)
        if age < 0 or age > trailing_days:
            continue
        authority = weights.authority_for(doc)
        materiality = weights.materiality_weight[ext.materiality.value]
        events.append(AttentionEvent(materiality_weight=materiality, document_authority=authority, days_ago=age))
    return events


def build_narrative_events(
    extractions: list[ChannelAExtraction],
    documents_by_id: dict[str, Document],
    weights: WeightsConfig,
    as_of_date: date,
    trailing_days: int = 180,
) -> list[NarrativeClaimEvent]:
    events: list[NarrativeClaimEvent] = []
    for ext in extractions:
        doc = documents_by_id.get(ext.document_id)
        if doc is None:
            continue
        age = _age_days(doc.knowledge_date, as_of_date)
        if age < 0 or age > trailing_days:
            continue
        for claim in ext.narrative_claims:
            strength_value = weights.theme_strength[claim.strength.value]
            events.append(NarrativeClaimEvent(strength_value=strength_value, age_days=age))
    return events


def build_insider_events(documents: list[Document], as_of_date: date, trailing_days: int = 90) -> list[InsiderTxnEvent]:
    events: list[InsiderTxnEvent] = []
    for doc in documents:
        if doc.document_type != DocumentType.FORM_4:
            continue
        for txn in doc.insider_transactions:
            age = _age_days(txn.transaction_date, as_of_date)
            if age < 0 or age > trailing_days:
                continue
            events.append(
                InsiderTxnEvent(
                    code=txn.transaction_code,
                    shares=Decimal(txn.shares),
                    price_per_share=Decimal(txn.price_per_share),
                    is_officer_or_director=txn.is_officer or txn.is_director,
                    is_ten_percent_owner=txn.is_ten_percent_owner,
                    days_ago=age,
                )
            )
    return events


def _latest_facts(
    snapshots: list[FundamentalSnapshot],
    concept_aliases: list[str],
    as_of_date: date,
    n: int = 1,
    *,
    unit: str | None = None,
) -> list[Decimal]:
    """Most recent ``n`` distinct-period values for the first matching alias, filed <= as_of_date."""

    candidates = []
    for snap in snapshots:
        if snap.filed > as_of_date:
            continue
        for alias in concept_aliases:
            for fact in snap.facts:
                if (
                    fact.concept == alias
                    and fact.filed <= as_of_date
                    and (unit is None or fact.unit == unit)
                ):
                    candidates.append((fact.end, Decimal(fact.value)))
    candidates.sort(key=lambda t: t[0])
    # de-duplicate by period end, keep latest filed value per period (already sorted by end)
    by_end: dict = {}
    for end, value in candidates:
        by_end[end] = value
    ordered = [by_end[k] for k in sorted(by_end)]
    return ordered[-n:] if n else ordered


def _reporting_currency(
    snapshots: list[FundamentalSnapshot],
    revenue_aliases: list[str],
    as_of_date: date,
) -> str | None:
    candidates = [
        (fact.filed, fact.end, fact.unit)
        for snapshot in snapshots
        if snapshot.filed <= as_of_date
        for fact in snapshot.facts
        if (
            fact.concept in revenue_aliases
            and fact.filed <= as_of_date
            and len(fact.unit) == 3
            and fact.unit.isalpha()
        )
    ]
    return max(candidates)[2] if candidates else None


def build_fundamental_health_inputs(
    snapshots: list[FundamentalSnapshot],
    xbrl_concepts: dict,
    roic_tax_rate: Decimal,
    as_of_date: date,
) -> FundamentalHealthInputs:
    concepts = xbrl_concepts["concepts"]
    reporting_currency = _reporting_currency(
        snapshots,
        concepts["revenues"],
        as_of_date,
    )

    revenues = _latest_facts(
        snapshots,
        concepts["revenues"],
        as_of_date,
        n=5,
        unit=reporting_currency,
    )
    revenue_growth = None
    if len(revenues) >= 5:
        revenue_growth = revenue_growth_yoy(revenues[-1], revenues[0])

    gross_profits = _latest_facts(
        snapshots,
        concepts["gross_profit"],
        as_of_date,
        n=4,
        unit=reporting_currency,
    )
    margins = None
    if len(gross_profits) == 4 and len(revenues) >= 4:
        recent_revenues = revenues[-4:]
        margins = [gp / rev for gp, rev in zip(gross_profits, recent_revenues, strict=False) if rev != 0]
    margin_slope = gross_margin_trend_slope(margins) if margins and len(margins) >= 2 else None

    cfo = _latest_facts(
        snapshots,
        concepts["net_cash_from_operations"],
        as_of_date,
        n=1,
        unit=reporting_currency,
    )
    capex = _latest_facts(
        snapshots,
        concepts["capex"],
        as_of_date,
        n=1,
        unit=reporting_currency,
    )
    fcf = None
    if cfo and capex and revenues:
        fcf = fcf_margin(cfo[-1], capex[-1], revenues[-1])

    cash = _latest_facts(
        snapshots,
        concepts["cash_and_equivalents"],
        as_of_date,
        n=1,
        unit=reporting_currency,
    )
    st_inv = _latest_facts(
        snapshots,
        concepts["short_term_investments"],
        as_of_date,
        n=1,
        unit=reporting_currency,
    )
    debt = _latest_facts(
        snapshots,
        concepts["total_debt"],
        as_of_date,
        n=1,
        unit=reporting_currency,
    )
    assets = _latest_facts(
        snapshots,
        concepts["total_assets"],
        as_of_date,
        n=1,
        unit=reporting_currency,
    )
    net_cash = None
    if cash and assets:
        net_cash = net_cash_ratio(
            cash[-1] if cash else Decimal(0),
            st_inv[-1] if st_inv else Decimal(0),
            debt[-1] if debt else Decimal(0),
            assets[-1],
        )

    op_income = _latest_facts(
        snapshots,
        concepts["operating_income"],
        as_of_date,
        n=1,
        unit=reporting_currency,
    )
    equity = _latest_facts(
        snapshots,
        concepts["stockholders_equity"],
        as_of_date,
        n=1,
        unit=reporting_currency,
    )
    roic_value = None
    if op_income and equity:
        roic_value = roic(
            op_income[-1], roic_tax_rate, equity[-1], debt[-1] if debt else Decimal(0), cash[-1] if cash else Decimal(0)
        )

    return FundamentalHealthInputs(
        revenue_growth_yoy=revenue_growth,
        gross_margin_trend_slope=margin_slope,
        fcf_margin=fcf,
        net_cash_ratio=net_cash,
        roic=roic_value,
    )


def build_valuation_metrics(
    market_cap: Decimal | None,
    snapshots: list[FundamentalSnapshot],
    xbrl_concepts: dict,
    as_of_date: date,
) -> ValuationMetrics:
    if market_cap is None:
        return ValuationMetrics(ev_sales=None, ev_ebitda=None, fcf_yield=None)

    concepts = xbrl_concepts["concepts"]
    if _reporting_currency(snapshots, concepts["revenues"], as_of_date) != "USD":
        return ValuationMetrics(ev_sales=None, ev_ebitda=None, fcf_yield=None)
    revenues = _latest_facts(snapshots, concepts["revenues"], as_of_date, n=1, unit="USD")
    cash = _latest_facts(
        snapshots,
        concepts["cash_and_equivalents"],
        as_of_date,
        n=1,
        unit="USD",
    )
    debt = _latest_facts(snapshots, concepts["total_debt"], as_of_date, n=1, unit="USD")
    op_income = _latest_facts(
        snapshots,
        concepts["ebitda_operating_income"],
        as_of_date,
        n=1,
        unit="USD",
    )
    da = _latest_facts(
        snapshots,
        concepts["depreciation_amortization"],
        as_of_date,
        n=1,
        unit="USD",
    )
    cfo = _latest_facts(
        snapshots,
        concepts["net_cash_from_operations"],
        as_of_date,
        n=1,
        unit="USD",
    )
    capex = _latest_facts(snapshots, concepts["capex"], as_of_date, n=1, unit="USD")

    cash_v = cash[-1] if cash else Decimal(0)
    debt_v = debt[-1] if debt else Decimal(0)
    ev = market_cap + debt_v - cash_v

    ev_sales = (ev / revenues[-1]) if revenues and revenues[-1] != 0 else None
    ebitda = (op_income[-1] + (da[-1] if da else Decimal(0))) if op_income else None
    ev_ebitda = (ev / ebitda) if ebitda and ebitda != 0 else None
    fcf = (cfo[-1] - capex[-1]) if cfo and capex else None
    fcf_yield = (fcf / market_cap) if fcf is not None and market_cap != 0 else None

    return ValuationMetrics(ev_sales=ev_sales, ev_ebitda=ev_ebitda, fcf_yield=fcf_yield)
