"""Evidence-backed descriptions of the inputs behind each deterministic leg.

This module describes supplied results; it never recomputes or changes a score.
Absolute issuer observations and the final, peer-relative contribution are kept
separate, particularly when net selling or growing sales compare well or poorly
with peers. Thesis linkage describes unsigned theme exposure, not a prediction
of business success.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from auspex.models.document import Document, InsiderTransaction
from auspex.models.enums import DocumentType, FilerProfile, Form4TransactionCode, LegName, NarrativeClaimType
from auspex.models.extraction import ChannelAExtraction, NarrativeClaim, ThemeClaim
from auspex.models.fundamentals import FundamentalSnapshot
from auspex.models.scoring import LegExplanation, ScoreEvidence
from auspex.models.security import Security
from auspex.pipeline.feature_builder import (
    VALUATION_REASON_FX_UNAVAILABLE,
    VALUATION_REASON_NO_MARKET_CAP,
    ValuationBuildResult,
    WeightsConfig,
    build_attention_events,
)
from auspex.scoring.composite import REASON_DEGENERATE_CROSS_SECTION, LegCompositeResult
from auspex.scoring.engine import SecurityScoreResult
from auspex.scoring.legs import (
    ATTENTION_OBSERVATION_WINDOW_DAYS,
    ATTENTION_RECENT_WINDOW_DAYS,
    SUB_METRIC_NAMES,
    AttentionEvent,
    FundamentalHealthInputs,
    FundamentalHealthResult,
    NarrativeClaimEvent,
    narrative_claim_aggregate,
)
from auspex.scoring.normalize import exponential_decay


@dataclass(frozen=True)
class LegEvidenceContext:
    """Pipeline-verified evidence with valuation signals already oriented cheap-high."""

    security: Security
    as_of_date: date
    documents: list[Document]
    extractions: list[ChannelAExtraction]
    fundamentals: list[FundamentalSnapshot]
    weights: WeightsConfig
    taxonomy_labels: dict[str, str]
    fundamental_inputs: FundamentalHealthInputs
    fundamental_health: FundamentalHealthResult
    valuation: ValuationBuildResult
    valuation_signals: dict[str, Decimal | None]
    growth_percentile: int | None


@dataclass(frozen=True)
class _Observation:
    summary: str
    evidence: list[ScoreEvidence]
    missing_reason: str
    subject: str
    unavailable: bool = False


_SOURCE_TYPES = {
    DocumentType.FORM_10K: "annual reports",
    DocumentType.FORM_10Q: "quarterly reports",
    DocumentType.FORM_8K: "current-event filings",
    DocumentType.FORM_20F: "foreign-issuer annual reports",
    DocumentType.FORM_6K: "foreign-issuer updates",
    DocumentType.FORM_S1: "registration filings",
    DocumentType.FORM_4: "insider-transaction filings",
    DocumentType.NEWS: "reviewed news reports",
}
_NARRATIVE_TYPES = {
    NarrativeClaimType.TAM_EXPANSION: "market-expansion claims",
    NarrativeClaimType.NEW_PRODUCT: "new-product announcements",
    NarrativeClaimType.PARTNERSHIP: "partnership announcements",
    NarrativeClaimType.DESIGN_WIN: "design-win announcements",
    NarrativeClaimType.CAPACITY_EXPANSION: "capacity-expansion plans",
    NarrativeClaimType.MANAGEMENT_CHANGE: "management changes",
}
_FUNDAMENTAL_LABELS = {
    "revenue_growth_yoy": "sales growth",
    "gross_margin_trend_slope": "the gross-margin trend",
    "fcf_margin": "cash generation after investment",
    "net_cash_ratio": "cash relative to debt",
    "roic": "returns on invested capital",
}
_VALUATION_LABELS = {
    "ev_sales": "Sales-based valuation",
    "ev_ebitda": "Operating-earnings valuation",
    "fcf_yield": "Cash-flow valuation",
}


def _join(items: list[str]) -> str:
    if len(items) < 2:
        return "".join(items)
    if len(items) == 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + ", and " + items[-1]


def _finite(value: Decimal | None) -> Decimal | None:
    return value if value is not None and value.is_finite() else None


def _amount(value: str) -> Decimal | None:
    try:
        number = _finite(Decimal(value))
    except InvalidOperation:
        return None
    return number if number is not None and number >= 0 else None


def _bounded(evidence: list[ScoreEvidence]) -> list[ScoreEvidence]:
    result: list[ScoreEvidence] = []
    seen: set[str] = set()
    for item in evidence:
        if item.evidence_id not in seen:
            result.append(item)
            seen.add(item.evidence_id)
        if len(result) == 3:
            break
    return result


def _document_evidence(document: Document, excerpt: str | None = None) -> ScoreEvidence:
    return ScoreEvidence(
        evidence_id=document.id,
        label=f"{document.document_type.value}: {document.title}" if document.title else document.document_type.value,
        knowledge_date=document.knowledge_date,
        source_url=document.url,
        excerpt=excerpt if excerpt else document.content_excerpt,
    )


def _visible_inputs(context: LegEvidenceContext) -> tuple[dict[str, Document], list[ChannelAExtraction]]:
    """Bound the pipeline's source-verified subset without rechecking its fingerprints."""

    documents = {
        doc.id: doc
        for doc in sorted(context.documents, key=lambda doc: (doc.knowledge_date, doc.id, doc.content_hash))
        if doc.security_id == context.security.id
        and doc.knowledge_date <= context.as_of_date
        and (doc.filed_date is None or doc.filed_date <= context.as_of_date)
        and (doc.published_at is None or doc.published_at.date() <= context.as_of_date)
    }
    extractions = [
        ext
        for ext in sorted(context.extractions, key=lambda ext: (ext.document_id, ext.id))
        if ext.security_id == context.security.id
        and ext.document_id in documents
    ]
    return documents, extractions


def _financial_evidence(context: LegEvidenceContext) -> list[ScoreEvidence]:
    evidence: list[ScoreEvidence] = []
    cik = context.security.cik
    valid_cik = re.fullmatch(r"[0-9]{1,10}", cik) is not None and int(cik) > 0
    for snapshot in context.fundamentals:
        if snapshot.security_id != context.security.id or snapshot.filed > context.as_of_date:
            continue
        facts = [
            fact for fact in snapshot.facts
            if fact.filed <= context.as_of_date and fact.end <= context.as_of_date
        ]
        if not facts:
            continue
        source_url = None
        if valid_cik:
            source_url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik.zfill(10)}.json"
            if (
                re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", snapshot.accn)
                and all(fact.accn == snapshot.accn for fact in facts)
            ):
                accession_path = snapshot.accn.replace("-", "")
                source_url = (
                    f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_path}/"
                    f"{snapshot.accn}-index.html"
                )
        evidence.append(
            ScoreEvidence(
                evidence_id=snapshot.id,
                label=f"Reported financial data ({snapshot.form})",
                knowledge_date=max(snapshot.filed, *(fact.filed for fact in facts)),
                source_url=source_url,
            )
        )
    return _bounded(sorted(evidence, key=lambda item: (-item.knowledge_date.toordinal(), item.evidence_id)))


def _thesis(
    context: LegEvidenceContext,
    documents: dict[str, Document],
    extractions: list[ChannelAExtraction],
    leg: LegCompositeResult | None,
) -> _Observation:
    reviewed: list[Document] = []
    claims: list[tuple[Decimal, Document, ThemeClaim]] = []
    strengths: dict[str, Decimal] = {}
    discarded_claims = False
    raw = _finite(leg.raw) if leg else None
    for extraction in extractions:
        document = documents[extraction.document_id]
        age = (context.as_of_date - document.knowledge_date).days
        if age > 180:
            continue
        reviewed.append(document)
        discarded_claims = discarded_claims or extraction.discarded_claim_count > 0
        for claim in sorted(
            extraction.theme_claims, key=lambda claim: (claim.theme_id, claim.strength.value, claim.evidence_excerpt)
        ):
            label = context.taxonomy_labels.get(claim.theme_id, "").strip()
            if not label:
                continue
            strength = (
                context.weights.theme_strength[claim.strength.value]
                * context.weights.authority_for(document)
                * exponential_decay(age, context.weights.recency_half_life_days)
            )
            claims.append((strength, document, claim))
            strengths[label] = strengths.get(label, Decimal(0)) + strength
    ranked = sorted(
        claims,
        key=lambda row: (-row[0], -row[1].knowledge_date.toordinal(), row[1].id, row[2].theme_id,
                         row[2].evidence_excerpt),
    )
    labels = sorted(strengths, key=lambda label: (-strengths[label], label))[:3]
    if labels:
        summary = (
            f"Reports connect this company to {_join(labels)}. "
            "Such links can reflect risks as well as opportunities; "
            "they show exposure, not proof of future success."
        )
        candidates = [
            next(row for row in ranked if context.taxonomy_labels[row[2].theme_id].strip() == label)
            for label in labels
        ]
        evidence = [_document_evidence(doc, claim.evidence_excerpt) for _, doc, claim in candidates + ranked]
        missing = (
            "These claims are available, but a verified theme assessment was not established; "
            "this is not a reviewed finding that no approved theme matches exist."
        )
    elif reviewed and raw == 0:
        summary = (
            "Reviewed disclosures contain no claims matching the approved investment themes; "
            "this is a completed review, not an evidence gap. "
            "It is not a forecast of business success or failure."
        )
        evidence = [
            _document_evidence(doc)
            for doc in sorted(reviewed, key=lambda doc: (-doc.knowledge_date.toordinal(), doc.id))
        ]
        missing = "The absence of approved matches has been reviewed, but a current peer comparison is unavailable."
    elif reviewed:
        summary = (
            "The available review does not establish usable theme evidence; "
            "whether approved investment-theme links are present remains unknown."
        )
        if discarded_claims:
            summary += " Some extracted claims were discarded and are not used as evidence."
        evidence = [
            _document_evidence(doc)
            for doc in sorted(reviewed, key=lambda doc: (-doc.knowledge_date.toordinal(), doc.id))
        ]
        missing = "This is missing or unverified evidence, not a reviewed finding of no approved theme matches."
    else:
        recent = any((context.as_of_date - doc.knowledge_date).days <= 180 for doc in documents.values())
        summary = (
            "Recent disclosures have not been reviewed successfully for links to the approved investment themes."
            if recent else "No issuer disclosures are available within the theme-review period."
        )
        evidence = []
        missing = (
            "This is missing or unverified theme evidence, "
            "not a reviewed finding that the business lacks theme links."
        )
    subject = (
        "absence of approved theme matches in the reviewed disclosures"
        if reviewed and raw == 0 and not labels else "documented theme exposure"
    )
    return _Observation(
        summary, _bounded(evidence), missing, subject,
        unavailable=not reviewed or (not labels and raw != 0),
    )


def _attention(
    context: LegEvidenceContext,
    documents: dict[str, Document],
    extractions: list[ChannelAExtraction],
) -> _Observation:
    by_document: dict[str, list[ChannelAExtraction]] = {}
    for ext in extractions:
        by_document.setdefault(ext.document_id, []).append(ext)
    entries: list[tuple[Document, AttentionEvent]] = []
    for document in documents.values():
        events = build_attention_events(
            by_document.get(document.id, []), {document.id: document}, context.weights, context.as_of_date
        )
        entries.extend(
            (document, event) for event in events
            if 0 <= event.days_ago < ATTENTION_OBSERVATION_WINDOW_DAYS
        )
    ranked = sorted(
        entries,
        key=lambda row: (
            -(row[1].materiality_weight * row[1].document_authority),
            -row[0].knowledge_date.toordinal(), row[0].id,
        ),
    )
    recent = [row for row in ranked if row[1].days_ago < ATTENTION_RECENT_WINDOW_DAYS]
    prior = [row for row in ranked if row[1].days_ago >= ATTENTION_RECENT_WINDOW_DAYS]
    recent_activity = sum((e.materiality_weight * e.document_authority for _, e in recent), Decimal(0))
    prior_activity = sum((e.materiality_weight * e.document_authority for _, e in prior), Decimal(0))

    def sources(rows: list[tuple[Document, AttentionEvent]]) -> str:
        return _join(sorted({_SOURCE_TYPES[doc.document_type] for doc, _ in rows}))

    if not ranked:
        summary = "No qualifying filings or material reviewed news are available in either disclosure period."
    elif recent_activity > prior_activity:
        comparison = (
            f"outweigh {sources(prior)} in the preceding period"
            if prior else "follow a preceding period with no qualifying disclosures"
        )
        summary = f"Important disclosure activity has increased: recent {sources(recent)} {comparison}."
    elif recent_activity < prior_activity:
        comparison = (
            f"outweigh recent {sources(recent)}"
            if recent else "have not been followed by qualifying recent disclosures"
        )
        summary = f"Important disclosure activity has decreased: earlier {sources(prior)} {comparison}."
    else:
        source_types = sources(ranked)
        summary = (
            f"Important disclosure activity is similar across the recent and preceding periods, "
            f"after accounting for the importance and source of {source_types}."
        )
    selected = recent[:1] + prior[:1] + ranked
    return _Observation(
        summary,
        _bounded([_document_evidence(doc) for doc, _ in selected]),
        "A current disclosure-activity comparison cannot be established from the available observations.",
        "change in important disclosure activity",
        unavailable=not entries,
    )


def _growth_description(context: LegEvidenceContext) -> str:
    growth = _finite(context.fundamental_inputs.revenue_growth_yoy)
    comparison = context.growth_percentile
    if growth is None:
        absolute = "The underlying sales-growth figure is unavailable"
    elif growth > 0:
        absolute = "Sales are growing"
    elif growth < 0:
        absolute = "Sales are falling"
    else:
        absolute = "Sales are flat"
    if comparison is None:
        return absolute + ", and their growth cannot be compared with peers."
    if comparison > 50:
        return absolute + ", while the growth comparison is stronger than typical peers'."
    if comparison < 50:
        return absolute + ", while the growth comparison is weaker than typical peers'."
    return absolute + ", with growth around the middle of comparable businesses."


def _narrative(
    context: LegEvidenceContext,
    documents: dict[str, Document],
    extractions: list[ChannelAExtraction],
    financial_evidence: list[ScoreEvidence],
    leg: LegCompositeResult | None,
) -> _Observation:
    claims: list[tuple[Decimal, Document, NarrativeClaim]] = []
    reviewed: list[Document] = []
    discarded_claims = False
    for ext in extractions:
        document = documents[ext.document_id]
        age = (context.as_of_date - document.knowledge_date).days
        if age > 180:
            continue
        reviewed.append(document)
        discarded_claims = discarded_claims or ext.discarded_claim_count > 0
        for claim in ext.narrative_claims:
            emphasis = narrative_claim_aggregate([
                NarrativeClaimEvent(context.weights.theme_strength[claim.strength.value], age)
            ])
            claims.append((emphasis, document, claim))
    ranked = sorted(
        claims,
        key=lambda row: (-row[0], -row[1].knowledge_date.toordinal(), row[1].id, row[2].claim_type.value,
                         row[2].evidence_excerpt),
    )
    if ranked:
        types = list(dict.fromkeys(_NARRATIVE_TYPES[claim.claim_type] for _, _, claim in ranked))[:3]
        newest_age = min((context.as_of_date - doc.knowledge_date).days for _, doc, _ in ranked)
        recency = "Recent" if newest_age < ATTENTION_RECENT_WINDOW_DAYS else "Earlier"
        summary = f"{recency} disclosures emphasize {_join(types)}."
        if newest_age >= 90:
            summary += " These older claims carry less emphasis as they age."
        evidence = _bounded([_document_evidence(doc, claim.evidence_excerpt) for _, doc, claim in ranked])
    else:
        if discarded_claims:
            summary = (
                "No usable narrative claims were retained from the available review. "
                "Some extracted claims were discarded; this does not establish that the company made no announcements."
            )
        else:
            summary = (
                "Reviewed disclosures contain no extracted narrative claims."
                if reviewed else "No reviewed narrative claims are available; company announcements cannot be inferred."
            )
        evidence = _bounded([
            _document_evidence(doc)
            for doc in sorted(reviewed, key=lambda doc: (-doc.knowledge_date.toordinal(), doc.id))
        ])
    summary += " " + _growth_description(context)
    raw = _finite(leg.raw) if leg else None
    if context.growth_percentile is not None and raw is not None:
        if raw > 0 and ranked:
            summary += " The recorded business story is more prominent than the business-growth comparison."
        elif raw < 0:
            summary += " The business-growth comparison is stronger than the recorded storytelling."
        elif raw == 0:
            summary += " The recorded story and business-growth comparison are in balance."
    if financial_evidence:
        evidence = _bounded(evidence[:2] + financial_evidence[:1])
    if context.growth_percentile is None:
        missing = (
            "Comparable business-growth evidence is missing, so the story cannot be assessed against business growth."
        )
    elif discarded_claims and not ranked:
        missing = (
            "The available review does not provide enough verified narrative evidence "
            "to compare the story with business growth."
        )
    else:
        missing = "A current comparison of the recorded story with business growth is unavailable."
    return _Observation(
        summary,
        evidence,
        missing,
        "story-versus-growth balance",
        unavailable=context.growth_percentile is None,
    )


def _trade_order(row: tuple[Document, InsiderTransaction, Decimal]) -> tuple:
    document, transaction, value = row
    return (
        -value, -transaction.transaction_date.toordinal(), -document.knowledge_date.toordinal(),
        document.id, transaction.owner_name, transaction.transaction_code.value,
    )


def _smart_money(
    context: LegEvidenceContext,
    documents: dict[str, Document],
    leg: LegCompositeResult | None,
) -> _Observation:
    observed: list[tuple[Document, InsiderTransaction]] = []
    trades: list[tuple[Document, InsiderTransaction, Decimal]] = []
    invalid_amounts = False
    for document in documents.values():
        if document.document_type != DocumentType.FORM_4:
            continue
        for transaction in document.insider_transactions:
            if not 0 <= (context.as_of_date - transaction.transaction_date).days < 90:
                continue
            observed.append((document, transaction))
            if transaction.transaction_code not in {Form4TransactionCode.P, Form4TransactionCode.S}:
                continue
            influence = (
                Decimal(1) if transaction.is_officer or transaction.is_director
                else Decimal("0.5") if transaction.is_ten_percent_owner else Decimal(0)
            )
            if not influence:
                continue
            shares, price = _amount(transaction.shares), _amount(transaction.price_per_share)
            if shares is None or price is None:
                invalid_amounts = True
                continue
            trades.append((document, transaction, shares * price * influence))
    ranked = sorted(trades, key=_trade_order)
    purchases = [row for row in ranked if row[1].transaction_code == Form4TransactionCode.P]
    sales = [row for row in ranked if row[1].transaction_code == Form4TransactionCode.S]
    purchase_value = sum((value for _, _, value in purchases), Decimal(0))
    sale_value = sum((value for _, _, value in sales), Decimal(0))
    net = purchase_value - sale_value
    if net < 0:
        summary = "Qualifying insider sales outweigh purchases after accounting for the insiders' roles."
        leading = sales
    elif net > 0:
        summary = "Qualifying insider purchases outweigh sales after accounting for the insiders' roles."
        leading = purchases
    elif purchase_value > 0:
        summary = "Qualifying insider purchases and sales are balanced after accounting for the insiders' roles."
        leading = ranked
    elif ranked:
        summary = "The qualifying purchases and sales have no positive disclosed transaction value."
        leading = ranked
    elif invalid_amounts:
        summary = "Qualifying open-market trades were disclosed, but usable transaction values are unavailable."
        leading = []
    else:
        leading = []
        excluded = {
            Form4TransactionCode.A: "share awards",
            Form4TransactionCode.M: "option exercises",
            Form4TransactionCode.F: "tax-withholding transactions",
            Form4TransactionCode.G: "gifts",
            Form4TransactionCode.C: "conversion transactions",
            Form4TransactionCode.D: "other dispositions",
        }
        kinds = sorted({excluded[txn.transaction_code] for _, txn in observed if txn.transaction_code in excluded})
        summary = "No qualifying open-market insider purchases or sales are recorded in the recent trading window."
        if kinds:
            summary += f" The available filings include {_join(kinds)}, which do not count as open-market trades."
        if any(
            txn.transaction_code in {Form4TransactionCode.P, Form4TransactionCode.S}
            and not (txn.is_officer or txn.is_director or txn.is_ten_percent_owner)
            for _, txn in observed
        ):
            summary += " Other reported trades are excluded because the owners do not hold qualifying insider roles."
    meaningful = next((row for row in leading if row[2] > 0 and row[1].owner_name.strip()), None)
    if meaningful:
        transaction = meaningful[1]
        action = "purchase" if transaction.transaction_code == Form4TransactionCode.P else "sale"
        summary += f" This includes a {action} by {transaction.owner_name.strip()}."
    if invalid_amounts:
        if ranked:
            summary = "Among trades with usable amounts, " + summary[0].lower() + summary[1:]
        summary += (
            " Some qualifying trade amounts are unusable, so the complete buying-versus-selling balance is unknown."
        )
    no_market_cap = context.valuation.reason == VALUATION_REASON_NO_MARKET_CAP
    if leg and leg.computable and leg.z is not None and not (no_market_cap or invalid_amounts):
        if net < 0 and leg.z > 0:
            summary += " Despite net selling, the balance relative to company size is stronger than peers'."
        elif net > 0 and leg.z < 0:
            summary += " Despite net buying, the balance relative to company size is weaker than peers'."
    if sale_value > 0:
        summary += " Insider selling alone does not establish the business outlook."
    selected = leading[:1] + purchases[:1] + sales[:1] + ranked
    evidence = [_document_evidence(doc) for doc, _, _ in selected]
    if not evidence:
        evidence = [
            _document_evidence(doc)
            for doc, _ in sorted(observed, key=lambda row: (-row[0].knowledge_date.toordinal(), row[0].id))
        ]
    return _Observation(
        summary,
        _bounded(evidence),
        "The company's market value is unavailable, so insider trades cannot be compared fairly across company sizes."
        if no_market_cap else "A comparable insider-trading balance cannot be established from the available inputs.",
        "company-size-adjusted insider-trading balance",
        unavailable=no_market_cap or invalid_amounts,
    )


def _fundamental_fact(name: str, value: Decimal) -> str:
    descriptions = {
        "revenue_growth_yoy": ("sales are growing", "sales are flat", "sales are falling"),
        "gross_margin_trend_slope": (
            "gross margins are widening", "gross margins show no overall widening or narrowing",
            "gross margins are narrowing",
        ),
        "fcf_margin": (
            "operations leave cash after investment", "operations just cover investment",
            "operations do not cover investment",
        ),
        "net_cash_ratio": (
            "cash and short-term investments exceed debt", "cash and short-term investments match debt",
            "debt exceeds cash and short-term investments",
        ),
        "roic": (
            "returns on invested capital are positive", "returns on invested capital are zero",
            "returns on invested capital are negative",
        ),
    }
    return descriptions[name][0 if value > 0 else 2 if value < 0 else 1]


def _fundamentals(context: LegEvidenceContext, evidence: list[ScoreEvidence]) -> _Observation:
    values = {name: _finite(value) for name, value in context.fundamental_inputs.as_map().items()}
    comparable = {
        name: z for name in SUB_METRIC_NAMES
        if values[name] is not None
        and (z := _finite(context.fundamental_health.sub_metric_z.get(name))) is not None
    }
    clauses: list[str] = []
    for name, value in values.items():
        if value is None:
            continue
        clause = _fundamental_fact(name, value)
        z = comparable.get(name)
        if z is not None and z < 0 and value > 0:
            clause += ", but this still trails peers"
        elif z is not None and z > 0 and value < 0:
            clause += ", but this still compares favorably with peers"
        clauses.append(clause)
    if clauses:
        summary = "; ".join(clauses)
        summary = summary[0].upper() + summary[1:] + "."
    else:
        summary = "Reported accounts do not provide usable business-health inputs."
    if comparable:
        strongest = max(comparable, key=lambda name: comparable[name])
        weakest = min(comparable, key=lambda name: comparable[name])
        if comparable[strongest] > 0:
            summary += f" The clearest relative strength is {_FUNDAMENTAL_LABELS[strongest]}."
        if comparable[weakest] < 0:
            summary += f" The largest relative shortfall is {_FUNDAMENTAL_LABELS[weakest]}."
        if all(z == 0 for z in comparable.values()):
            summary += " The comparable business measures are in line with peers."
    missing = [_FUNDAMENTAL_LABELS[name] for name in SUB_METRIC_NAMES if values[name] is None]
    unrankable = [
        _FUNDAMENTAL_LABELS[name] for name in SUB_METRIC_NAMES
        if values[name] is not None and name not in comparable
    ]
    if missing:
        summary += f" Usable reported inputs are missing for {_join(missing)}."
    if unrankable:
        summary += f" A reliable peer comparison is unavailable for {_join(unrankable)}."
    return _Observation(
        summary,
        evidence,
        "There are too few usable business-health comparisons to establish a current assessment.",
        "combined business-health picture",
        unavailable=_finite(context.fundamental_health.value) is None,
    )


def _valuation(context: LegEvidenceContext, evidence: list[ScoreEvidence]) -> _Observation:
    clauses: list[str] = []
    comparable = False
    for name, label in _VALUATION_LABELS.items():
        value = _finite(getattr(context.valuation.metrics, name))
        signal = _finite(context.valuation_signals.get(name))
        if value is None:
            clauses.append(f"{label} is unavailable.")
        elif value <= 0:
            clauses.append(f"{label} is not positive and is excluded, rather than treated as evidence of a bargain.")
        elif signal is None:
            clauses.append(f"{label} is available, but a reliable peer comparison is not.")
        else:
            comparable = True
            if name == "fcf_yield":
                relation = "higher than" if signal > 0 else "lower than" if signal < 0 else "similar to"
                clauses.append(
                    f"Cash left after investment relative to the company's market value is {relation} peers'."
                )
            else:
                basis = "sales" if name == "ev_sales" else "operating earnings"
                if signal == 0:
                    clauses.append(f"The business's valuation relative to {basis} is similar to peers'.")
                else:
                    relation = "less" if signal > 0 else "more"
                    clauses.append(f"The business is valued {relation} richly relative to {basis} than peers.")
    no_market_cap = context.valuation.reason == VALUATION_REASON_NO_MARKET_CAP
    return _Observation(
        " ".join(clauses),
        evidence,
        "The company's market value is unavailable, so its valuation cannot be related to sales, earnings or cash flow."
        if no_market_cap else
        "Usable positive valuation measures and sufficiently varied peer observations are needed for a comparison.",
        "combined valuation picture",
        unavailable=no_market_cap or not comparable,
    )


def _finish(
    observation: _Observation,
    leg: LegCompositeResult | None,
    *,
    stale: bool,
) -> LegExplanation:
    summary = observation.summary
    if stale:
        return LegExplanation(
            summary=(
                summary + " These observations are not used in a current assessment "
                "because the issuer's inputs are stale."
            ),
            effect="unavailable",
            evidence=observation.evidence,
        )
    if leg is not None and not leg.applicable:
        return LegExplanation(
            summary=summary + " This measure is not applicable to the current assessment.",
            effect="not_applicable",
            evidence=observation.evidence,
        )
    if (
        observation.unavailable or leg is None or not leg.computable
        or _finite(leg.raw) is None or _finite(leg.contribution) is None
    ):
        reason = observation.missing_reason
        if leg is not None and leg.reason_not_computable == REASON_DEGENERATE_CROSS_SECTION:
            reason = (
                "There are too few comparable observations, or too little variation among peers, "
                "to establish a meaningful comparison."
            )
        return LegExplanation(summary=f"{summary} {reason}", effect="unavailable", evidence=observation.evidence)
    contribution = leg.contribution
    assert contribution is not None
    transition = "Even so, this" if leg.raw is not None and leg.raw * contribution < 0 else "This"
    if contribution > 0:
        effect = "supports"
        summary += f" {transition} {observation.subject} supports the overall assessment relative to peers."
    elif contribution < 0:
        effect = "weighs"
        summary += f" {transition} {observation.subject} weighs on the overall assessment relative to peers."
    else:
        effect = "neutral"
        summary += f" This {observation.subject} has no net effect on the overall assessment."
    return LegExplanation(summary=summary, effect=effect, evidence=observation.evidence)


def build_leg_explanations(
    context: LegEvidenceContext, score: SecurityScoreResult,
) -> dict[LegName, LegExplanation]:
    """Describe all six legs using only this issuer's point-in-time evidence.

    Effects follow the final contribution, not the sign of a raw observation.
    A missing or unrankable measure is never described as a neutral measurement.
    The pipeline owns source verification and supplies the eligible subset;
    issuer/date bounds remain local, and an empty extraction alone never proves
    a completed theme review without the pipeline's observed zero result.
    Financial citations identify supplied reported-data records, not fabricated
    documents or an asserted line-by-line provenance of a derived ratio.
    """

    if context.security.id != score.security_id:
        raise ValueError("Score and evidence must refer to the same security.")
    documents, extractions = _visible_inputs(context)
    financial_evidence = _financial_evidence(context)
    legs = score.composite_result.legs if score.composite_result else {}
    observations = {
        LegName.THESIS_LINKAGE: _thesis(context, documents, extractions, legs.get(LegName.THESIS_LINKAGE)),
        LegName.ATTENTION_ACCELERATION: _attention(context, documents, extractions),
        LegName.NARRATIVE_PREMIUM: _narrative(
            context, documents, extractions, financial_evidence, legs.get(LegName.NARRATIVE_PREMIUM),
        ),
        LegName.SMART_MONEY: _smart_money(context, documents, legs.get(LegName.SMART_MONEY)),
        LegName.FUNDAMENTAL_HEALTH: _fundamentals(context, financial_evidence),
        LegName.VALUATION_BRAKE: _valuation(context, financial_evidence),
    }
    explanations = {
        name: _finish(observations[name], legs.get(name), stale=score.excluded_stale) for name in LegName
    }
    if context.security.filer_profile == FilerProfile.FPI:
        explanations[LegName.SMART_MONEY] = LegExplanation(
            summary=(
                "Foreign private issuers are not subject to the same routine insider-transaction reporting "
                "as domestic issuers, so this insider-trading comparison does not apply."
            ),
            effect="not_applicable",
        )
    if context.valuation.fx_unavailable or context.valuation.reason == VALUATION_REASON_FX_UNAVAILABLE:
        currency = context.valuation.reporting_currency
        reporting = f"The accounts are reported in {currency}. " if currency else ""
        explanations[LegName.VALUATION_BRAKE] = LegExplanation(
            summary=(
                reporting + "An authoritative historical currency conversion is unavailable, so valuation "
                "cannot be put on a comparable currency basis with peers."
            ),
            effect="not_applicable",
            evidence=financial_evidence,
        )
    return explanations
