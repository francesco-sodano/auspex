from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from auspex.models.document import Document, InsiderTransaction
from auspex.models.enums import (
    DocumentType,
    ExtractionConfidence,
    FilerProfile,
    Form4TransactionCode,
    GuidanceDirection,
    LegName,
    Materiality,
    NarrativeClaimType,
    Novelty,
    RiskCategory,
    RiskSeverity,
    Sentiment,
    ThemeStrength,
)
from auspex.models.extraction import ChannelAExtraction, NarrativeClaim, RiskClaim, ThemeClaim
from auspex.models.fundamentals import FundamentalSnapshot, XbrlFact
from auspex.models.security import Security
from auspex.pipeline.feature_builder import (
    VALUATION_REASON_FX_UNAVAILABLE,
    VALUATION_REASON_NO_MARKET_CAP,
    ValuationBuildResult,
    WeightsConfig,
    build_attention_events,
)
from auspex.pipeline.score_explanations import LegEvidenceContext, build_leg_explanations
from auspex.scoring.composite import compute_security_composite
from auspex.scoring.engine import SecurityScoreResult
from auspex.scoring.legs import (
    FundamentalHealthInputs,
    FundamentalHealthResult,
    ValuationMetrics,
    attention_acceleration,
    valuation_brake,
    valuation_metric_signals,
)

D = Decimal
AS_OF = date(2026, 9, 18)
SECURITY = Security(
    id="issuer", ticker="TEST", cik="0000123456", name="Test Issuer",
    cohort="technology", filer_profile=FilerProfile.DOMESTIC,
)
WEIGHTS = WeightsConfig(
    document_authority={"10-K": D(1), "10-Q": D("0.9"), "8-K": D("0.7"), "news": D("0.4")},
    theme_strength={"STRONG": D(1), "MODERATE": D("0.6"), "WEAK": D("0.25")},
    materiality_weight={"HIGH": D(1), "MEDIUM": D("0.5"), "LOW": D("0.2"), "NONE": D(0)},
    recency_half_life_days=D(90),
    roic_tax_rate=D("0.21"),
)


def _context(**changes) -> LegEvidenceContext:
    context = LegEvidenceContext(
        security=SECURITY,
        as_of_date=AS_OF,
        documents=[],
        extractions=[],
        fundamentals=[],
        weights=WEIGHTS,
        taxonomy_labels={"compute": "AI infrastructure", "automation": "Industrial automation"},
        fundamental_inputs=FundamentalHealthInputs(None, None, None, None, None),
        fundamental_health=FundamentalHealthResult(None, {}, 0, D(0)),
        valuation=ValuationBuildResult(ValuationMetrics(None, None, None), "USD"),
        valuation_signals={},
        growth_percentile=None,
    )
    return replace(context, **changes)


def _score(
    name: LegName = LegName.THESIS_LINKAGE,
    raw: Decimal | None = D(0),
    peers: tuple[Decimal, ...] = (D(-1), D(1)),
    *,
    applicable: bool = True,
) -> SecurityScoreResult:
    composite = compute_security_composite(
        leg_raw_by_leg={name: raw},
        cohort_raw_by_leg={name: {"issuer": raw, **{f"peer-{i}": value for i, value in enumerate(peers)}}},
        weights={name: D(1)},
        security_id=SECURITY.id,
        not_applicable_legs=frozenset() if applicable else frozenset({name}),
    )
    return SecurityScoreResult(
        security_id=SECURITY.id, excluded_stale=False, cohort_scope=None, composite_result=composite,
        coverage=D(int(composite.legs[name].computable)), percentile=None,
    )


def _document(
    document_id: str,
    days_ago: int = 1,
    kind: DocumentType = DocumentType.FORM_10K,
    *,
    security_id: str = SECURITY.id,
    transactions: list[InsiderTransaction] | None = None,
) -> Document:
    known = AS_OF - timedelta(days=days_ago)
    return Document(
        id=document_id, security_id=security_id, source="manual", source_record_id=document_id,
        document_type=kind, form_type=kind.value, filed_date=known,
        title=f"Source {document_id}", url=f"https://example.test/disclosures/{document_id}",
        content_hash=f"hash-{document_id}", retrieved_at=datetime(2026, 9, 18, tzinfo=UTC),
        knowledge_date=known, insider_transactions=transactions or [],
    )


def _theme(theme: str = "compute", excerpt: str = "We manufacture AI infrastructure.") -> ThemeClaim:
    return ThemeClaim(theme_id=theme, strength=ThemeStrength.STRONG, evidence_excerpt=excerpt)


def _extraction(
    document: Document,
    *,
    themes: list[ThemeClaim] | None = None,
    narratives: list[NarrativeClaim] | None = None,
    materiality: Materiality = Materiality.HIGH,
) -> ChannelAExtraction:
    return ChannelAExtraction(
        id=f"extraction-{document.id}", security_id=document.security_id, document_id=document.id,
        content_hash=document.content_hash, model_version="test", taxonomy_version="test",
        materiality=materiality, sentiment=Sentiment.NEUTRAL, guidance_direction=GuidanceDirection.NONE,
        novelty=Novelty.NEW_INFORMATION, theme_claims=themes or [], narrative_claims=narratives or [],
        extraction_confidence=ExtractionConfidence.HIGH,
    )


def _transaction(
    code: Form4TransactionCode,
    shares: str = "10",
    *,
    owner: str = "Jane Issuer",
    days_ago: int = 1,
    officer: bool = True,
    director: bool = False,
    substantial_owner: bool = False,
    price: str = "1",
) -> InsiderTransaction:
    return InsiderTransaction(
        owner_name=owner, is_officer=officer, is_director=director, is_ten_percent_owner=substantial_owner,
        transaction_code=code, transaction_date=AS_OF - timedelta(days=days_ago),
        shares=shares, price_per_share=price,
    )


def _financial(
    accession: str = "0000123456-26-000001",
    days_ago: int = 10,
    *,
    security_id: str = SECURITY.id,
    fact_days_ago: int | None = None,
) -> FundamentalSnapshot:
    filed = AS_OF - timedelta(days=days_ago)
    fact_filed = filed if fact_days_ago is None else AS_OF - timedelta(days=fact_days_ago)
    return FundamentalSnapshot(
        id=f"{security_id}:{accession}", security_id=security_id, accn=accession,
        form="10-Q", fy=2026, fp="Q2", filed=filed,
        facts=[XbrlFact(
            concept="Revenues", value="100", accn=accession, fy=2026, fp="Q2",
            form="10-Q", end=date(2026, 6, 30), filed=fact_filed,
        )],
    )


def _health(
    inputs: FundamentalHealthInputs,
    signals: dict[str, Decimal | None],
    value: Decimal | None = D(0),
) -> FundamentalHealthResult:
    available = sum(inputs.as_map()[name] is not None and z is not None for name, z in signals.items())
    return FundamentalHealthResult(value, signals, available, D(available) / D(5))


def test_context_has_exact_frozen_interface() -> None:
    context = _context()
    assert [field.name for field in fields(context)] == [
        "security", "as_of_date", "documents", "extractions", "fundamentals", "weights",
        "taxonomy_labels", "fundamental_inputs", "fundamental_health", "valuation",
        "valuation_signals", "growth_percentile",
    ]
    with pytest.raises(FrozenInstanceError):
        context.__setattr__("as_of_date", date(2020, 1, 1))


@pytest.mark.parametrize("fpi,stale", [(False, False), (True, False), (False, True), (True, True)])
def test_all_six_legs_are_returned_even_without_composite(fpi: bool, stale: bool) -> None:
    security = SECURITY.model_copy(update={"filer_profile": FilerProfile.FPI if fpi else FilerProfile.DOMESTIC})
    score = replace(_score(raw=None), excluded_stale=stale, composite_result=None)
    result = build_leg_explanations(_context(security=security), score)

    assert list(result) == list(LegName)
    assert all(explanation.summary for explanation in result.values())
    for name, explanation in result.items():
        assert explanation.effect == (
            "not_applicable" if fpi and name == LegName.SMART_MONEY else "unavailable"
        )
        if stale and explanation.effect != "not_applicable":
            assert "stale" in explanation.summary


def test_thesis_names_actual_approved_themes_and_keeps_verbatim_evidence() -> None:
    filing = _document("filing")
    claim = _theme()
    second = _theme("automation", "Factory automation is an important product line.")
    extraction = _extraction(filing, themes=[claim, second, _theme("not-approved", "Unapproved story.")])
    result = build_leg_explanations(
        _context(documents=[filing], extractions=[extraction]), _score(raw=D("0.8"))
    )[LegName.THESIS_LINKAGE]

    assert result.effect == "supports"
    assert "AI infrastructure" in result.summary
    assert "Industrial automation" in result.summary
    assert "exposure, not proof of future success" in result.summary
    assert "not-approved" not in result.summary
    assert result.evidence[0].evidence_id == filing.id
    assert result.evidence[0].excerpt in {claim.evidence_excerpt, second.evidence_excerpt}
    assert result.evidence[0].source_url == filing.url


@pytest.mark.parametrize(
    "peers,effect",
    [
        ((D("0.2"), D("0.4")), "supports"),
        ((D("0.6"), D(1)), "neutral"),
        ((D("0.9"), D(1)), "weighs"),
    ],
)
def test_risk_only_theme_linkage_means_exposure_not_bullishness(peers, effect) -> None:
    filing = _document("export-controls", kind=DocumentType.FORM_10Q)
    excerpt = "Export controls may restrict our sales of datacenter processors."
    extraction = _extraction(filing, themes=[_theme("compute", excerpt)]).model_copy(update={
        "input_fingerprint": "verified-quarterly-text",
        "sentiment": Sentiment.NEGATIVE,
        "risk_claims": [
            RiskClaim(category=RiskCategory.REGULATORY, severity=RiskSeverity.HIGH, evidence_excerpt=excerpt),
        ],
    })
    score = _score(raw=D("0.8"), peers=peers)
    score_before = deepcopy(score)
    explanation = build_leg_explanations(
        _context(documents=[filing], extractions=[extraction]), score,
    )[LegName.THESIS_LINKAGE]

    assert explanation.effect == effect
    assert explanation.summary.startswith("Reports connect this company to AI infrastructure.")
    assert "can reflect risks as well as opportunities" in explanation.summary
    assert "exposure, not proof of future success" in explanation.summary
    assert "documented theme exposure" in explanation.summary
    assert "improved prospects" not in explanation.summary
    assert explanation.evidence[0].excerpt == excerpt
    assert score == score_before


def test_thesis_filters_window_issuer_orphans_and_future_filings() -> None:
    boundary = _document("boundary", 180)
    old = _document("old", 181)
    future = _document("future", -1)
    foreign = _document("foreign", security_id="other-issuer")
    unreviewed = _document("unreviewed")
    docs = [old, boundary, future, foreign, unreviewed]
    extractions = [_extraction(doc, themes=[_theme()]) for doc in docs[:-1]]
    extractions += [
        _extraction(_document("orphan"), themes=[_theme()]),
        _extraction(unreviewed, themes=[_theme()]).model_copy(update={"security_id": "other-issuer"}),
    ]
    context = _context(documents=docs, extractions=extractions)
    explanation = build_leg_explanations(context, _score(raw=D("0.3")))[LegName.THESIS_LINKAGE]

    assert [source.evidence_id for source in explanation.evidence] == ["boundary"]
    assert explanation.evidence[0].knowledge_date == boundary.knowledge_date


@pytest.mark.parametrize("kind", [DocumentType.FORM_10Q, DocumentType.NEWS])
@pytest.mark.parametrize("fingerprint", [None, "verified-section-input"])
def test_source_verified_pipeline_subset_is_not_revalidated_from_content_hash(
    kind: DocumentType, fingerprint: str | None,
) -> None:
    document = _document("verified-source", kind=kind)
    extraction = _extraction(document, themes=[_theme()]).model_copy(update={
        "content_hash": "pipeline-selected-source-hash",
        "input_fingerprint": fingerprint,
    })
    context = _context(documents=[document], extractions=[extraction])
    explanation = build_leg_explanations(context, _score(raw=D("0.8")))[LegName.THESIS_LINKAGE]
    attention = build_leg_explanations(
        context, _score(LegName.ATTENTION_ACCELERATION, D("0.3")),
    )[LegName.ATTENTION_ACCELERATION]

    assert explanation.effect == "supports"
    assert "AI infrastructure" in explanation.summary
    assert explanation.evidence[0].evidence_id == document.id
    assert explanation.evidence[0].excerpt == extraction.theme_claims[0].evidence_excerpt
    assert attention.effect == "supports"
    assert "increased" in attention.summary
    if kind == DocumentType.NEWS:
        assert "reviewed news reports" in attention.summary


def test_zero_reviewed_theme_matches_are_not_missing_evidence() -> None:
    filing = _document("reviewed")
    explanation = build_leg_explanations(
        _context(documents=[filing], extractions=[_extraction(filing)]), _score(raw=D(0))
    )[LegName.THESIS_LINKAGE]

    assert explanation.effect == "neutral"
    assert "no claims matching" in explanation.summary
    assert "completed review, not an evidence gap" in explanation.summary
    assert "missing" not in explanation.summary
    assert explanation.evidence[0].evidence_id == filing.id


@pytest.mark.parametrize("discarded", [0, 3])
@pytest.mark.parametrize("fingerprint", [None, "verified-section-input"])
def test_reviewed_empty_extraction_is_not_a_verified_absence_when_raw_is_missing(
    discarded: int, fingerprint: str | None,
) -> None:
    filing = _document("incomplete-review")
    extraction = _extraction(filing).model_copy(update={
        "input_fingerprint": fingerprint,
        "discarded_claim_count": discarded,
    })
    explanation = build_leg_explanations(
        _context(documents=[filing], extractions=[extraction]), _score(raw=None),
    )[LegName.THESIS_LINKAGE]

    assert explanation.effect == "unavailable"
    assert "unknown" in explanation.summary
    assert "missing or unverified" in explanation.summary
    assert "no claims matching" not in explanation.summary
    assert "completed review" not in explanation.summary
    assert ("discarded" in explanation.summary) == bool(discarded)
    assert explanation.evidence[0].evidence_id == filing.id
    assert explanation.evidence[0].excerpt is None


def test_zero_theme_match_result_can_weigh_without_becoming_missing_or_unverified() -> None:
    filing = _document("reviewed-zero")
    extraction = _extraction(filing).model_copy(update={
        "input_fingerprint": "verified-input",
        "discarded_claim_count": 1,
    })
    explanation = build_leg_explanations(
        _context(documents=[filing], extractions=[extraction]),
        _score(raw=D(0), peers=(D("0.4"), D("0.8"))),
    )[LegName.THESIS_LINKAGE]

    assert explanation.effect == "weighs"
    assert "completed review, not an evidence gap" in explanation.summary
    assert "missing or unverified" not in explanation.summary
    assert "unknown" not in explanation.summary
    assert "not a forecast of business success or failure" in explanation.summary


def test_out_of_scope_discarded_claims_do_not_change_a_verified_zero_theme_result() -> None:
    filing = _document("reviewed")
    excluded = [_document("future", -1), _document("old", 181), _document("foreign", security_id="other")]
    extractions = [_extraction(filing)] + [
        _extraction(doc).model_copy(update={"discarded_claim_count": 10}) for doc in excluded
    ]
    explanation = build_leg_explanations(
        _context(documents=[filing, *excluded], extractions=extractions), _score(raw=D(0)),
    )[LegName.THESIS_LINKAGE]

    assert explanation.effect == "neutral"
    assert "completed review" in explanation.summary
    assert "discarded" not in explanation.summary
    assert [item.evidence_id for item in explanation.evidence] == [filing.id]


def test_unreviewed_theme_disclosures_explain_the_gap() -> None:
    explanation = build_leg_explanations(
        _context(documents=[_document("unreviewed")]), _score(raw=None)
    )[LegName.THESIS_LINKAGE]

    assert explanation.effect == "unavailable"
    assert "not been reviewed" in explanation.summary
    assert "raw_value_missing" not in explanation.summary


def test_theme_links_can_weigh_despite_positive_absolute_evidence() -> None:
    filing = _document("weak")
    explanation = build_leg_explanations(
        _context(documents=[filing], extractions=[_extraction(filing, themes=[_theme()])]),
        _score(raw=D("0.2"), peers=(D("0.5"), D("0.9"))),
    )[LegName.THESIS_LINKAGE]

    assert explanation.effect == "weighs"
    assert "AI infrastructure" in explanation.summary
    assert "Even so" in explanation.summary


@pytest.mark.parametrize("contribution,effect", [(D(-1), "weighs"), (D(0), "neutral"), (D(1), "supports")])
def test_effect_follows_final_contribution_not_raw_or_peer_signal(contribution, effect) -> None:
    filing = _document("positive")
    score = _score(raw=D("0.5"))
    assert score.composite_result is not None
    leg = replace(score.composite_result.legs[LegName.THESIS_LINKAGE], contribution=contribution)
    score = replace(score, composite_result=replace(
        score.composite_result, legs={LegName.THESIS_LINKAGE: leg},
    ))

    explanation = build_leg_explanations(
        _context(documents=[filing], extractions=[_extraction(filing, themes=[_theme()])]), score,
    )[LegName.THESIS_LINKAGE]

    assert explanation.effect == effect
    assert "AI infrastructure" in explanation.summary


def test_degenerate_peer_group_is_not_missing_or_neutral_measurement() -> None:
    filing = _document("reviewed")
    explanation = build_leg_explanations(
        _context(documents=[filing], extractions=[_extraction(filing)]),
        _score(raw=D(0), peers=(D(0), D(0))),
    )[LegName.THESIS_LINKAGE]

    assert explanation.effect == "unavailable"
    assert "no claims matching" in explanation.summary
    assert "too little variation" in explanation.summary
    assert "degenerate_cross_section" not in explanation.summary


@pytest.mark.parametrize("age,expected", [(0, "increased"), (29, "increased"), (30, "decreased"), (59, "decreased")])
def test_attention_uses_exact_recent_and_prior_boundaries(age: int, expected: str) -> None:
    document = _document("annual", age)
    events = build_attention_events([], {document.id: document}, WEIGHTS, AS_OF)
    explanation = build_leg_explanations(
        _context(documents=[document]),
        _score(LegName.ATTENTION_ACCELERATION, attention_acceleration(events)),
    )[LegName.ATTENTION_ACCELERATION]

    assert expected in explanation.summary
    assert "annual reports" in explanation.summary
    assert "price" not in explanation.summary
    assert explanation.evidence[0].evidence_id == document.id


def test_attention_drops_boundary_old_and_unmatched_or_immaterial_news() -> None:
    old = _document("old", 60)
    news = _document("unreviewed-news", 0, DocumentType.NEWS)
    immaterial = _document("immaterial-news", 0, DocumentType.NEWS)
    explanation = build_leg_explanations(
        _context(
            documents=[old, news, immaterial],
            extractions=[_extraction(immaterial, materiality=Materiality.NONE)],
        ),
        _score(LegName.ATTENTION_ACCELERATION, None),
    )[LegName.ATTENTION_ACCELERATION]

    assert explanation.effect == "unavailable"
    assert "No qualifying" in explanation.summary
    assert explanation.evidence == []


def test_attention_compares_importance_not_document_counts_or_extraction_count() -> None:
    prior = _document("prior-annual", 40)
    news = [_document(f"recent-news-{i}", 2, DocumentType.NEWS) for i in range(2)]
    unmatched = [_document(f"unmatched-{i}", 1, DocumentType.NEWS) for i in range(5)]
    extractions = [_extraction(doc) for doc in news]
    extractions.append(extractions[0].model_copy(update={"id": "another-extraction"}))
    context = _context(documents=[prior, *news, *unmatched], extractions=extractions)
    events = build_attention_events(extractions, {d.id: d for d in context.documents}, WEIGHTS, AS_OF)
    explanation = build_leg_explanations(
        context, _score(LegName.ATTENTION_ACCELERATION, attention_acceleration(events)),
    )[LegName.ATTENTION_ACCELERATION]

    assert "decreased" in explanation.summary
    assert "annual reports" in explanation.summary
    assert "reviewed news reports" in explanation.summary
    assert {item.evidence_id for item in explanation.evidence} <= {prior.id, *(doc.id for doc in news)}
    assert len({item.evidence_id for item in explanation.evidence}) == len(explanation.evidence)


def test_attention_equal_activity_is_a_valid_neutral_observation() -> None:
    documents = [_document("recent", 1), _document("prior", 31)]
    explanation = build_leg_explanations(
        _context(documents=documents), _score(LegName.ATTENTION_ACCELERATION, D(0))
    )[LegName.ATTENTION_ACCELERATION]

    assert explanation.effect == "neutral"
    assert "similar" in explanation.summary
    assert {item.evidence_id for item in explanation.evidence} == {"recent", "prior"}


def test_narrative_uses_real_claim_type_growth_and_final_contribution() -> None:
    filing = _document("new-product")
    claim = NarrativeClaim(
        claim_type=NarrativeClaimType.NEW_PRODUCT, strength=ThemeStrength.STRONG,
        evidence_excerpt="We announced a new processor.",
    )
    context = _context(
        documents=[filing], extractions=[_extraction(filing, narratives=[claim])],
        fundamental_inputs=FundamentalHealthInputs(D("0.1"), None, None, None, None),
        growth_percentile=20,
    )
    explanation = build_leg_explanations(
        context, _score(LegName.NARRATIVE_PREMIUM, D("0.7"), peers=(D("0.8"), D("0.9")))
    )[LegName.NARRATIVE_PREMIUM]

    assert explanation.effect == "weighs"
    assert "Recent" in explanation.summary
    assert "new-product announcements" in explanation.summary
    assert "Sales are growing" in explanation.summary
    assert "growth comparison is weaker" in explanation.summary
    assert "story is more prominent" in explanation.summary
    assert "partnership announcements" not in explanation.summary
    assert explanation.evidence[0].excerpt == claim.evidence_excerpt


def test_narrative_claims_fade_and_future_or_old_announcements_are_not_invented() -> None:
    earlier, old, future = _document("earlier", 100), _document("old", 181), _document("future", -1)
    claim = NarrativeClaim(
        claim_type=NarrativeClaimType.PARTNERSHIP, strength=ThemeStrength.STRONG,
        evidence_excerpt="We announced a collaboration.",
    )
    extraction = _extraction(earlier, narratives=[claim])
    excluded_claim = claim.model_copy(update={"claim_type": NarrativeClaimType.NEW_PRODUCT})
    context = _context(
        documents=[earlier, old, future],
        extractions=[extraction, _extraction(old, narratives=[excluded_claim]),
                     _extraction(future, narratives=[excluded_claim])],
        growth_percentile=50,
    )
    explanation = build_leg_explanations(
        context, _score(LegName.NARRATIVE_PREMIUM, D("-0.1"))
    )[LegName.NARRATIVE_PREMIUM]

    assert "Earlier disclosures" in explanation.summary
    assert "age" in explanation.summary
    assert "partnership announcements" in explanation.summary
    assert "new-product" not in explanation.summary
    assert [item.evidence_id for item in explanation.evidence] == ["earlier"]


@pytest.mark.parametrize("reviewed", [True, False])
def test_narrative_without_claims_does_not_fabricate_company_announcements(reviewed: bool) -> None:
    filing = _document("plain")
    context = _context(
        documents=[filing], extractions=[_extraction(filing)] if reviewed else [],
        growth_percentile=80,
        fundamental_inputs=FundamentalHealthInputs(D("0.2"), None, None, None, None),
    )
    explanation = build_leg_explanations(
        context, _score(LegName.NARRATIVE_PREMIUM, D("-0.8"))
    )[LegName.NARRATIVE_PREMIUM]

    expected = "no extracted narrative claims" if reviewed else "No reviewed"
    assert expected in explanation.summary
    assert "partnership announcements" not in explanation.summary
    assert "new-product announcements" not in explanation.summary
    assert "business-growth comparison is stronger" in explanation.summary


def test_missing_growth_comparison_blocks_narrative_explanation() -> None:
    explanation = build_leg_explanations(
        _context(), _score(LegName.NARRATIVE_PREMIUM, None)
    )[LegName.NARRATIVE_PREMIUM]

    assert explanation.effect == "unavailable"
    assert "business-growth evidence is missing" in explanation.summary
    assert "percentile" not in explanation.summary


def test_discarded_claims_do_not_turn_missing_narrative_evidence_into_company_silence() -> None:
    filing = _document("discarded-story")
    extraction = _extraction(filing).model_copy(update={
        "input_fingerprint": "verified-input", "discarded_claim_count": 2,
    })
    explanation = build_leg_explanations(
        _context(documents=[filing], extractions=[extraction], growth_percentile=50),
        _score(LegName.NARRATIVE_PREMIUM, None),
    )[LegName.NARRATIVE_PREMIUM]

    assert explanation.effect == "unavailable"
    assert "discarded" in explanation.summary
    assert "does not establish that the company made no announcements" in explanation.summary
    assert "no extracted narrative claims" not in explanation.summary
    assert "enough verified narrative evidence" in explanation.summary
    assert [item.evidence_id for item in explanation.evidence] == [filing.id]


def test_qualifying_insider_sales_outweigh_purchases_and_name_actual_seller() -> None:
    seller = _document(
        "sale", kind=DocumentType.FORM_4,
        transactions=[_transaction(Form4TransactionCode.S, "100", owner="Jane Seller")],
    )
    buyer = _document(
        "purchase", kind=DocumentType.FORM_4,
        transactions=[_transaction(Form4TransactionCode.P, "20", owner="Pat Buyer")],
    )
    explanation = build_leg_explanations(
        _context(documents=[seller, buyer]), _score(LegName.SMART_MONEY, D("-0.08"))
    )[LegName.SMART_MONEY]

    assert explanation.effect == "weighs"
    assert "sales outweigh purchases" in explanation.summary
    assert "Jane Seller" in explanation.summary
    assert "does not establish the business outlook" in explanation.summary
    assert {item.evidence_id for item in explanation.evidence} == {"sale", "purchase"}


@pytest.mark.parametrize(
    "code,raw,peers,observation,relative,effect",
    [
        (Form4TransactionCode.S, D("-0.01"), (D("-0.1"), D("-0.2")),
         "sales outweigh purchases", "Despite net selling", "supports"),
        (Form4TransactionCode.P, D("0.01"), (D("0.1"), D("0.2")),
         "purchases outweigh sales", "Despite net buying", "weighs"),
    ],
)
def test_insider_absolute_balance_is_distinct_from_peer_relative_effect(
    code, raw, peers, observation, relative, effect,
) -> None:
    filing = _document("trade", kind=DocumentType.FORM_4, transactions=[_transaction(code)])
    explanation = build_leg_explanations(
        _context(documents=[filing]), _score(LegName.SMART_MONEY, raw, peers)
    )[LegName.SMART_MONEY]

    assert explanation.effect == effect
    assert observation in explanation.summary
    assert relative in explanation.summary
    assert "relative to company size" in explanation.summary


def test_insider_owner_role_adjustment_and_unsupported_owners() -> None:
    transactions = [
        _transaction(Form4TransactionCode.S, "100", officer=False, substantial_owner=True, owner="Large Holder"),
        _transaction(Form4TransactionCode.P, "60", officer=False, director=True, owner="Board Buyer"),
        _transaction(Form4TransactionCode.S, "10000", officer=False, owner="Unrelated Holder"),
    ]
    filing = _document("roles", kind=DocumentType.FORM_4, transactions=transactions)
    explanation = build_leg_explanations(
        _context(documents=[filing]), _score(LegName.SMART_MONEY, D("0.001"))
    )[LegName.SMART_MONEY]

    assert "purchases outweigh sales" in explanation.summary
    assert "Board Buyer" in explanation.summary
    assert "Unrelated Holder" not in explanation.summary


def test_officer_and_director_flags_do_not_double_count_or_discount_substantial_owners() -> None:
    filing = _document("roles", kind=DocumentType.FORM_4, transactions=[
        _transaction(Form4TransactionCode.S, "100", director=True, substantial_owner=True),
        _transaction(Form4TransactionCode.P, "100"),
    ])
    explanation = build_leg_explanations(
        _context(documents=[filing]), _score(LegName.SMART_MONEY, D(0))
    )[LegName.SMART_MONEY]

    assert explanation.effect == "neutral"
    assert "purchases and sales are balanced" in explanation.summary


def test_awards_exercises_tax_withholding_are_not_described_as_sales_or_purchases() -> None:
    filing = _document("non-market", kind=DocumentType.FORM_4, transactions=[
        _transaction(Form4TransactionCode.A, "1000"),
        _transaction(Form4TransactionCode.M, "1000"),
        _transaction(Form4TransactionCode.F, "1000"),
    ])
    explanation = build_leg_explanations(
        _context(documents=[filing]), _score(LegName.SMART_MONEY, D(0))
    )[LegName.SMART_MONEY]

    assert explanation.effect == "neutral"
    assert "share awards" in explanation.summary
    assert "option exercises" in explanation.summary
    assert "tax-withholding" in explanation.summary
    assert "do not count as open-market trades" in explanation.summary
    assert "outweigh" not in explanation.summary
    assert "missing" not in explanation.summary


def test_future_filings_future_trades_foreign_issuers_and_boundary_trades_are_excluded() -> None:
    allowed = _document(
        "allowed", kind=DocumentType.FORM_4,
        transactions=[_transaction(Form4TransactionCode.P, "10", days_ago=89, owner="Known Buyer")],
    )
    future_filing = _document(
        "future-filed", -1, DocumentType.FORM_4,
        transactions=[_transaction(Form4TransactionCode.S, "1000", owner="Future Seller")],
    )
    wrong_issuer = _document(
        "wrong-issuer", kind=DocumentType.FORM_4, security_id="other",
        transactions=[_transaction(Form4TransactionCode.S, "1000", owner="Other Seller")],
    )
    old_or_future_trades = _document("out-of-window", kind=DocumentType.FORM_4, transactions=[
        _transaction(Form4TransactionCode.S, "1000", days_ago=90, owner="Old Seller"),
        _transaction(Form4TransactionCode.S, "1000", days_ago=-1, owner="Future Seller"),
    ])
    explanation = build_leg_explanations(
        _context(documents=[future_filing, wrong_issuer, old_or_future_trades, allowed]),
        _score(LegName.SMART_MONEY, D("0.01")),
    )[LegName.SMART_MONEY]

    assert "purchases outweigh sales" in explanation.summary
    assert "Known Buyer" in explanation.summary
    assert "Seller" not in explanation.summary
    assert [item.evidence_id for item in explanation.evidence] == ["allowed"]


def test_insider_market_value_gap_does_not_pretend_a_valid_peer_comparison() -> None:
    filing = _document(
        "sale", kind=DocumentType.FORM_4, transactions=[_transaction(Form4TransactionCode.S, "10")]
    )
    context = _context(
        documents=[filing],
        valuation=ValuationBuildResult(
            ValuationMetrics(None, None, None), "USD", reason=VALUATION_REASON_NO_MARKET_CAP,
        ),
    )
    explanation = build_leg_explanations(
        context, _score(LegName.SMART_MONEY, D("-0.01"), (D("-0.1"), D("-0.2")))
    )[LegName.SMART_MONEY]

    assert explanation.effect == "unavailable"
    assert "market value is unavailable" in explanation.summary
    assert "sales outweigh purchases" in explanation.summary
    assert "stronger than peers" not in explanation.summary
    assert "supports" not in explanation.summary


@pytest.mark.parametrize("amount", ["unusable", "NaN", "-10"])
def test_unusable_insider_amounts_do_not_become_an_observed_absence_of_trades(amount: str) -> None:
    filing = _document(
        "unusable", kind=DocumentType.FORM_4, transactions=[_transaction(Form4TransactionCode.S, amount)]
    )
    explanation = build_leg_explanations(
        _context(documents=[filing]), _score(LegName.SMART_MONEY, None)
    )[LegName.SMART_MONEY]

    assert explanation.effect == "unavailable"
    assert "trades were disclosed" in explanation.summary
    assert "complete buying-versus-selling balance is unknown" in explanation.summary
    assert "No qualifying" not in explanation.summary
    assert "do not hold qualifying insider roles" not in explanation.summary


def test_fpi_insider_comparison_is_explicitly_not_applicable_even_if_filings_exist() -> None:
    filing = _document(
        "fpi-sale", kind=DocumentType.FORM_4, transactions=[_transaction(Form4TransactionCode.S, "10")]
    )
    context = _context(
        security=SECURITY.model_copy(update={"filer_profile": FilerProfile.FPI}), documents=[filing],
    )
    explanation = build_leg_explanations(
        context, _score(LegName.SMART_MONEY, D("-0.1"))
    )[LegName.SMART_MONEY]

    assert explanation.effect == "not_applicable"
    assert "Foreign private issuers" in explanation.summary
    assert "same routine insider-transaction reporting" in explanation.summary
    assert "sales outweigh" not in explanation.summary
    assert explanation.evidence == []


def test_mixed_fundamental_drivers_distinguish_absolute_growth_from_peer_strength() -> None:
    inputs = FundamentalHealthInputs(D("0.1"), D("0.01"), D("-0.05"), D("-0.2"), D("0.08"))
    signals = dict(zip(inputs.as_map(), [D(-2), D("0.5"), D(2), D("-0.5"), D(0)], strict=True))
    snapshot = _financial()
    explanation = build_leg_explanations(
        _context(fundamental_inputs=inputs, fundamental_health=_health(inputs, signals), fundamentals=[snapshot]),
        _score(LegName.FUNDAMENTAL_HEALTH, D(0), peers=(D(1), D(2))),
    )[LegName.FUNDAMENTAL_HEALTH]

    assert explanation.effect == "weighs"
    assert "Sales are growing, but this still trails peers" in explanation.summary
    assert "gross margins are widening" in explanation.summary
    assert "operations do not cover investment, but this still compares favorably with peers" in explanation.summary
    assert "debt exceeds cash" in explanation.summary
    assert "returns on invested capital are positive" in explanation.summary
    assert "clearest relative strength is cash generation" in explanation.summary
    assert "largest relative shortfall is sales growth" in explanation.summary
    assert explanation.evidence[0].source_url == (
        "https://www.sec.gov/Archives/edgar/data/123456/000012345626000001/0000123456-26-000001-index.html"
    )


def test_zero_fundamental_inputs_remain_observed_and_missing_components_remain_missing() -> None:
    inputs = FundamentalHealthInputs(D(0), None, D(0), None, D(0))
    signals = {name: D(0) if value is not None else None for name, value in inputs.as_map().items()}
    explanation = build_leg_explanations(
        _context(fundamental_inputs=inputs, fundamental_health=_health(inputs, signals)),
        _score(LegName.FUNDAMENTAL_HEALTH, D(0)),
    )[LegName.FUNDAMENTAL_HEALTH]

    assert explanation.effect == "neutral"
    assert "Sales are flat" in explanation.summary
    assert "operations just cover investment" in explanation.summary
    assert "returns on invested capital are zero" in explanation.summary
    assert "missing for the gross-margin trend and cash relative to debt" in explanation.summary


def test_unstandardisable_fundamentals_are_not_mislabeled_as_poor_business_performance() -> None:
    inputs = FundamentalHealthInputs(D("0.1"), D("0.1"), D("0.1"), D("0.1"), D("0.1"))
    signals = dict.fromkeys(inputs.as_map())
    explanation = build_leg_explanations(
        _context(fundamental_inputs=inputs, fundamental_health=_health(inputs, signals, value=None)),
        _score(LegName.FUNDAMENTAL_HEALTH, None),
    )[LegName.FUNDAMENTAL_HEALTH]

    assert explanation.effect == "unavailable"
    assert "Sales are growing" in explanation.summary
    assert "peer comparison is unavailable" in explanation.summary
    assert "too few usable business-health comparisons" in explanation.summary
    assert "relative shortfall" not in explanation.summary


def test_valuation_excludes_negative_earnings_and_cash_flow_even_if_signals_look_cheap() -> None:
    context = _context(
        valuation=ValuationBuildResult(ValuationMetrics(D(2), D(-5), D("-0.1")), "USD"),
        valuation_signals={"ev_sales": D(1), "ev_ebitda": D(100), "fcf_yield": D(100)},
        fundamentals=[_financial()],
    )
    explanation = build_leg_explanations(
        context, _score(LegName.VALUATION_BRAKE, D(1))
    )[LegName.VALUATION_BRAKE]

    assert explanation.effect == "supports"
    assert "less richly relative to sales" in explanation.summary
    assert "Operating-earnings valuation is not positive and is excluded" in explanation.summary
    assert "Cash-flow valuation is not positive and is excluded" in explanation.summary
    assert "less richly relative to operating earnings" not in explanation.summary
    assert "cash left after investment" not in explanation.summary.lower()


def test_valuation_accepts_scoring_helpers_oriented_signals_without_reorientation() -> None:
    metrics = ValuationMetrics(D(2), D(-4), D("0.08"))
    peers = {
        SECURITY.id: metrics,
        "peer-a": ValuationMetrics(D(4), D(8), D("0.04")),
        "peer-b": ValuationMetrics(D(6), D(12), D("0.02")),
    }
    signals = valuation_metric_signals(metrics, peers)
    explanation = build_leg_explanations(
        _context(
            valuation=ValuationBuildResult(metrics, "USD"),
            valuation_signals=signals,
        ),
        _score(LegName.VALUATION_BRAKE, valuation_brake(metrics, peers, SECURITY.id)),
    )[LegName.VALUATION_BRAKE]

    assert set(signals) == {"ev_sales", "ev_ebitda", "fcf_yield"}
    assert signals["ev_ebitda"] is None
    assert explanation.effect == "supports"
    assert "less richly relative to sales" in explanation.summary
    assert "market value is higher than peers" in explanation.summary
    assert "Operating-earnings valuation is not positive and is excluded" in explanation.summary
    assert "less richly relative to operating earnings" not in explanation.summary


@pytest.mark.parametrize("signal,effect,relation", [(D(1), "supports", "higher"), (D(-1), "weighs", "lower")])
def test_positive_cash_flow_yield_uses_oriented_peer_signal(signal, effect, relation) -> None:
    explanation = build_leg_explanations(
        _context(
            valuation=ValuationBuildResult(ValuationMetrics(None, None, D("0.04")), "USD"),
            valuation_signals={"fcf_yield": signal},
        ),
        _score(LegName.VALUATION_BRAKE, signal),
    )[LegName.VALUATION_BRAKE]

    assert explanation.effect == effect
    assert f"market value is {relation} than peers" in explanation.summary


def test_valuation_observations_do_not_override_actual_final_contribution() -> None:
    explanation = build_leg_explanations(
        _context(
            valuation=ValuationBuildResult(ValuationMetrics(D(10), None, None), "USD"),
            valuation_signals={"ev_sales": D(-1)},
        ),
        _score(LegName.VALUATION_BRAKE, D(-1), (D(-3), D(-2))),
    )[LegName.VALUATION_BRAKE]

    assert explanation.effect == "supports"
    assert "more richly relative to sales" in explanation.summary
    assert "Even so" in explanation.summary
    assert "less richly" not in explanation.summary


@pytest.mark.parametrize("value", [None, D(0), D(-1), D("NaN")])
def test_no_usable_positive_valuation_does_not_become_a_bargain(value: Decimal | None) -> None:
    explanation = build_leg_explanations(
        _context(
            valuation=ValuationBuildResult(ValuationMetrics(value, value, value), "USD"),
            valuation_signals={"ev_sales": D(1), "ev_ebitda": D(1), "fcf_yield": D(1)},
        ),
        _score(LegName.VALUATION_BRAKE, None),
    )[LegName.VALUATION_BRAKE]

    assert explanation.effect == "unavailable"
    assert "less richly" not in explanation.summary
    assert "higher than peers" not in explanation.summary


def test_valuation_without_peer_signals_does_not_infer_relative_cheapness_from_a_ratio() -> None:
    explanation = build_leg_explanations(
        _context(valuation=ValuationBuildResult(ValuationMetrics(D("0.1"), None, None), "USD")),
        _score(LegName.VALUATION_BRAKE, None),
    )[LegName.VALUATION_BRAKE]

    assert explanation.effect == "unavailable"
    assert "reliable peer comparison is not" in explanation.summary
    assert "less richly" not in explanation.summary


def test_valuation_market_value_gap_explains_why_a_comparison_is_unavailable() -> None:
    explanation = build_leg_explanations(
        _context(valuation=ValuationBuildResult(
            ValuationMetrics(None, None, None), "USD", reason=VALUATION_REASON_NO_MARKET_CAP,
        )),
        _score(LegName.VALUATION_BRAKE, None),
    )[LegName.VALUATION_BRAKE]

    assert explanation.effect == "unavailable"
    assert "market value is unavailable" in explanation.summary


def test_fx_absence_is_explicitly_not_applicable_without_parity_assumptions() -> None:
    explanation = build_leg_explanations(
        _context(
            valuation=ValuationBuildResult(
                ValuationMetrics(None, None, None), "EUR", fx_unavailable=True, reason=VALUATION_REASON_FX_UNAVAILABLE,
            ),
        ),
        _score(LegName.VALUATION_BRAKE, None, applicable=False),
    )[LegName.VALUATION_BRAKE]

    assert explanation.effect == "not_applicable"
    assert "EUR" in explanation.summary
    assert "authoritative historical currency conversion is unavailable" in explanation.summary
    assert "fx_rate_unavailable" not in explanation.summary


def test_financial_sources_exclude_future_foreign_and_future_only_facts() -> None:
    known = _financial()
    future = _financial("0000123456-26-000002", -1)
    other = _financial("0000123456-26-000003", security_id="other")
    future_facts = _financial("0000123456-26-000004", fact_days_ago=-1)
    empty = _financial("0000123456-26-000005").model_copy(update={"facts": []})
    inputs = FundamentalHealthInputs(D("0.1"), D("0.1"), D("0.1"), D("0.1"), D("0.1"))
    signals = dict.fromkeys(inputs.as_map(), D(1))
    context = _context(
        fundamentals=[future, other, future_facts, empty, known],
        fundamental_inputs=inputs, fundamental_health=_health(inputs, signals, D(1)),
    )
    result = build_leg_explanations(context, _score(LegName.FUNDAMENTAL_HEALTH, D(1)))

    assert [item.evidence_id for item in result[LegName.FUNDAMENTAL_HEALTH].evidence] == [known.id]
    assert all(item.knowledge_date <= AS_OF for leg in result.values() for item in leg.evidence)


@pytest.mark.parametrize(
    "cik,expected_url",
    [
        ("0000123456", "https://data.sec.gov/api/xbrl/companyfacts/CIK0000123456.json"),
        ("unverified-cik", None),
        ("0000000000", None),
    ],
)
def test_unverified_accessions_use_only_official_companyfacts_or_no_url(cik, expected_url) -> None:
    context = _context(
        security=SECURITY.model_copy(update={"cik": cik}),
        fundamentals=[_financial("unverified-accession")],
    )
    evidence = build_leg_explanations(context, _score())[LegName.FUNDAMENTAL_HEALTH].evidence

    assert evidence[0].source_url == expected_url
    assert evidence[0].label == "Reported financial data (10-Q)"
    assert evidence[0].excerpt is None


def test_evidence_is_deterministic_bounded_and_builder_does_not_mutate_inputs() -> None:
    documents = [_document(f"document-{i}", i) for i in range(6)]
    narrative = NarrativeClaim(
        claim_type=NarrativeClaimType.DESIGN_WIN, strength=ThemeStrength.MODERATE,
        evidence_excerpt="Our component was selected for the platform.",
    )
    extractions = [
        _extraction(doc, themes=[_theme()], narratives=[narrative]) for doc in documents
    ]
    financials = [_financial(f"0000123456-26-{i:06d}", i) for i in range(6)]
    context = _context(documents=documents, extractions=extractions, fundamentals=financials, growth_percentile=50)
    score = _score(raw=D("0.5"))
    before_context, before_score = deepcopy(context), deepcopy(score)

    result = build_leg_explanations(context, score)
    reordered = build_leg_explanations(
        replace(context, documents=documents[::-1], extractions=extractions[::-1], fundamentals=financials[::-1]),
        score,
    )

    assert result == reordered
    assert context == before_context
    assert score == before_score
    allowed_ids = {doc.id for doc in documents} | {snapshot.id for snapshot in financials}
    for explanation in result.values():
        assert len(explanation.evidence) <= 3
        assert len({item.evidence_id for item in explanation.evidence}) == len(explanation.evidence)
        assert all(item.evidence_id in allowed_ids and item.knowledge_date <= AS_OF for item in explanation.evidence)
        assert not any(term in explanation.summary for term in ("z-score", "percentile", "raw_value_missing", "weight"))


def test_missing_document_url_is_not_replaced_by_an_invented_sec_document_path() -> None:
    document = _document("no-url").model_copy(update={"url": None, "accession_number": "unverified-accession"})
    explanation = build_leg_explanations(
        _context(documents=[document], extractions=[_extraction(document, themes=[_theme()])]),
        _score(raw=D("0.5")),
    )[LegName.THESIS_LINKAGE]

    assert explanation.evidence[0].source_url is None


def test_score_for_a_different_security_is_rejected() -> None:
    with pytest.raises(ValueError, match="same security"):
        build_leg_explanations(_context(), replace(_score(), security_id="other"))
