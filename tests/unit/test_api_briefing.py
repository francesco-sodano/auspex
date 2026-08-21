"""Unit tests for `GET /api/briefing` (arc42 §11, §12 Home page).

Response shape is asserted against `web/src/lib/types.ts`'s `Briefing`
contract — key names, nesting, and the values `Home.tsx` actually renders.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from auspex.api.auth import AuthenticatedUser, get_current_user
from auspex.api.deps import (
    get_portfolio_projection_repo,
    get_recommendation_repo,
    get_run_repo,
    get_score_repo,
    get_universe,
)
from auspex.api.repos import get_digest_repo, get_document_repo, get_extraction_repo, get_leg_change_repo
from auspex.api.routes import briefing
from auspex.models.document import Document
from auspex.models.enums import (
    CohortConfidence,
    DocumentType,
    ExtractionConfidence,
    FilerProfile,
    LegName,
    Materiality,
    RiskCategory,
    RiskSeverity,
    RunStatus,
    Sentiment,
)
from auspex.models.extraction import ChannelAExtraction, ChannelBDigest, ComparativeDiff, RiskFactorAdded, ThemeClaim
from auspex.models.policy import GateResult, Recommendation
from auspex.models.portfolio import PortfolioProjection, PositionProjectionRow
from auspex.models.run import RunManifest
from auspex.models.scoring import LegChange, LegResult, ScoreSnapshot
from auspex.models.security import Security
from tests.unit.conftest import FakeCosmosRepository, FakeUniverse, make_router_app

SEC_A = Security(
    id="sec-a", ticker="AAA", cik="0000000001", name="Alpha Corp", cohort="tech", filer_profile=FilerProfile.DOMESTIC
)
SEC_B = Security(
    id="sec-b", ticker="BBB", cik="0000000002", name="Beta Corp", cohort="tech", filer_profile=FilerProfile.DOMESTIC
)
AS_OF = date(2026, 8, 8)


def _leg_change(security_id: str, leg: LegName, delta_z: str) -> LegChange:
    return LegChange(
        id=f"{security_id}:{AS_OF.isoformat()}:{leg.value}",
        security_id=security_id,
        as_of_date=AS_OF,
        leg=leg,
        delta_z=delta_z,
    )


def _score(
    security_id: str,
    weight: str = "0.5",
    narrative: str | None = "Cloud demand accelerated.",
    *,
    as_of: date = AS_OF,
    percentile: int = 70,
) -> ScoreSnapshot:
    return ScoreSnapshot(
        id=f"{security_id}:{as_of.isoformat()}",
        security_id=security_id,
        as_of_date=as_of,
        config_version_id="cfg-1",
        cohort_used="tech",
        cohort_confidence=CohortConfidence.HIGH,
        filer_profile=FilerProfile.DOMESTIC,
        coverage="1.0",
        legs={LegName.SMART_MONEY: LegResult(weight=weight, computable=True)},
        composite="1.2",
        percentile=percentile,
        narrative=narrative,
        package_fingerprint="fp-1",
        max_knowledge_date=as_of,
    )


def _recommendation(security_id: str, user_id: str = "owner-1") -> Recommendation:
    return Recommendation(
        id=f"{user_id}:{security_id}:{AS_OF.isoformat()}",
        user_id=user_id,
        security_id=security_id,
        as_of_date=AS_OF,
        action="BUY",
        target_weight_pct="0.05",
        gate_trace=[GateResult(gate="coverage_floor", passed=True, actual_value="1.0", threshold_value="0.8")],
        config_version_id="cfg-1",
    )


def _run(status: RunStatus, degraded_reasons: list[str] | None = None, run_type: str = "nightly") -> RunManifest:
    return RunManifest(
        id=f"{AS_OF.isoformat()}:{run_type}",
        run_date=AS_OF,
        run_type=run_type,
        status=status,
        started_at=datetime.now(UTC),
        degraded_reasons=degraded_reasons or [],
    )


def _default_overrides(universe=None):
    return {
        get_current_user: lambda: AuthenticatedUser(user_id="owner-1", claims={}),
        get_universe: lambda: universe or FakeUniverse(securities=[SEC_A, SEC_B]),
        get_recommendation_repo: lambda: FakeCosmosRepository(),
        get_leg_change_repo: lambda: FakeCosmosRepository(),
        get_document_repo: lambda: FakeCosmosRepository(),
        get_digest_repo: lambda: FakeCosmosRepository(),
        get_extraction_repo: lambda: FakeCosmosRepository(),
        get_score_repo: lambda: FakeCosmosRepository(),
        get_run_repo: lambda: FakeCosmosRepository(),
        get_portfolio_projection_repo: lambda: FakeCosmosRepository(),
    }


def _make_client(overrides: dict | None = None):
    merged = _default_overrides()
    merged.update(overrides or {})
    return make_router_app(briefing.router, merged)


class TestRequiresAuth:
    def test_requires_auth(self):
        overrides = _default_overrides()
        del overrides[get_current_user]
        client = make_router_app(briefing.router, overrides)
        assert client.get("/api/briefing").status_code == 401


class TestContractShape:
    def test_top_level_keys_match_the_briefing_type(self):
        client = _make_client()

        response = client.get("/api/briefing", params={"date": "2026-08-08"})

        assert response.status_code == 200
        body = response.json()
        assert set(body) == {
            "date",
            "run_status",
            "max_knowledge_date",
            "portfolio",
            "changes",
            "movers_up",
            "movers_down",
            "escalated_risks",
            "recommendations",
            "assertion_failures",
        }
        assert body["date"] == "2026-08-08"

    def test_defaults_when_nothing_has_been_computed_yet(self):
        client = _make_client()

        response = client.get("/api/briefing", params={"date": "2026-08-08"})

        body = response.json()
        assert body["run_status"] == "RUNNING"
        assert body["portfolio"] is None
        assert body["changes"] == []
        assert body["escalated_risks"] == []
        assert body["recommendations"] == []
        assert body["assertion_failures"] == []


class TestRunStatus:
    def test_reports_the_nightly_runs_status_and_degraded_reasons(self):
        run = _run(RunStatus.DEGRADED, degraded_reasons=["ASSERT: assertion X failed"])
        client = _make_client({get_run_repo: lambda: FakeCosmosRepository([run])})

        response = client.get("/api/briefing", params={"date": "2026-08-08"})

        body = response.json()
        assert body["run_status"] == "DEGRADED"
        assert body["assertion_failures"] == ["ASSERT: assertion X failed"]

    def test_maps_timeout_onto_the_failed_frontend_literal(self):
        run = _run(RunStatus.TIMEOUT)
        client = _make_client({get_run_repo: lambda: FakeCosmosRepository([run])})

        response = client.get("/api/briefing", params={"date": "2026-08-08"})

        assert response.json()["run_status"] == "FAILED"

    def test_provider_refresh_warning_does_not_render_as_application_failure(self):
        run = _run(
            RunStatus.DEGRADED,
            degraded_reasons=["COLLECT_NEWS: provider_failures=2; cached data retained"],
        )
        client = _make_client({get_run_repo: lambda: FakeCosmosRepository([run])})

        body = client.get("/api/briefing", params={"date": "2026-08-08"}).json()

        assert body["run_status"] == "SUCCESS"
        assert body["assertion_failures"] == []


class TestMaxKnowledgeDate:
    def test_is_the_max_across_every_scored_security_that_date(self):
        scores = [
            ScoreSnapshot(
                id="sec-a:2026-08-08",
                security_id="sec-a",
                as_of_date=AS_OF,
                config_version_id="cfg-1",
                cohort_used="tech",
                cohort_confidence=CohortConfidence.HIGH,
                filer_profile=FilerProfile.DOMESTIC,
                coverage="1.0",
                legs={},
                package_fingerprint="fp-1",
                max_knowledge_date=date(2026, 8, 6),
            ),
            ScoreSnapshot(
                id="sec-b:2026-08-08",
                security_id="sec-b",
                as_of_date=AS_OF,
                config_version_id="cfg-1",
                cohort_used="tech",
                cohort_confidence=CohortConfidence.HIGH,
                filer_profile=FilerProfile.DOMESTIC,
                coverage="1.0",
                legs={},
                package_fingerprint="fp-1",
                max_knowledge_date=date(2026, 8, 7),
            ),
        ]
        client = _make_client({get_score_repo: lambda: FakeCosmosRepository(scores)})

        response = client.get("/api/briefing", params={"date": "2026-08-08"})

        assert response.json()["max_knowledge_date"] == "2026-08-07"


class TestPortfolioSummary:
    def test_is_null_when_no_projection_exists_for_the_date(self):
        client = _make_client()
        response = client.get("/api/briefing", params={"date": "2026-08-08"})
        assert response.json()["portfolio"] is None

    def test_computes_day_change_and_summed_unrealised(self):
        today = PortfolioProjection(
            id="owner-1:2026-08-08",
            user_id="owner-1",
            as_of_date=AS_OF,
            lot_level=True,
            total_value_chf="105000",
            cash_chf="5000",
            positions=[
                PositionProjectionRow(
                    ticker="AAA", quantity="10", unrealised_chf="1000", source_ledger_read_at=datetime.now(UTC)
                ),
                PositionProjectionRow(
                    ticker="BBB", quantity="5", unrealised_chf="500", source_ledger_read_at=datetime.now(UTC)
                ),
            ],
        )
        yesterday = PortfolioProjection(
            id="owner-1:2026-08-07",
            user_id="owner-1",
            as_of_date=date(2026, 8, 7),
            lot_level=True,
            total_value_chf="100000",
            cash_chf="5000",
        )
        client = _make_client(
            {get_portfolio_projection_repo: lambda: FakeCosmosRepository([today, yesterday])},
        )

        response = client.get("/api/briefing", params={"date": "2026-08-08"})

        portfolio = response.json()["portfolio"]
        assert set(portfolio) == {
            "value_chf",
            "invested_chf",
            "cash_chf",
            "total_gain_chf",
            "day_change_chf",
            "unrealised_chf",
            "expenses_chf",
            "dividends_chf",
        }
        assert portfolio["value_chf"] == "105000"
        assert portfolio["day_change_chf"] == "5000"
        assert portfolio["unrealised_chf"] == "1500"
        assert portfolio["cash_chf"] == "5000"


class TestScoreMovers:
    def test_returns_top_movers_on_zero_to_one_hundred_scale(self):
        current = [
            _score("sec-a", percentile=85),
            _score("sec-b", percentile=25),
        ]
        prior = [
            _score("sec-a", as_of=date(2026, 8, 7), percentile=60),
            _score("sec-b", as_of=date(2026, 8, 7), percentile=55),
        ]
        client = _make_client(
            {get_score_repo: lambda: FakeCosmosRepository(current + prior)}
        )

        body = client.get("/api/briefing", params={"date": "2026-08-08"}).json()

        assert body["movers_up"][0]["ticker"] == "AAA"
        assert body["movers_up"][0]["score"] == 85
        assert body["movers_up"][0]["score_change"] == 25
        assert body["movers_down"][0]["ticker"] == "BBB"
        assert body["movers_down"][0]["score_change"] == -30

    def test_day_change_defaults_to_zero_with_no_prior_day(self):
        today = PortfolioProjection(
            id="owner-1:2026-08-08", user_id="owner-1", as_of_date=AS_OF, lot_level=True,
            total_value_chf="105000", cash_chf="5000",
        )
        client = _make_client({get_portfolio_projection_repo: lambda: FakeCosmosRepository([today])})

        response = client.get("/api/briefing", params={"date": "2026-08-08"})

        assert response.json()["portfolio"]["day_change_chf"] == "0"


class TestChanges:
    def test_matches_the_change_contract_and_ranks_by_absolute_contribution_delta(self):
        leg_changes = [
            _leg_change("sec-a", LegName.SMART_MONEY, "0.2"),
            _leg_change("sec-b", LegName.SMART_MONEY, "-2.0"),
        ]
        score_a = _score("sec-a", weight="0.5")
        score_b = _score("sec-b", weight="0.5")
        client = _make_client(
            {
                get_leg_change_repo: lambda: FakeCosmosRepository(leg_changes),
                get_score_repo: lambda: FakeCosmosRepository([score_a, score_b]),
            }
        )

        response = client.get("/api/briefing", params={"date": "2026-08-08"})

        changes = response.json()["changes"]
        assert [c["security_id"] for c in changes] == ["sec-b", "sec-a"]
        first = changes[0]
        assert set(first) == {
            "security_id",
            "ticker",
            "company_name",
            "leg",
            "contribution_delta",
            "narrative",
            "evidence_excerpt",
        }
        assert first["ticker"] == "BBB"
        assert first["contribution_delta"] == "-1.00"  # weight 0.5 * delta_z -2.0
        assert first["narrative"] == "Cloud demand accelerated."

    def test_evidence_excerpt_is_sourced_from_the_most_recent_extraction(self):
        leg_changes = [_leg_change("sec-a", LegName.SMART_MONEY, "0.5")]
        document = Document(
            id="doc-1",
            security_id="sec-a",
            source="edgar",
            source_record_id="acc-1",
            document_type=DocumentType.FORM_10K,
            content_hash="sha256:abc",
            retrieved_at=datetime.now(UTC),
            knowledge_date=date(2026, 8, 8),
        )
        extraction = ChannelAExtraction(
            id="extraction-1",
            security_id="sec-a",
            document_id="doc-1",
            content_hash="sha256:abc",
            model_version="test",
            taxonomy_version="v1",
            materiality=Materiality.HIGH,
            sentiment=Sentiment.POSITIVE,
            guidance_direction="RAISED",
            novelty="NEW_INFORMATION",
            theme_claims=[
                ThemeClaim(theme_id="ai-datacenter", strength="STRONG", evidence_excerpt="Demand remains strong.")
            ],
            extraction_confidence=ExtractionConfidence.HIGH,
        )
        client = _make_client(
            {
                get_leg_change_repo: lambda: FakeCosmosRepository(leg_changes),
                get_document_repo: lambda: FakeCosmosRepository([document]),
                get_extraction_repo: lambda: FakeCosmosRepository([extraction]),
            }
        )

        response = client.get("/api/briefing", params={"date": "2026-08-08"})

        assert response.json()["changes"][0]["evidence_excerpt"] == "Demand remains strong."

    def test_truncates_to_ten_movements(self):
        leg_changes = [_leg_change(f"sec-{i}", LegName.SMART_MONEY, str(i)) for i in range(1, 15)]
        client = _make_client({get_leg_change_repo: lambda: FakeCosmosRepository(leg_changes)})

        response = client.get("/api/briefing", params={"date": "2026-08-08"})

        assert len(response.json()["changes"]) == 10


class TestEscalatedRisks:
    def test_matches_the_contract_and_only_includes_high_severity(self):
        document = Document(
            id="doc-1",
            security_id="sec-a",
            source="edgar",
            source_record_id="acc-1",
            document_type=DocumentType.FORM_10K,
            content_hash="sha256:abc",
            retrieved_at=datetime.now(UTC),
            knowledge_date=date(2026, 8, 8),
        )
        digest = ChannelBDigest(
            id="digest-1",
            security_id="sec-a",
            document_id="doc-1",
            content_hash="sha256:abc",
            model_version="test",
            headline="h",
            digest="d",
            comparative=ComparativeDiff(
                risk_factors_added=[
                    RiskFactorAdded(
                        summary="supply risk", verbatim="v", category=RiskCategory.SUPPLY, severity=RiskSeverity.HIGH
                    ),
                    RiskFactorAdded(
                        summary="minor risk", verbatim="v2", category=RiskCategory.OTHER, severity=RiskSeverity.LOW
                    ),
                ]
            ),
        )
        client = _make_client(
            {
                get_document_repo: lambda: FakeCosmosRepository([document]),
                get_digest_repo: lambda: FakeCosmosRepository([digest]),
            }
        )

        response = client.get("/api/briefing", params={"date": "2026-08-08"})

        escalated = response.json()["escalated_risks"]
        assert len(escalated) == 1
        assert set(escalated[0]) == {"security_id", "ticker", "category", "summary", "severity"}
        assert escalated[0]["ticker"] == "AAA"
        assert escalated[0]["summary"] == "supply risk"


class TestRecommendations:
    def test_suppressed_recommendation_is_not_returned(self):
        recommendation = _recommendation("sec-a").model_copy(
            update={
                "decision_signature": "sig-v1:test",
                "suppressed": True,
                "suppression_reason": "REJECTED",
            }
        )
        client = _make_client(
            {
                get_recommendation_repo: lambda: FakeCosmosRepository([recommendation]),
                get_score_repo: lambda: FakeCosmosRepository([_score("sec-a")]),
            }
        )

        response = client.get("/api/briefing", params={"date": "2026-08-08"})

        assert response.status_code == 200
        assert response.json()["recommendations"] == []

    def test_matches_the_recommendation_contract_and_is_scoped_to_the_authenticated_user(self):
        recommendation = _recommendation("sec-a")
        recommendation_repo = FakeCosmosRepository([recommendation])
        score_a = _score("sec-a")
        client = _make_client(
            {
                get_recommendation_repo: lambda: recommendation_repo,
                get_score_repo: lambda: FakeCosmosRepository([score_a]),
            }
        )

        response = client.get("/api/briefing", params={"date": "2026-08-08"})

        assert response.status_code == 200
        recommendations = response.json()["recommendations"]
        assert len(recommendations) == 1
        rec = recommendations[0]
        assert set(rec) == {
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
        assert rec["ticker"] == "AAA"
        assert rec["rationale"] == "Cloud demand accelerated."
        gate = rec["gate_trace"][0]
        assert set(gate) == {"gate", "passed", "actual", "threshold", "reason"}

        recorded = recommendation_repo.queries[0]
        assert recorded.partition_key == "owner-1"
        assert {"name": "@user_id", "value": "owner-1"} in recorded.parameters

    def test_falls_back_to_a_gate_cascade_summary_with_no_narrative(self):
        recommendation = _recommendation("sec-a")
        score_without_narrative = _score("sec-a", narrative=None)
        client = _make_client(
            {
                get_recommendation_repo: lambda: FakeCosmosRepository([recommendation]),
                get_score_repo: lambda: FakeCosmosRepository([score_without_narrative]),
            }
        )

        response = client.get("/api/briefing", params={"date": "2026-08-08"})

        assert response.json()["recommendations"][0]["rationale"] == "Buy — 1/1 policy gates passed."
