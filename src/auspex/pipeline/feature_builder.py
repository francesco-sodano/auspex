"""Maps raw collected/extracted data into the six legs' pure-function inputs.

This is the bridge between ingestion/extraction (documents, XBRL facts, Form 4
transactions, Channel A extractions) and the deterministic scoring functions
in :mod:`auspex.scoring.legs`, which never perform I/O themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

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
    """Exactly one attention event per source document.

    Attention measures how much *disclosure* a security produced. A single
    filing that happens to yield three Channel A extractions is still one
    disclosure event; emitting a document event and then an additional event per
    extraction counted the same filing up to four times and let extraction
    verbosity — an artefact of the extractor, not of issuer behaviour — inflate
    the leg.

    Extraction materiality therefore *enriches* the document's single event:
    the event's weight is ``max(baseline, best extraction materiality)``. Taking
    the maximum keeps the mapping monotone (more material findings can only
    raise attention) and never lets an extraction demote a filing below its
    structural baseline. A news item with no extraction has a baseline of 0 and
    so still contributes nothing, preserving the existing "news counts only when
    something was extracted from it" rule.
    """

    materiality_by_document: dict[str, Decimal] = {}
    for ext in extractions:
        if ext.document_id not in documents_by_id:
            continue
        materiality = weights.materiality_weight[ext.materiality.value]
        current = materiality_by_document.get(ext.document_id)
        if current is None or materiality > current:
            materiality_by_document[ext.document_id] = materiality

    events: list[AttentionEvent] = []
    for doc_id, doc in sorted(documents_by_id.items()):
        is_filing = doc.document_type in _FILING_DOCUMENT_TYPES
        extraction_materiality = materiality_by_document.get(doc_id)
        if not is_filing and extraction_materiality is None:
            continue
        age = _age_days(doc.knowledge_date, as_of_date)
        if age < 0 or age > trailing_days:
            continue
        baseline = Decimal(1) if is_filing else Decimal(0)
        weight = baseline if extraction_materiality is None else max(baseline, extraction_materiality)
        if weight <= 0:
            continue
        events.append(
            AttentionEvent(
                materiality_weight=weight,
                document_authority=weights.authority_for(doc),
                days_ago=age,
            )
        )
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


def _latest_facts_with_end(
    snapshots: list[FundamentalSnapshot],
    concept_aliases: list[str],
    as_of_date: date,
    n: int = 1,
    *,
    unit: str | None = None,
) -> list[tuple[date, Decimal]]:
    """As :func:`_latest_facts` but keeps each value's period end for FX lookup."""

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
    ordered = [(k, by_end[k]) for k in sorted(by_end)]
    return ordered[-n:] if n else ordered


def _latest_facts(
    snapshots: list[FundamentalSnapshot],
    concept_aliases: list[str],
    as_of_date: date,
    n: int = 1,
    *,
    unit: str | None = None,
) -> list[Decimal]:
    """Most recent ``n`` distinct-period values for the first matching alias, filed <= as_of_date."""

    return [value for _end, value in _latest_facts_with_end(snapshots, concept_aliases, as_of_date, n, unit=unit)]


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


class FxConverter(Protocol):
    """Point-in-time authoritative FX, supplied by the pipeline layer.

    ``rate_to_usd`` must return the rate that was authoritative *on
    ``on_date``* — the period end of the fact being converted — or ``None`` if
    no such rate is available. Returning ``None`` is a first-class answer:
    scoring must never approximate a rate, carry one backwards from today, or
    assume parity.
    """

    def rate_to_usd(self, currency: str, on_date: date) -> Decimal | None: ...


VALUATION_REASON_NO_MARKET_CAP = "market_cap_unavailable"
VALUATION_REASON_FX_UNAVAILABLE = "fx_rate_unavailable"


@dataclass(frozen=True)
class ValuationBuildResult:
    """Valuation metrics plus why they may be absent.

    ``fx_unavailable`` distinguishes "this security's valuation could not be
    computed because nobody can put it on a comparable footing" from "this
    security has no valuation data". The former is a *structural* exclusion:
    the caller drops the valuation leg from the applicable set entirely so a
    non-USD reporter is not silently marked down on coverage for a leg that
    could not exist for it.
    """

    metrics: ValuationMetrics
    reporting_currency: str | None
    fx_unavailable: bool = False
    reason: str | None = None


_EMPTY_VALUATION_METRICS = ValuationMetrics(ev_sales=None, ev_ebitda=None, fcf_yield=None)


def build_valuation_metrics(
    market_cap: Decimal | None,
    snapshots: list[FundamentalSnapshot],
    xbrl_concepts: dict,
    as_of_date: date,
    *,
    fx_converter: FxConverter | None = None,
) -> ValuationBuildResult:
    """Build EV/Sales, EV/EBITDA and FCF yield in USD.

    ``market_cap`` is already USD (prices are collected in USD). Fundamentals
    are reported in the issuer's own currency, so for a non-USD reporter every
    fundamental must be converted at the rate authoritative on that fact's own
    period end before it can be divided into a USD market cap.

    When no ``fx_converter`` is supplied, or it cannot provide a rate for any
    required period, the result is an explicit ``fx_unavailable`` — never a
    silent all-``None`` that reads downstream as "this issuer has poor data".
    """

    concepts = xbrl_concepts["concepts"]
    currency = _reporting_currency(snapshots, concepts["revenues"], as_of_date)

    if market_cap is None:
        return ValuationBuildResult(
            metrics=_EMPTY_VALUATION_METRICS,
            reporting_currency=currency,
            reason=VALUATION_REASON_NO_MARKET_CAP,
        )

    unit = currency if currency else "USD"
    needs_fx = unit != "USD"
    if needs_fx and fx_converter is None:
        return ValuationBuildResult(
            metrics=_EMPTY_VALUATION_METRICS,
            reporting_currency=currency,
            fx_unavailable=True,
            reason=VALUATION_REASON_FX_UNAVAILABLE,
        )

    def _latest(concept_key: str) -> tuple[date, Decimal] | None:
        facts = _latest_facts_with_end(snapshots, concepts[concept_key], as_of_date, n=1, unit=unit)
        return facts[-1] if facts else None

    raw = {
        key: _latest(key)
        for key in (
            "revenues",
            "cash_and_equivalents",
            "total_debt",
            "ebitda_operating_income",
            "depreciation_amortization",
            "net_cash_from_operations",
            "capex",
        )
    }

    converted: dict[str, Decimal | None] = {}
    for key, fact in raw.items():
        if fact is None:
            converted[key] = None
            continue
        end, value = fact
        if not needs_fx:
            converted[key] = value
            continue
        rate = fx_converter.rate_to_usd(unit, end) if fx_converter else None
        if rate is None or rate <= 0:
            return ValuationBuildResult(
                metrics=_EMPTY_VALUATION_METRICS,
                reporting_currency=currency,
                fx_unavailable=True,
                reason=VALUATION_REASON_FX_UNAVAILABLE,
            )
        converted[key] = value * rate

    cash_v = converted["cash_and_equivalents"] or Decimal(0)
    debt_v = converted["total_debt"] or Decimal(0)
    ev = market_cap + debt_v - cash_v

    revenue = converted["revenues"]
    op_income = converted["ebitda_operating_income"]
    da = converted["depreciation_amortization"] or Decimal(0)
    cfo = converted["net_cash_from_operations"]
    capex = converted["capex"]

    ev_sales = (ev / revenue) if revenue else None
    ebitda = (op_income + da) if op_income is not None else None
    ev_ebitda = (ev / ebitda) if ebitda else None
    fcf = (cfo - capex) if cfo is not None and capex is not None else None
    fcf_yield = (fcf / market_cap) if fcf is not None and market_cap != 0 else None

    return ValuationBuildResult(
        metrics=ValuationMetrics(ev_sales=ev_sales, ev_ebitda=ev_ebitda, fcf_yield=fcf_yield),
        reporting_currency=currency,
    )
