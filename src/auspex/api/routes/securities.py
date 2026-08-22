"""Security universe and per-security package endpoints (arc42 §11).

`GET /api/securities` and `GET /api/securities/{id}` follow the same
universe-plus-latest-score composition pattern `scores.py` already uses
(`Universe` from config, not a Cosmos ``securities`` container — the
universe is loaded once from `config/universe.yaml` and every security id
is stable across reloads). History and documents are plain Cosmos queries
scoped to the security's own partition (`/security_id`).

Response shapes are reconciled 1:1 with `web/src/lib/types.ts`
(`SecuritySummary`, `SecurityPackage`) — see `auspex.api.schemas` for the
field-by-field mapping rationale.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from auspex.api.auth import AuthenticatedUser, get_current_user
from auspex.api.deps import (
    get_fundamental_repo,
    get_price_sink,
    get_recommendation_repo,
    get_score_repo,
    get_universe,
)
from auspex.api.explanations import score_reasoning
from auspex.api.repos import get_digest_repo, get_document_repo
from auspex.api.schemas import (
    FundamentalMetricOut,
    LegDetail,
    SecurityDocumentOut,
    SecurityHistoryPoint,
    SecurityPackage,
    SecurityPricePoint,
    SecuritySummary,
    SecuritySummaryWithProfile,
)
from auspex.api.viewmodels import build_recommendation_out
from auspex.config.loader import Universe, load_xbrl_concepts
from auspex.models.document import Document
from auspex.models.enums import DocumentType, LegName
from auspex.models.extraction import ChannelBDigest
from auspex.models.fundamentals import FundamentalSnapshot
from auspex.models.policy import Recommendation
from auspex.models.scoring import ScoreSnapshot
from auspex.models.security import Security
from auspex.persistence.repositories import CosmosPriceSink, CosmosRepository
from auspex.pipeline.feature_builder import (
    build_fundamental_health_inputs,
    build_valuation_metrics,
)
from auspex.scoring.normalize import percentile_rank

router = APIRouter(prefix="/securities", tags=["securities"])


def _format_pct(value: Decimal | None) -> str | None:
    return f"{(value * Decimal(100)):.1f}%" if value is not None else None


def _format_money(value: str | None, currency: str) -> str | None:
    if value is None:
        return None
    amount = Decimal(value)
    if abs(amount) >= Decimal("1000000000"):
        return f"{currency} {amount / Decimal('1000000000'):.2f}B"
    if abs(amount) >= Decimal("1000000"):
        return f"{currency} {amount / Decimal('1000000'):.1f}M"
    return f"{currency} {amount:,.0f}"


def _compact_recap(value: str, max_chars: int = 480) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) <= max_chars:
        return cleaned
    candidate = cleaned[: max_chars + 1]
    sentence_end = max(candidate.rfind(". "), candidate.rfind("; "))
    if sentence_end >= max_chars // 2:
        return candidate[: sentence_end + 1]
    return f"{cleaned[: max_chars - 3].rstrip()}..."


def _digest_text(digest: ChannelBDigest | None) -> str:
    if digest is None:
        return ""
    plain = (digest.plain_summary or "").strip()
    if digest.prompt_version == "digest-b-v2":
        return (
            plain
            if plain and digest.plain_summary_evidence
            else digest.digest.strip()
        )
    return digest.digest.strip()


def _preferred_digests(
    digests: list[ChannelBDigest],
) -> dict[str, ChannelBDigest]:
    def preference(digest: ChannelBDigest) -> tuple[int, int, str]:
        return (
            1 if digest.prompt_version == "digest-b-v2" else 0,
            1 if (digest.plain_summary or "").strip() else 0,
            digest.id,
        )

    selected: dict[str, ChannelBDigest] = {}
    for digest in digests:
        current = selected.get(digest.document_id)
        if current is None or preference(digest) > preference(current):
            selected[digest.document_id] = digest
    return selected


def _business_recap(
    security: Security,
    score: ScoreSnapshot,
    annual_update: SecurityDocumentOut | None,
    documents: list[SecurityDocumentOut],
    news: list[SecurityDocumentOut],
    *,
    displayed_document_count: int,
    digest_count: int,
) -> str:
    updates: list[tuple[str, SecurityDocumentOut]] = []
    latest_company_filing = next(
        (
            document
            for document in documents
            if document.form not in {"4", "NEWS"} and document.digest.strip()
        ),
        None,
    )
    if latest_company_filing is not None:
        updates.append(
            (f"Latest {latest_company_filing.form} filing", latest_company_filing)
        )
    if (
        annual_update is not None
        and annual_update.digest.strip()
        and (
            latest_company_filing is None
            or annual_update.document_id != latest_company_filing.document_id
        )
    ):
        updates.append(("Latest annual filing", annual_update))
    latest_form4 = next(
        (document for document in documents if document.form == "4"),
        None,
    )
    if latest_form4 is not None and latest_form4.digest.strip():
        updates.append(("Latest insider filing", latest_form4))
    if news and news[0].digest.strip():
        updates.append(("Latest news", news[0]))
    if not updates:
        latest_filing = next(
            (document for document in documents if document.digest.strip()),
            None,
        )
        if latest_filing is not None:
            updates.append((f"Latest {latest_filing.form} filing", latest_filing))
    if updates:
        summaries = [
            f"{label}: {_compact_recap(update.digest, 170)}"
            for label, update in updates[:4]
        ]
        return _compact_recap(" ".join(summaries), 720)

    cohort = security.cohort.replace("-", " ")
    score_text = (
        f"Its latest Auspex Score is {score.percentile}/100"
        if score.percentile is not None
        else "Its latest Auspex Score is not currently available"
    )
    coverage_pct = Decimal(score.coverage) * Decimal(100)
    package_text = (
        f"The current package includes {displayed_document_count} displayed regulatory "
        f"evidence items and {digest_count} grounded digests."
    )
    recap = (
        f"A plain-language company overview is not yet available for {security.name}. "
        f"Auspex compares it with other companies in the {cohort} group on "
        f"{security.exchange}. {score_text}, using reliable information for "
        f"{coverage_pct:.0f}% of the applicable research areas. {package_text}"
    )
    return recap


def _fundamentals(
    rows: list[FundamentalSnapshot],
    as_of: date,
    current_price: Decimal | None,
) -> list[FundamentalMetricOut]:
    config = load_xbrl_concepts()
    inputs = build_fundamental_health_inputs(rows, config, Decimal("0.21"), as_of)
    facts = [fact for row in rows for fact in row.facts if fact.filed <= as_of]
    latest_end = max((fact.end for fact in facts), default=None)
    revenue_aliases = set(config["concepts"]["revenues"])
    revenue_facts = sorted(
        (fact for fact in facts if fact.concept in revenue_aliases),
        key=lambda fact: (fact.end, fact.filed),
    )
    latest_revenue_fact = revenue_facts[-1] if revenue_facts else None
    latest_revenue = latest_revenue_fact.value if latest_revenue_fact else None
    revenue_currency = (
        latest_revenue_fact.unit
        if latest_revenue_fact is not None
        and len(latest_revenue_fact.unit) == 3
        and latest_revenue_fact.unit.isalpha()
        else "USD"
    )
    shares_aliases = set(config["concepts"]["shares_outstanding"])
    share_facts = sorted(
        (
            fact
            for fact in facts
            if fact.concept in shares_aliases and fact.unit == "shares"
        ),
        key=lambda fact: (fact.end, fact.filed),
    )
    shares = Decimal(share_facts[-1].value) if share_facts else None
    market_cap = (
        current_price * shares
        if current_price is not None and shares is not None
        else None
    )
    valuation = build_valuation_metrics(market_cap, rows, config, as_of).metrics
    eps_aliases = set(config["concepts"]["diluted_eps"])
    annual_eps = sorted(
        (
            fact
            for fact in facts
            if (
                fact.concept in eps_aliases
                and fact.fp == "FY"
                and fact.unit == "USD/shares"
            )
        ),
        key=lambda fact: (fact.end, fact.filed),
    )
    latest_eps = Decimal(annual_eps[-1].value) if annual_eps else None
    pe_ratio = (
        current_price / latest_eps
        if current_price is not None and latest_eps is not None and latest_eps > 0
        else None
    )
    return [
        FundamentalMetricOut(
            label="Latest revenue",
            value=_format_money(latest_revenue, revenue_currency),
            period_end=latest_revenue_fact.end if latest_revenue_fact else latest_end,
        ),
        FundamentalMetricOut(
            label="Revenue growth YoY",
            value=_format_pct(inputs.revenue_growth_yoy),
            period_end=latest_end,
        ),
        FundamentalMetricOut(
            label="Gross-margin trend",
            value=_format_pct(inputs.gross_margin_trend_slope),
            period_end=latest_end,
        ),
        FundamentalMetricOut(label="FCF margin", value=_format_pct(inputs.fcf_margin), period_end=latest_end),
        FundamentalMetricOut(
            label="Net cash / assets",
            value=_format_pct(inputs.net_cash_ratio),
            period_end=latest_end,
        ),
        FundamentalMetricOut(label="ROIC", value=_format_pct(inputs.roic), period_end=latest_end),
        FundamentalMetricOut(
            label="P / E (latest FY)",
            value=f"{pe_ratio:.1f}x" if pe_ratio is not None else None,
            period_end=annual_eps[-1].end if annual_eps else latest_end,
        ),
        FundamentalMetricOut(
            label="EV / Sales",
            value=f"{valuation.ev_sales:.1f}x" if valuation.ev_sales is not None else None,
            period_end=latest_end,
        ),
        FundamentalMetricOut(
            label="FCF yield",
            value=_format_pct(valuation.fcf_yield),
            period_end=latest_end,
        ),
    ]


def _leg_scores(
    score: ScoreSnapshot,
    population: list[ScoreSnapshot],
) -> dict[str, LegDetail]:
    details: dict[str, LegDetail] = {}
    for leg, result in score.legs.items():
        population_values = [
            Decimal(candidate.legs[leg].z)
            for candidate in population
            if leg in candidate.legs and candidate.legs[leg].z is not None
        ]
        display_score = (
            percentile_rank(Decimal(result.z), population_values)
            if result.z is not None and population_values
            else None
        )
        status_explanation = result.reason_not_computable
        neutral = False
        if (
            leg == LegName.SMART_MONEY
            and result.raw is not None
            and result.z is None
            and all(
                candidate.legs.get(leg) is None
                or candidate.legs[leg].raw is None
                or Decimal(candidate.legs[leg].raw) == 0
                for candidate in population
            )
        ):
            neutral = True
            status_explanation = (
                "No meaningful insider buying or selling was recorded recently, "
                "so this area is neutral rather than missing."
            )
        details[leg.value] = LegDetail(
            raw=result.raw,
            z=result.z,
            weight=result.weight,
            contribution=result.contribution,
            computable=result.computable,
            score=display_score,
            neutral=neutral,
            status_explanation=status_explanation,
        )
    return details


async def _latest_score(repo: CosmosRepository, security_id: str) -> ScoreSnapshot | None:
    rows = await repo.query(
        query="SELECT TOP 1 * FROM c WHERE c.security_id = @security_id ORDER BY c.as_of_date DESC",
        parameters=[{"name": "@security_id", "value": security_id}],
        partition_key=security_id,
    )
    return rows[0] if rows else None


async def _latest_recommendation(repo: CosmosRepository, user_id: str, security_id: str) -> Recommendation | None:
    rows = await repo.query(
        query=(
            "SELECT TOP 1 * FROM c WHERE c.user_id = @user_id AND c.security_id = @security_id "
            "ORDER BY c.as_of_date DESC"
        ),
        parameters=[{"name": "@user_id", "value": user_id}, {"name": "@security_id", "value": security_id}],
        partition_key=user_id,
    )
    return rows[0] if rows else None


def _form4_text(document: Document) -> tuple[str, str]:
    owners = list(dict.fromkeys(transaction.owner_name for transaction in document.insider_transactions))
    owner_label = ", ".join(owners[:2]) or "Insider"
    labels = {
        "P": "open-market purchase",
        "S": "open-market sale",
        "M": "option exercise",
        "F": "tax withholding",
        "A": "grant or award",
        "G": "gift",
    }
    transactions = [
        (
            f"{labels.get(transaction.transaction_code.value, transaction.transaction_code.value)} "
            f"of {Decimal(transaction.shares):,.0f} shares"
        )
        for transaction in document.insider_transactions
    ]
    headline = f"Form 4 — {owner_label}"
    summary = "; ".join(transactions) or "No non-derivative transaction rows were parsed."
    return headline, summary


def _source_url(document: Document, security: Security) -> str:
    if document.url:
        return document.url
    if document.form_type == "4" and document.accession_number and security.cik:
        accession = document.accession_number
        return (
            "https://www.sec.gov/Archives/edgar/data/"
            f"{int(security.cik)}/{accession.replace('-', '')}/{accession}-index.html"
        )
    return ""


def _map_document(
    document: Document,
    digest: ChannelBDigest | None,
    security: Security,
) -> SecurityDocumentOut:
    filed_at = (
        document.filed_date
        or (document.published_at.date() if document.published_at else None)
        or document.knowledge_date
    )
    form4_headline, form4_digest = (
        _form4_text(document)
        if document.form_type == "4"
        else ("", "")
    )
    if document.document_type.value == "NEWS":
        relevance_reason = (
            f"Headline explicitly references {security.ticker} or {security.name}."
        )
    elif document.form_type == "4":
        relevance_reason = "Insider ownership filing for the selected issuer."
    else:
        relevance_reason = "Regulatory filing submitted by the selected issuer."
    return SecurityDocumentOut(
        document_id=document.id,
        form=document.form_type or document.document_type.value,
        filed_at=filed_at.isoformat(),
        headline=digest.headline if digest else (document.title or form4_headline),
        digest=(
            _digest_text(digest)
            if digest
            else form4_digest
            or document.content_excerpt
            or ""
        ),
        source_url=_source_url(document, security),
        publisher=document.source.upper(),
        retrieved_at=document.retrieved_at,
        relevance_reason=relevance_reason,
        stale=(date.today() - filed_at).days > 180,
    )


def _news_is_relevant(document: Document, security: Security) -> bool:
    title = document.title or ""
    if re.search(rf"\b{re.escape(security.ticker)}\b", title, flags=re.IGNORECASE):
        return True
    company = re.sub(
        r"\b(incorporated|inc|corporation|corp|limited|ltd|plc)\b\.?",
        "",
        security.name,
        flags=re.IGNORECASE,
    )
    company = re.sub(r"[^a-z0-9]+", " ", company.lower()).strip()
    normalized_title = re.sub(r"[^a-z0-9]+", " ", title.lower())
    return bool(company and company in normalized_title)


@router.get("", response_model=list[SecuritySummary])
async def list_securities(
    user: AuthenticatedUser = Depends(get_current_user),
    universe: Universe = Depends(get_universe),
    score_repo: CosmosRepository = Depends(get_score_repo),
    recommendation_repo: CosmosRepository = Depends(get_recommendation_repo),
) -> list[SecuritySummary]:
    latest_row = await score_repo.query(
        query="SELECT TOP 1 * FROM c ORDER BY c.as_of_date DESC",
    )
    latest_date = latest_row[0].as_of_date if latest_row else date.today()
    latest_scores = (
        await score_repo.query(
            query="SELECT * FROM c WHERE c.as_of_date=@as_of_date",
            parameters=[{"name": "@as_of_date", "value": latest_date.isoformat()}],
        )
        if latest_row
        else []
    )
    scores_by_id = {score.security_id: score for score in latest_scores}
    recommendations = await recommendation_repo.query(
        query="SELECT * FROM c WHERE c.user_id=@user_id AND c.as_of_date=@as_of_date",
        parameters=[
            {"name": "@user_id", "value": user.user_id},
            {"name": "@as_of_date", "value": latest_date.isoformat()},
        ],
        partition_key=user.user_id,
    )
    recommendations_by_id = {row.security_id: row for row in recommendations}
    summaries: list[SecuritySummary] = []
    for security in universe.securities:
        score = scores_by_id.get(security.id)
        recommendation = recommendations_by_id.get(security.id)
        summaries.append(
            SecuritySummary(
                security_id=security.id,
                ticker=security.ticker,
                name=security.name,
                market=security.exchange,
                cohort=security.cohort,
                score=score.composite if score else None,
                percentile=score.percentile if score else None,
                direction=score.direction if score else None,
                coverage=score.coverage if score else None,
                action=recommendation.action if recommendation else None,
            )
        )
    return summaries


@router.get("/{security_id}", response_model=SecurityPackage)
async def get_security(
    security_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    universe: Universe = Depends(get_universe),
    score_repo: CosmosRepository = Depends(get_score_repo),
    recommendation_repo: CosmosRepository = Depends(get_recommendation_repo),
    document_repo: CosmosRepository = Depends(get_document_repo),
    digest_repo: CosmosRepository = Depends(get_digest_repo),
    fundamental_repo: CosmosRepository = Depends(get_fundamental_repo),
    price_sink: CosmosPriceSink = Depends(get_price_sink),
) -> SecurityPackage:
    security = universe.by_id().get(security_id)
    if security is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown security {security_id!r}")

    score = await _latest_score(score_repo, security_id)
    if score is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no score for this security")

    recommendation = await _latest_recommendation(recommendation_repo, user.user_id, security_id)

    summary = SecuritySummaryWithProfile(
        security_id=security.id,
        ticker=security.ticker,
        name=security.name,
        market=security.exchange,
        cohort=security.cohort,
        score=score.composite,
        percentile=score.percentile,
        direction=score.direction,
        coverage=score.coverage,
        action=recommendation.action if recommendation else None,
        filer_profile=security.filer_profile,
    )

    score_population = await score_repo.query(
        query="SELECT * FROM c WHERE c.as_of_date=@as_of_date",
        parameters=[{"name": "@as_of_date", "value": score.as_of_date.isoformat()}],
    )
    legs = _leg_scores(
        score,
        [
            candidate
            for candidate in score_population
            if candidate.cohort_used == score.cohort_used
        ],
    )

    recommendation_out = (
        build_recommendation_out(recommendation, security.ticker, security.name, score)
        if recommendation is not None
        else None
    )

    history_rows = await score_repo.query(
        query="SELECT * FROM c WHERE c.security_id = @security_id ORDER BY c.as_of_date ASC",
        parameters=[{"name": "@security_id", "value": security_id}],
        partition_key=security_id,
    )
    history = [
        SecurityHistoryPoint(as_of_date=row.as_of_date, composite=row.composite, percentile=row.percentile)
        for row in history_rows
        if row.composite is not None and row.percentile is not None
    ]

    documents = await document_repo.query(
        query=(
            "SELECT TOP 20 * FROM c WHERE c.security_id = @security_id "
            "AND c.document_type != 'NEWS' ORDER BY c.knowledge_date DESC"
        ),
        parameters=[{"name": "@security_id", "value": security_id}],
        partition_key=security_id,
    )
    news_documents = await document_repo.query(
        query=(
            "SELECT TOP 50 * FROM c WHERE c.security_id = @security_id "
            "AND c.document_type = 'NEWS' ORDER BY c.knowledge_date DESC"
        ),
        parameters=[{"name": "@security_id", "value": security_id}],
        partition_key=security_id,
    )
    annual_documents = await document_repo.query(
        query=(
            "SELECT TOP 1 * FROM c WHERE c.security_id=@security_id "
            "AND (c.document_type='10-K' OR c.document_type='20-F') "
            "ORDER BY c.knowledge_date DESC"
        ),
        parameters=[{"name": "@security_id", "value": security_id}],
        partition_key=security_id,
    )
    digests = await digest_repo.query(
        query="SELECT * FROM c WHERE c.security_id = @security_id",
        parameters=[{"name": "@security_id", "value": security_id}],
        partition_key=security_id,
    )
    digest_by_document_id = _preferred_digests(digests)
    documents_out = [
        _map_document(document, digest_by_document_id.get(document.id), security)
        for document in documents
    ]
    news = [
        _map_document(document, digest_by_document_id.get(document.id), security)
        for document in news_documents
        if _news_is_relevant(document, security)
    ][:3]
    annual_document = next(
        (
            document
            for document in annual_documents
            if document.document_type
            in {DocumentType.FORM_10K, DocumentType.FORM_20F}
        ),
        None,
    )
    if annual_document is None:
        annual_document = next(
            (
                document
                for document in documents
                if document.document_type
                in {DocumentType.FORM_10K, DocumentType.FORM_20F}
            ),
            None,
        )
    annual_digest = digest_by_document_id.get(annual_document.id) if annual_document else None
    annual_update = (
        _map_document(annual_document, annual_digest, security)
        if annual_document is not None
        else None
    )
    business_summary = _business_recap(
        security,
        score,
        annual_update,
        documents_out,
        news,
        displayed_document_count=len(documents_out),
        digest_count=len(digests),
    )
    price_rows = await price_sink.history_as_of(security_id, score.as_of_date, 15)
    current_price = Decimal(price_rows[-1].close_adjusted) if price_rows else None
    prior_price = Decimal(price_rows[-2].close_adjusted) if len(price_rows) >= 2 else None
    price_change_pct = (
        (current_price - prior_price) / prior_price * Decimal(100)
        if current_price is not None and prior_price not in (None, Decimal(0))
        else None
    )
    fundamental_rows = await fundamental_repo.query(
        query="SELECT * FROM c WHERE c.security_id=@security_id",
        parameters=[{"name": "@security_id", "value": security_id}],
        partition_key=security_id,
    )
    prior_score = next(
        (row for row in reversed(history_rows) if row.as_of_date < score.as_of_date),
        None,
    )

    return SecurityPackage(
        security=summary,
        as_of_date=score.as_of_date,
        narrative=score.narrative or "",
        legs=legs,
        recommendation=recommendation_out,
        market=security.exchange,
        business_summary=business_summary,
        current_price_usd=str(current_price) if current_price is not None else None,
        price_change_pct=str(price_change_pct) if price_change_pct is not None else None,
        price_history=[
            SecurityPricePoint(date=row.session_date, close=row.close_adjusted)
            for row in price_rows
        ],
        fundamentals=_fundamentals(
            fundamental_rows,
            score.as_of_date,
            current_price,
        ),
        score_change=(
            score.percentile - prior_score.percentile
            if score.percentile is not None
            and prior_score is not None
            and prior_score.percentile is not None
            else None
        ),
        score_reasoning=score_reasoning(
            score,
            prior_score,
            {name: detail.score for name, detail in legs.items()},
        ),
        news=news,
        history=history,
        documents=documents_out,
    )


@router.get("/{security_id}/history", response_model=list[ScoreSnapshot])
async def get_security_history(
    security_id: str,
    date_from: str = Query(alias="from"),
    date_to: str = Query(alias="to"),
    user: AuthenticatedUser = Depends(get_current_user),
    score_repo: CosmosRepository = Depends(get_score_repo),
) -> list[ScoreSnapshot]:
    return await score_repo.query(
        query=(
            "SELECT * FROM c WHERE c.security_id = @security_id "
            "AND c.as_of_date >= @from AND c.as_of_date <= @to ORDER BY c.as_of_date ASC"
        ),
        parameters=[
            {"name": "@security_id", "value": security_id},
            {"name": "@from", "value": date_from},
            {"name": "@to", "value": date_to},
        ],
        partition_key=security_id,
    )


@router.get("/{security_id}/documents", response_model=list[SecurityDocumentOut])
async def get_security_documents(
    security_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    universe: Universe = Depends(get_universe),
    document_repo: CosmosRepository = Depends(get_document_repo),
    digest_repo: CosmosRepository = Depends(get_digest_repo),
) -> list[SecurityDocumentOut]:
    security = universe.by_id().get(security_id)
    if security is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown security {security_id!r}",
        )
    documents = await document_repo.query(
        query="SELECT * FROM c WHERE c.security_id = @security_id ORDER BY c.knowledge_date DESC",
        parameters=[{"name": "@security_id", "value": security_id}],
        partition_key=security_id,
    )
    digests = await digest_repo.query(
        query="SELECT * FROM c WHERE c.security_id = @security_id",
        parameters=[{"name": "@security_id", "value": security_id}],
        partition_key=security_id,
    )
    digest_by_document_id = _preferred_digests(digests)
    return [
        _map_document(document, digest_by_document_id.get(document.id), security)
        for document in documents
    ]
