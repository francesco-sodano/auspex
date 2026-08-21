"""Unit tests for `GET /api/securities*` (arc42 §11).

Response shapes are asserted against `web/src/lib/types.ts`'s
`SecuritySummary`/`SecurityPackage` contract — key names, nesting, and the
values the SPA actually reads (`Discussion.tsx`).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from auspex.api.auth import AuthenticatedUser, get_current_user
from auspex.api.deps import (
    get_fundamental_repo,
    get_price_sink,
    get_recommendation_repo,
    get_score_repo,
    get_universe,
)
from auspex.api.repos import get_digest_repo, get_document_repo
from auspex.api.routes import securities
from auspex.models.document import Document, InsiderTransaction
from auspex.models.enums import (
    CohortConfidence,
    Direction,
    DocumentType,
    FilerProfile,
    Form4TransactionCode,
    LegName,
)
from auspex.models.extraction import ChannelBDigest
from auspex.models.policy import GateResult, Recommendation
from auspex.models.scoring import LegResult, ScoreSnapshot
from auspex.models.security import Security
from tests.unit.conftest import FakeCosmosRepository, FakeUniverse, make_router_app


class FakePriceSink:
    async def history_as_of(self, security_id, as_of, days=15):
        return []

SEC_A = Security(
    id="sec-a", ticker="AAA", cik="0000000001", name="Alpha Corp", cohort="tech", filer_profile=FilerProfile.DOMESTIC
)
SEC_B = Security(
    id="sec-b", ticker="BBB", cik="0000000002", name="Beta Corp", cohort="tech", filer_profile=FilerProfile.DOMESTIC
)


def _score(security_id: str, as_of: date, composite: str = "1.5", percentile: int = 80) -> ScoreSnapshot:
    return ScoreSnapshot(
        id=f"{security_id}:{as_of.isoformat()}",
        security_id=security_id,
        as_of_date=as_of,
        config_version_id="cfg-1",
        cohort_used="tech",
        cohort_confidence=CohortConfidence.HIGH,
        filer_profile=FilerProfile.DOMESTIC,
        coverage="1.0",
        legs={
            LegName.THESIS_LINKAGE: LegResult(raw="0.4", z="0.9", weight="0.5", contribution="0.45", computable=True)
        },
        composite=composite,
        percentile=percentile,
        direction=Direction.STRENGTHENING,
        package_fingerprint="fp-1",
        narrative="Strong quarter driven by cloud demand.",
        max_knowledge_date=as_of,
    )


def _recommendation(security_id: str, as_of: date, user_id: str = "owner-1") -> Recommendation:
    return Recommendation(
        id=f"{user_id}:{security_id}:{as_of.isoformat()}",
        user_id=user_id,
        security_id=security_id,
        as_of_date=as_of,
        action="BUY",
        target_weight_pct="0.05",
        gate_trace=[GateResult(gate="coverage_floor", passed=True, actual_value="1.0", threshold_value="0.8")],
        config_version_id="cfg-1",
    )


def _document(doc_id: str = "doc-1", security_id: str = "sec-a") -> Document:
    return Document(
        id=doc_id,
        security_id=security_id,
        source="edgar",
        source_record_id="acc-1",
        document_type=DocumentType.FORM_10K,
        form_type="10-K",
        content_hash="sha256:abc",
        url="https://example.com/doc-1",
        filed_date=date(2026, 8, 1),
        retrieved_at=datetime.now(UTC),
        knowledge_date=date(2026, 8, 1),
    )


def _default_overrides(universe=None):
    return {
        get_current_user: lambda: AuthenticatedUser(user_id="owner-1", claims={}),
        get_universe: lambda: universe or FakeUniverse(securities=[SEC_A, SEC_B]),
        get_score_repo: lambda: FakeCosmosRepository(),
        get_recommendation_repo: lambda: FakeCosmosRepository(),
        get_document_repo: lambda: FakeCosmosRepository(),
        get_digest_repo: lambda: FakeCosmosRepository(),
        get_fundamental_repo: lambda: FakeCosmosRepository(),
        get_price_sink: lambda: FakePriceSink(),
    }


def _make_client(overrides: dict | None = None):
    merged = _default_overrides()
    merged.update(overrides or {})
    return make_router_app(securities.router, merged)


class TestRequiresAuth:
    def test_list_requires_auth(self):
        overrides = _default_overrides()
        del overrides[get_current_user]
        client = make_router_app(securities.router, overrides)
        assert client.get("/api/securities").status_code == 401

    def test_get_requires_auth(self):
        overrides = _default_overrides()
        del overrides[get_current_user]
        client = make_router_app(securities.router, overrides)
        assert client.get("/api/securities/sec-a").status_code == 401


class TestListSecuritiesContract:
    def test_matches_the_securitysummary_contract_keys(self):
        score_a = _score("sec-a", date(2026, 8, 8), composite="2.0", percentile=95)
        recommendation = _recommendation("sec-a", date(2026, 8, 8))
        client = _make_client(
            {
                get_score_repo: lambda: FakeCosmosRepository([score_a]),
                get_recommendation_repo: lambda: FakeCosmosRepository([recommendation]),
            }
        )

        response = client.get("/api/securities")

        assert response.status_code == 200
        body = {row["security_id"]: row for row in response.json()}
        assert set(body["sec-a"]) == {
            "security_id",
            "ticker",
            "name",
            "market",
            "cohort",
            "score",
            "percentile",
            "direction",
            "coverage",
            "action",
        }
        assert body["sec-a"]["score"] == "2.0"
        assert body["sec-a"]["percentile"] == 95
        assert body["sec-a"]["coverage"] == "1.0"
        assert body["sec-a"]["action"] == "BUY"

    def test_unscored_security_reports_null_score_percentile_and_action(self):
        client = _make_client()

        response = client.get("/api/securities")

        body = {row["security_id"]: row for row in response.json()}
        assert body["sec-b"]["score"] is None
        assert body["sec-b"]["percentile"] is None
        assert body["sec-b"]["action"] is None


class TestGetSecurity:
    def test_404_for_unknown_security(self):
        client = _make_client()
        assert client.get("/api/securities/unknown-id").status_code == 404

    def test_404_when_no_score_exists(self):
        client = _make_client()
        assert client.get("/api/securities/sec-a").status_code == 404

    def test_matches_the_securitypackage_contract(self):
        score_a = _score("sec-a", date(2026, 8, 8))
        recommendation = _recommendation("sec-a", date(2026, 8, 8))
        document = _document()
        digest = ChannelBDigest(
            id="digest-1",
            security_id="sec-a",
            document_id="doc-1",
            content_hash="sha256:abc",
            model_version="test",
            headline="Q2 beat",
            digest="Strong quarter.",
        )
        client = _make_client(
            {
                get_score_repo: lambda: FakeCosmosRepository([score_a]),
                get_recommendation_repo: lambda: FakeCosmosRepository([recommendation]),
                get_document_repo: lambda: FakeCosmosRepository([document]),
                get_digest_repo: lambda: FakeCosmosRepository([digest]),
            }
        )

        response = client.get("/api/securities/sec-a")

        assert response.status_code == 200
        body = response.json()
        assert set(body) == {
            "security",
            "as_of_date",
            "narrative",
            "legs",
            "recommendation",
            "market",
            "business_summary",
            "current_price_usd",
            "price_change_pct",
            "price_history",
            "fundamentals",
            "score_change",
            "score_reasoning",
            "news",
            "history",
            "documents",
        }

        assert set(body["security"]) == {
            "security_id",
            "ticker",
            "name",
            "market",
            "cohort",
            "score",
            "percentile",
            "direction",
            "coverage",
            "action",
            "filer_profile",
        }
        assert body["security"]["filer_profile"] == "DOMESTIC"
        assert body["security"]["action"] == "BUY"
        assert body["narrative"] == "Strong quarter driven by cloud demand."
        assert body["business_summary"].startswith("Alpha Corp is tracked by Auspex")
        assert "Strong quarter driven by cloud demand." in body["business_summary"]

        leg = body["legs"]["thesis_linkage"]
        assert set(leg) == {
            "raw",
            "z",
            "weight",
            "contribution",
            "computable",
            "score",
            "neutral",
            "status_explanation",
        }
        assert leg["z"] == "0.9"

        recommendation_out = body["recommendation"]
        assert set(recommendation_out) == {
            "id",
            "security_id",
            "ticker",
            "company_name",
            "action",
            "rationale",
            "target_weight",
            "current_weight",
            "suggested_trade_chf",
            "suggested_quantity",
            "allocation_mode",
            "allocation_trace",
            "estimated_cost_chf",
            "auspex_score",
            "buy_ready",
            "blocking_reasons",
            "gate_trace",
            "as_of_date",
            "disposition",
            "followed",
            "outcome_matures_on",
            "outcome_mature",
        }
        assert recommendation_out["ticker"] == "AAA"
        assert recommendation_out["target_weight"] == "0.05"
        gate = recommendation_out["gate_trace"][0]
        assert set(gate) == {"gate", "passed", "actual", "threshold", "reason"}
        assert gate["actual"] == "1.0"
        assert gate["threshold"] == "0.8"

        document_out = body["documents"][0]
        assert set(document_out) == {
            "document_id",
            "form",
            "filed_at",
            "headline",
            "digest",
            "source_url",
            "publisher",
            "retrieved_at",
            "relevance_reason",
            "stale",
        }
        assert document_out["headline"] == "Q2 beat"
        assert document_out["source_url"] == "https://example.com/doc-1"

    def test_history_only_includes_points_with_a_computed_composite_and_percentile(self):
        scored = _score("sec-a", date(2026, 8, 8), composite="1.5", percentile=80)
        unscored = ScoreSnapshot(
            id="sec-a:2026-08-01",
            security_id="sec-a",
            as_of_date=date(2026, 8, 1),
            config_version_id="cfg-1",
            cohort_used="tech",
            cohort_confidence=CohortConfidence.LOW,
            filer_profile=FilerProfile.DOMESTIC,
            coverage="0.0",
            legs={},
            composite=None,
            percentile=None,
            package_fingerprint="fp-0",
            max_knowledge_date=date(2026, 8, 1),
        )
        client = _make_client(
            {get_score_repo: lambda: FakeCosmosRepository([scored, unscored])},
        )

        response = client.get("/api/securities/sec-a")

        history = response.json()["history"]
        assert history == [{"as_of_date": "2026-08-08", "composite": "1.5", "percentile": 80}]

    def test_form4_digest_is_never_used_as_the_company_recap(self):
        score = _score("sec-a", date(2026, 8, 8))
        form4 = Document(
            id="form4-1",
            security_id="sec-a",
            source="edgar",
            source_record_id="form4-1",
            document_type=DocumentType.FORM_4,
            form_type="4",
            content_hash="sha256:form4",
            filed_date=date(2026, 8, 7),
            retrieved_at=datetime.now(UTC),
            knowledge_date=date(2026, 8, 7),
        )
        digest = ChannelBDigest(
            id="digest-form4",
            security_id="sec-a",
            document_id="form4-1",
            content_hash="sha256:form4",
            model_version="test",
            headline="Insider sale",
            digest="Open-market sale of 10,000 shares by a company officer.",
        )
        client = _make_client(
            {
                get_score_repo: lambda: FakeCosmosRepository([score]),
                get_document_repo: lambda: FakeCosmosRepository([form4]),
                get_digest_repo: lambda: FakeCosmosRepository([digest]),
            }
        )

        response = client.get("/api/securities/sec-a")

        assert response.status_code == 200
        recap = response.json()["business_summary"]
        assert recap.startswith("Alpha Corp is tracked by Auspex")
        assert "open-market sale" not in recap.lower()

    def test_no_recommendation_yields_a_null_recommendation_and_null_action(self):
        score_a = _score("sec-a", date(2026, 8, 8))
        client = _make_client({get_score_repo: lambda: FakeCosmosRepository([score_a])})

        response = client.get("/api/securities/sec-a")

        body = response.json()
        assert body["recommendation"] is None
        assert body["security"]["action"] is None

    def test_recommendation_lookup_is_scoped_to_the_authenticated_user(self):
        score_a = _score("sec-a", date(2026, 8, 8))
        recommendation_repo = FakeCosmosRepository()
        client = _make_client(
            {
                get_score_repo: lambda: FakeCosmosRepository([score_a]),
                get_recommendation_repo: lambda: recommendation_repo,
            }
        )

        client.get("/api/securities/sec-a")

        recorded = recommendation_repo.queries[0]
        assert recorded.partition_key == "owner-1"
        assert {"name": "@user_id", "value": "owner-1"} in recorded.parameters
        assert {"name": "@security_id", "value": "sec-a"} in recorded.parameters


class TestSecurityHistoryEndpoint:
    def test_returns_scores_within_the_date_range_in_ascending_order(self):
        scores = [
            _score("sec-a", date(2026, 8, 5), composite="1.0"),
            _score("sec-a", date(2026, 8, 8), composite="1.5"),
            _score("sec-a", date(2026, 7, 1), composite="0.5"),  # out of range
        ]
        client = _make_client({get_score_repo: lambda: FakeCosmosRepository(scores)})

        response = client.get("/api/securities/sec-a/history", params={"from": "2026-08-01", "to": "2026-08-31"})

        assert response.status_code == 200
        body = response.json()
        assert [row["as_of_date"] for row in body] == ["2026-08-05", "2026-08-08"]


class TestSecurityDocumentsEndpoint:
    def test_matches_the_securitydocumentout_contract(self):
        document = _document()
        digest = ChannelBDigest(
            id="digest-1",
            security_id="sec-a",
            document_id="doc-1",
            content_hash="sha256:abc",
            model_version="test",
            headline="Q2 beat",
            digest="digest text",
        )
        client = _make_client(
            {
                get_document_repo: lambda: FakeCosmosRepository([document]),
                get_digest_repo: lambda: FakeCosmosRepository([digest]),
            }
        )

        response = client.get("/api/securities/sec-a/documents")

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert set(body[0]) == {
            "document_id",
            "form",
            "filed_at",
            "headline",
            "digest",
            "source_url",
            "publisher",
            "retrieved_at",
            "relevance_reason",
            "stale",
        }
        assert body[0]["headline"] == "Q2 beat"

    def test_document_without_a_digest_reports_empty_headline_and_digest(self):
        document = _document()
        client = _make_client({get_document_repo: lambda: FakeCosmosRepository([document])})

        response = client.get("/api/securities/sec-a/documents")

        body = response.json()[0]
        assert body["headline"] == ""
        assert body["digest"] == ""


def test_smart_money_zero_variance_is_explained_as_neutral() -> None:
    score = _score("sec-a", date(2026, 8, 8))
    score.legs[LegName.SMART_MONEY] = LegResult(
        raw="0",
        z=None,
        weight="0.2",
        contribution=None,
        computable=False,
    )

    details = securities._leg_scores(score, [score])

    assert details["smart_money"].score is None
    assert details["smart_money"].neutral is True
    assert details["smart_money"].status_explanation is not None
    assert "Neutral, not missing" in details["smart_money"].status_explanation


def test_form4_without_url_gets_meaningful_text_and_sec_link() -> None:
    document = _document().model_copy(
        update={
            "form_type": "4",
            "accession_number": "0000000001-26-000123",
            "url": None,
            "title": None,
            "insider_transactions": [
                InsiderTransaction(
                    owner_name="Jane Executive",
                    is_officer=True,
                    transaction_code=Form4TransactionCode.S,
                    transaction_date=date(2026, 8, 8),
                    shares="1250",
                    price_per_share="100",
                )
            ],
        }
    )

    mapped = securities._map_document(document, None, SEC_A)

    assert mapped.headline == "Form 4 — Jane Executive"
    assert "open-market sale of 1,250 shares" in mapped.digest
    assert mapped.source_url.endswith(
        "/1/000000000126000123/0000000001-26-000123-index.html"
    )


def test_news_relevance_requires_ticker_or_company_name_in_title() -> None:
    generic = _document().model_copy(
        update={
            "document_type": DocumentType.NEWS,
            "title": "AI Spending Accelerates",
        }
    )
    relevant = generic.model_copy(
        update={"title": "AAA Expands Its AI Product Portfolio"}
    )

    assert securities._news_is_relevant(generic, SEC_A) is False
    assert securities._news_is_relevant(relevant, SEC_A) is True


def test_analysis_exposes_nine_fundamental_slots() -> None:
    metrics = securities._fundamentals([], date(2026, 8, 8), Decimal("100"))

    assert len(metrics) == 9
    assert [metric.label for metric in metrics[-3:]] == [
        "P / E (latest FY)",
        "EV / Sales",
        "FCF yield",
    ]
