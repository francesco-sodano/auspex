"""Integration: the nightly run with two users (arc42 §6.1, §5.7).

Exercises the real 20-step pipeline over seeded fixture evidence, but with
two application users instead of one, and asserts the properties that make
multi-user Auspex correct:

* shared research (scores, snapshots, leg changes) is computed once and is
  byte-identical for both users;
* each user's recommendations and projection are written under their own
  ``user_id`` and reflect only their own ledger;
* a rejected decision is suppressed for that user only, and only while its
  decision signature is unchanged;
* one user's failure does not deny the other their nightly output.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

from auspex.models.common import utc_now
from auspex.models.enums import Action, DispositionStatus, RunStatus
from auspex.models.policy import RecommendationDisposition
from auspex.pipeline.context import PipelineContext
from auspex.pipeline.fanout import run_multi_user_pipeline
from auspex.portfolio.port import Holding, PortfolioSnapshot
from tests.integration.conftest import (
    build_repos,
    seed_channel_a_extraction,
    seed_fundamentals,
    seed_fx,
    seed_insider_form4,
    seed_prices,
)

ALICE = "user-alice"
BOB = "user-bob"
AS_OF = date(2026, 8, 8)


class StaticPortfolioReader:
    """A per-user ledger binding that can only ever return one user's book."""

    def __init__(self, holdings: list[Holding], cash_chf: Decimal) -> None:
        self._holdings = holdings
        self._cash_chf = cash_chf
        self.reads = 0

    async def read_snapshot(self, as_of, fx_rate_to_chf=None) -> PortfolioSnapshot:
        self.reads += 1
        return PortfolioSnapshot(
            holdings=list(self._holdings),
            cash_chf=self._cash_chf,
            as_of=as_of,
            lot_level=True,
        )

    def degraded_fields(self) -> list[str]:
        return []


class DispositionRepo:
    """Partition-respecting stand-in for `recommendation_dispositions`."""

    def __init__(self, rows: list[RecommendationDisposition] | None = None) -> None:
        self.rows = list(rows or [])

    async def query(self, query, parameters=None, partition_key=None):
        wanted = {p["name"].lstrip("@"): p["value"] for p in (parameters or [])}["user_id"]
        assert partition_key in (None, wanted)
        return [row for row in self.rows if row.user_id == wanted]

    async def upsert(self, item) -> None:
        self.rows = [row for row in self.rows if row.id != item.id] + [item]


def seed_evidence(repos, universe):
    nvda = universe.by_ticker()["NVDA"]
    amd = universe.by_ticker()["AMD"]

    seed_fundamentals(repos, nvda.id, AS_OF)
    seed_channel_a_extraction(repos, nvda.id, AS_OF)
    seed_insider_form4(repos, nvda.id, AS_OF)
    seed_prices(repos, nvda.id, AS_OF, "180")

    seed_fundamentals(repos, amd.id, AS_OF, variant="b")
    seed_channel_a_extraction(repos, amd.id, AS_OF, variant="b")
    seed_prices(repos, amd.id, AS_OF, "220")

    seed_fx(repos, AS_OF)
    return nvda, amd


def run_two_user_night(universe, config_bundle, readers, dispositions=None):
    repos = build_repos()
    nvda, amd = seed_evidence(repos, universe)
    repos.recommendation_disposition_repo = dispositions or DispositionRepo()

    ctx = PipelineContext(
        universe=universe,
        config=config_bundle,
        as_of_date=AS_OF,
        user_id=ALICE,
        repos=repos,
    )

    result = asyncio.run(
        run_multi_user_pipeline(
            ctx,
            [ALICE, BOB],
            portfolio_reader_factory=readers.get,
            concurrency=2,
        )
    )
    return result, repos, nvda, amd


def recommendations_for(repos, user_id: str) -> dict[str, object]:
    return {
        row.security_id: row
        for row in repos.recommendation_repo.all()
        if row.user_id == user_id
    }


def test_two_users_share_research_but_not_portfolio_state(universe, config_bundle):
    nvda_ticker = universe.by_ticker()["NVDA"]
    readers = {
        ALICE: StaticPortfolioReader([], Decimal("20000")),
        BOB: StaticPortfolioReader(
            [Holding(ticker=nvda_ticker.ticker, quantity=Decimal("50"))],
            Decimal("100"),
        ),
    }

    result, repos, nvda, _amd = run_two_user_night(universe, config_bundle, readers)

    assert result.manifest.status in (RunStatus.SUCCESS, RunStatus.DEGRADED)
    assert sorted(result.succeeded_user_ids) == [ALICE, BOB]

    # The closing validation runs *after* the fan-out, so it reconciles a real
    # user's recommendations rather than an empty set.
    validate = result.manifest.step_by_name("VALIDATE")
    assert validate.status == "SUCCESS"
    assert "does not match" not in (validate.detail or "")

    # Shared research: one set of scores, computed once, not per user.
    scores = repos.score_repo.all()
    assert len({score.security_id for score in scores}) == len(scores)
    assert all(not hasattr(score, "user_id") for score in scores)

    # Private state: each user's own ledger drove their own recommendations.
    alice = recommendations_for(repos, ALICE)
    bob = recommendations_for(repos, BOB)
    assert alice and bob
    assert set(alice) == set(bob)
    assert alice[nvda.id].current_weight_pct != bob[nvda.id].current_weight_pct
    assert all(row.allocation_mode == "JOINT_CASH" for row in alice.values())
    alice_buy_notional = sum(
        Decimal(row.suggested_trade_chf or "0")
        for row in alice.values()
        if row.action in {Action.BUY, Action.ADD}
    )
    assert alice_buy_notional <= Decimal("17000")
    assert readers[ALICE].reads >= 1
    assert readers[BOB].reads >= 1

    # Projections are written per user, under the user's own partition.
    projections = {row.user_id: row for row in repos.portfolio_projection_repo.all()}
    assert set(projections) == {ALICE, BOB}
    assert projections[ALICE].id.startswith(f"{ALICE}:")
    assert projections[BOB].id.startswith(f"{BOB}:")
    assert Decimal(projections[ALICE].cash_chf) == Decimal("20000")
    assert Decimal(projections[BOB].cash_chf) == Decimal("100")


def test_every_recommendation_carries_a_decision_signature(universe, config_bundle):
    readers = {
        ALICE: StaticPortfolioReader([], Decimal("20000")),
        BOB: StaticPortfolioReader([], Decimal("20000")),
    }

    _result, repos, _nvda, _amd = run_two_user_night(universe, config_bundle, readers)

    alice = recommendations_for(repos, ALICE)
    assert all(row.decision_signature for row in alice.values())
    assert all(row.decision_signature.startswith("v2:") for row in alice.values())

    # Identical portfolios and identical shared research produce identical
    # signatures — the signature is a property of the decision, not the user.
    bob = recommendations_for(repos, BOB)
    for security_id, row in alice.items():
        assert row.decision_signature == bob[security_id].decision_signature


def test_rejected_signature_is_suppressed_for_that_user_only(universe, config_bundle):
    readers = {
        ALICE: StaticPortfolioReader([], Decimal("20000")),
        BOB: StaticPortfolioReader([], Decimal("20000")),
    }

    # First night: nothing is suppressed. Capture a real signature to reject.
    _result, repos, _nvda, _amd = run_two_user_night(universe, config_bundle, readers)
    actionable = next(
        (row for row in recommendations_for(repos, ALICE).values() if row.action != Action.HOLD_NO_ACTION),
        None,
    )
    assert actionable is not None
    assert actionable.suppressed is False

    dispositions = DispositionRepo(
        [
            RecommendationDisposition(
                id=f"{ALICE}:{actionable.security_id}",
                user_id=ALICE,
                security_id=actionable.security_id,
                disposition=DispositionStatus.REJECTED,
                decision_signature=actionable.decision_signature,
                recommendation_id=actionable.id,
                as_of_date=AS_OF,
                recorded_at=utc_now(),
            )
        ]
    )

    # Second night: same evidence, same portfolios, so the same signature.
    _result2, repos2, _n, _a = run_two_user_night(
        universe, config_bundle, readers, dispositions=dispositions
    )

    alice_row = recommendations_for(repos2, ALICE)[actionable.security_id]
    bob_row = recommendations_for(repos2, BOB)[actionable.security_id]

    assert alice_row.suppressed is True
    assert alice_row.disposition is DispositionStatus.REJECTED
    assert "REJECTED" in alice_row.suppression_reason
    # Bob never answered anything: he still sees the decision.
    assert bob_row.suppressed is False
    assert bob_row.disposition is None


def test_changed_signature_reappears_despite_a_rejection(universe, config_bundle):
    readers = {
        ALICE: StaticPortfolioReader([], Decimal("20000")),
        BOB: StaticPortfolioReader([], Decimal("20000")),
    }
    _result, repos, _nvda, _amd = run_two_user_night(universe, config_bundle, readers)
    actionable = next(
        (row for row in recommendations_for(repos, ALICE).values() if row.action != Action.HOLD_NO_ACTION),
        None,
    )
    assert actionable is not None

    stale = DispositionRepo(
        [
            RecommendationDisposition(
                id=f"{ALICE}:{actionable.security_id}",
                user_id=ALICE,
                security_id=actionable.security_id,
                disposition=DispositionStatus.REJECTED,
                # A signature from a materially different decision.
                decision_signature="v1:some-older-decision",
                recorded_at=utc_now(),
            )
        ]
    )

    _result2, repos2, _n, _a = run_two_user_night(universe, config_bundle, readers, dispositions=stale)

    assert recommendations_for(repos2, ALICE)[actionable.security_id].suppressed is False


def test_one_users_ledger_failure_does_not_deny_the_other(universe, config_bundle):
    class BrokenReader:
        async def read_snapshot(self, as_of, fx_rate_to_chf=None):
            raise RuntimeError("source ledger unavailable")

        def degraded_fields(self) -> list[str]:
            return []

    readers = {
        ALICE: StaticPortfolioReader([], Decimal("20000")),
        BOB: BrokenReader(),
    }

    result, repos, _nvda, _amd = run_two_user_night(universe, config_bundle, readers)

    assert result.failed_user_ids == [BOB]
    assert result.succeeded_user_ids == [ALICE]
    assert recommendations_for(repos, ALICE)
    assert recommendations_for(repos, BOB) == {}
    # The night still publishes: shared research plus one complete user.
    assert result.manifest.status in (RunStatus.SUCCESS, RunStatus.DEGRADED, RunStatus.RUNNING)
    assert result.manifest.step_by_name("RUN_POLICY").degraded is True
