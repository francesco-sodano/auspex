"""Repair-planning and service-level tests (arc42 §5.3).

Covers the two hard invariants: raw observations are never mutated, and a
split is never invented when authoritative evidence is absent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from auspex.marketdata.detect import to_decimal
from auspex.marketdata.policy import DEFAULT_POLICY
from auspex.marketdata.recompute import calendar_lookback_days, merge_ranges, targets_from_manifest
from auspex.marketdata.repair import plan_security_repair
from auspex.marketdata.service import MarketDataIntegrityService, plan_fingerprint
from auspex.models.market_integrity import (
    MANIFEST_CONFIG_TYPE,
    AffectedRange,
    IntegrityCode,
    IntegritySeverity,
    MarketDataRepairManifest,
)
from auspex.persistence.memory import (
    InMemoryPriceIntegrityStore,
    InMemoryPriceSink,
    InMemoryRepairManifestStore,
)
from tests.unit.test_market_data_detect import SECURITY, make_bar

NOW = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)


def frozen_clock() -> datetime:
    return NOW


async def seed(bars) -> tuple[InMemoryPriceSink, MarketDataIntegrityService]:
    sink = InMemoryPriceSink()
    for bar in bars:
        await sink.upsert_price_bar(bar)
    service = MarketDataIntegrityService(
        InMemoryPriceIntegrityStore(sink),
        InMemoryRepairManifestStore(),
        clock=frozen_clock,
        ticker_by_security={SECURITY: "TEST"},
    )
    return sink, service


def broken_split_series() -> list:
    """A 2:1 split whose adjusted series was never back-adjusted."""

    return [
        make_bar(0, "100", adjusted="100", factor="1"),
        make_bar(1, "50", adjusted="50", factor="1", split="2"),
        make_bar(2, "51", adjusted="51", factor="1"),
    ]


# --------------------------------------------------------------------------
# plan_security_repair
# --------------------------------------------------------------------------


def test_repair_rebuilds_the_adjusted_series_from_authoritative_split() -> None:
    plan = plan_security_repair(SECURITY, broken_split_series(), revision=1, now=NOW)

    assert plan.has_changes
    repaired = {bar.session_date: bar for bar in plan.updates}
    first = repaired[broken_split_series()[0].session_date]
    assert to_decimal(first.close_adjusted) == Decimal("50")
    assert to_decimal(first.adjustment_factor) == Decimal("0.5")
    assert first.integrity_revision == 1
    assert first.repaired_at == NOW


def test_repair_abstains_when_adjustment_convention_is_unverified() -> None:
    bars = [
        make_bar(0, "100", adjusted="50", factor="0.5"),
        make_bar(1, "101", adjusted="70", factor="0.7"),
        make_bar(2, "102", adjusted="90", factor="0.9"),
    ]

    plan = plan_security_repair(SECURITY, bars, revision=1, now=NOW)

    assert plan.report.repairs == []
    assert any(
        finding.code is IntegrityCode.ADJUSTED_SERIES_INCONSISTENT
        and "left unchanged" in finding.detail
        for finding in plan.report.findings
    )


def test_repair_never_mutates_raw_observations() -> None:
    original = broken_split_series()
    plan = plan_security_repair(SECURITY, original, revision=1, now=NOW)
    by_id = {bar.id: bar for bar in original}
    for updated in plan.updates:
        source = by_id[updated.id]
        assert updated.open_raw == source.open_raw
        assert updated.high_raw == source.high_raw
        assert updated.low_raw == source.low_raw
        assert updated.close_raw == source.close_raw
        assert updated.volume == source.volume
        assert updated.split_factor == source.split_factor
        assert updated.dividend_amount == source.dividend_amount


def test_repair_captures_provider_provenance_once() -> None:
    plan = plan_security_repair(SECURITY, broken_split_series(), revision=1, now=NOW)
    first = plan.updates[0]
    assert first.close_adjusted_source == "100"
    assert first.adjustment_factor_source == "1"

    # Re-planning over the already-repaired bar must not overwrite provenance.
    again = plan_security_repair(
        SECURITY,
        [first, *broken_split_series()[1:]],
        revision=2,
        now=NOW,
    )
    for bar in again.updates:
        if bar.id == first.id:
            assert bar.close_adjusted_source == "100"


def test_repair_is_idempotent() -> None:
    bars = broken_split_series()
    first = plan_security_repair(SECURITY, bars, revision=1, now=NOW)
    applied = {bar.id: bar for bar in bars}
    applied.update({bar.id: bar for bar in first.updates})

    second = plan_security_repair(SECURITY, list(applied.values()), revision=2, now=NOW)
    assert second.updates == []
    assert second.report.repairs == []


def test_repair_never_invents_a_split_and_quarantines_stale_history() -> None:
    bars = [
        make_bar(0, "100", adjusted="100"),
        make_bar(1, "100", adjusted="100"),
        # Halving with split_factor == 1: no authoritative evidence.
        make_bar(2, "50", adjusted="50"),
        make_bar(3, "51", adjusted="51"),
    ]
    plan = plan_security_repair(SECURITY, bars, revision=1, now=NOW)

    quarantined = {
        bar.session_date: bar
        for bar in plan.updates
        if bar.quarantined
    }
    assert quarantined == {}
    assert any(
        finding.code is IntegrityCode.UNEXPLAINED_SCALE_BREAK
        and finding.severity is IntegritySeverity.WARNING
        for finding in plan.report.findings
    )
    # No synthetic split factor was written anywhere.
    assert all(bar.split_factor == "1" for bar in plan.updates)


def test_repair_quarantines_impossible_bars() -> None:
    bars = [
        make_bar(0, "100", adjusted="100"),
        make_bar(1, "100", adjusted="100", high="1", low="200"),
    ]
    plan = plan_security_repair(SECURITY, bars, revision=1, now=NOW)
    quarantined = {bar.session_date for bar in plan.updates if bar.quarantined}
    assert bars[1].session_date in quarantined


def test_repair_quarantines_duplicate_losers_and_keeps_the_winner() -> None:
    bars = [
        make_bar(0, "100", adjusted="100", bar_id="sec-1:2024-01-01"),
        make_bar(0, "100", adjusted="100", bar_id="sec-1:2024-01-01#legacy"),
    ]
    plan = plan_security_repair(SECURITY, bars, revision=1, now=NOW)
    quarantined = {bar.id for bar in plan.updates if bar.quarantined}
    assert quarantined == {"sec-1:2024-01-01"}
    assert IntegrityCode.DUPLICATE_BAR.value in plan.report.findings[0].code.value


def test_repair_releases_a_bar_that_is_no_longer_broken() -> None:
    healthy = make_bar(0, "100", adjusted="100").model_copy(
        update={"quarantined": True, "quarantine_codes": ["IMPOSSIBLE_OHLC"]}
    )
    plan = plan_security_repair(SECURITY, [healthy, make_bar(1, "101", adjusted="101")], revision=3, now=NOW)
    released = [bar for bar in plan.updates if bar.id == healthy.id]
    assert released and released[0].quarantined is False
    assert released[0].quarantine_codes == []
    assert plan.report.released_dates == [healthy.session_date]


def test_repair_reports_affected_ranges() -> None:
    plan = plan_security_repair(SECURITY, broken_split_series(), revision=1, now=NOW)
    reasons = {item.reason for item in plan.report.affected_ranges}
    assert "adjusted_repair" in reasons
    assert all(item.security_id == SECURITY for item in plan.report.affected_ranges)


# --------------------------------------------------------------------------
# service
# --------------------------------------------------------------------------


async def test_diagnose_writes_nothing() -> None:
    sink, service = await seed(broken_split_series())
    before = {bar.id: bar.close_adjusted for bar in sink.raw_all()}

    manifest = await service.diagnose()

    assert manifest.dry_run is True
    assert manifest.bars_repaired > 0
    assert {bar.id: bar.close_adjusted for bar in sink.raw_all()} == before
    assert await service._manifests.latest() is None


async def test_repair_persists_bars_and_a_manifest_revision() -> None:
    sink, service = await seed(broken_split_series())

    manifest = await service.repair()

    assert manifest.revision == 1
    assert manifest.id == "market_data_repair:000001"
    assert manifest.config_type == MANIFEST_CONFIG_TYPE
    assert manifest.dry_run is False
    assert manifest.policy_version
    stored = {bar.session_date: bar for bar in sink.raw_all()}
    assert to_decimal(stored[broken_split_series()[0].session_date].close_adjusted) == Decimal("50")


async def test_repair_is_a_no_op_on_the_second_pass() -> None:
    _, service = await seed(broken_split_series())

    first = await service.repair()
    second = await service.repair()

    assert second.revision == first.revision
    assert second.id == first.id
    history = await service._manifests.history(limit=10)
    assert len(history) == 1


async def test_identical_manifest_reapplies_lost_bar_repairs() -> None:
    sink = InMemoryPriceSink()
    original = broken_split_series()
    for bar in original:
        await sink.upsert_price_bar(bar)
    manifests = InMemoryRepairManifestStore()
    service = MarketDataIntegrityService(
        InMemoryPriceIntegrityStore(sink),
        manifests,
        clock=frozen_clock,
    )
    first = await service.repair()
    await sink.upsert_price_bar(original[0])

    second = await service.repair()

    repaired = {
        bar.session_date: bar
        for bar in sink.raw_all()
    }[original[0].session_date]
    assert repaired.close_adjusted == "50.000000"
    assert second.fingerprint == first.fingerprint
    assert len(await manifests.history(limit=10)) == 1


async def test_repair_dry_run_leaves_storage_untouched() -> None:
    sink, service = await seed(broken_split_series())
    before = {bar.id: bar.close_adjusted for bar in sink.raw_all()}

    manifest = await service.repair(dry_run=True)

    assert manifest.dry_run is True
    assert {bar.id: bar.close_adjusted for bar in sink.raw_all()} == before
    assert await service._manifests.latest() is None


async def test_quarantined_bars_are_hidden_from_scoring_reads() -> None:
    sink, service = await seed(
        [
            make_bar(0, "100", adjusted="100"),
            make_bar(1, "100", adjusted="100", high="1", low="200"),
            make_bar(2, "101", adjusted="101"),
            make_bar(3, "102", adjusted="102"),
        ]
    )

    await service.repair()

    assert len(sink.raw_all()) == 4
    visible = sink.all()
    assert len(visible) < 4
    assert all(bar.quarantined is False for bar in visible)


async def test_repair_honours_an_explicit_security_filter() -> None:
    other = [make_bar(index, "10", adjusted="1", security_id="sec-2") for index in range(2)]
    sink, service = await seed([*broken_split_series(), *other])

    manifest = await service.repair(security_ids=[SECURITY])

    assert [report.security_id for report in manifest.securities] == [SECURITY]
    untouched = [bar for bar in sink.raw_all() if bar.security_id == "sec-2"]
    assert all(bar.integrity_revision == 0 for bar in untouched)


async def test_recompute_targets_cover_the_repaired_window() -> None:
    _, service = await seed(broken_split_series())
    manifest = await service.repair()

    targets = await service.recompute_targets(manifest)

    assert targets
    target = targets[0]
    assert target.security_id == SECURITY
    # Only the pre-split bar needed rewriting, so that is the affected window.
    repaired_date = broken_split_series()[0].session_date
    assert target.end_date == repaired_date
    # The window is extended back by the longest forward-return horizon.
    assert target.start_date < repaired_date
    assert target.as_dict()["security_id"] == SECURITY


async def test_recompute_targets_are_empty_without_a_manifest() -> None:
    _, service = await seed([])
    assert await service.recompute_targets() == []


def test_plan_fingerprint_ignores_revision_and_time() -> None:
    bars = broken_split_series()
    left = plan_security_repair(SECURITY, bars, revision=1, now=NOW)
    right = plan_security_repair(SECURITY, bars, revision=99, now=datetime(2030, 1, 1, tzinfo=UTC))
    assert plan_fingerprint([left], "v1") == plan_fingerprint([right], "v1")


def test_plan_fingerprint_changes_with_policy_version() -> None:
    plan = plan_security_repair(SECURITY, broken_split_series(), revision=1, now=NOW)
    assert plan_fingerprint([plan], "v1") != plan_fingerprint([plan], "v2")


# --------------------------------------------------------------------------
# recompute helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("sessions", "expected"), [(0, 0), (-3, 0), (5, 14), (21, 37)])
def test_calendar_lookback_days(sessions: int, expected: int) -> None:
    assert calendar_lookback_days(sessions) == expected


def test_merge_ranges_collapses_to_one_window_per_security() -> None:
    ranges = [
        AffectedRange(
            security_id=SECURITY,
            start_date=make_bar(5, "1").session_date,
            end_date=make_bar(6, "1").session_date,
            reason="adjusted_repair",
        ),
        AffectedRange(
            security_id=SECURITY,
            start_date=make_bar(1, "1").session_date,
            end_date=make_bar(2, "1").session_date,
            reason="quarantine",
        ),
    ]
    merged = merge_ranges(ranges, policy=DEFAULT_POLICY)
    assert len(merged) == 1
    target = merged[0]
    assert target.end_date == make_bar(6, "1").session_date
    assert target.start_date < make_bar(1, "1").session_date
    assert target.reasons == ("adjusted_repair", "quarantine")


def test_targets_from_manifest_is_empty_for_a_clean_manifest() -> None:
    manifest = MarketDataRepairManifest(
        id=MarketDataRepairManifest.make_id(1),
        revision=1,
        created_at=NOW,
        fingerprint="abc",
        policy_version="v1",
    )
    assert targets_from_manifest(manifest, policy=DEFAULT_POLICY) == []
