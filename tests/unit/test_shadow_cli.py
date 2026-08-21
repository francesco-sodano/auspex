"""Shadow-study CLI seam (arc42 §5.8).

Exercises the assembly and publication boundary with plain stub objects, so the
seam is covered without Cosmos, repositories or price history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import pytest

from auspex.cli.shadow_cli import (
    applicable_legs_for,
    build_cross_sections,
    render_report,
    run_shadow_study,
)
from auspex.models.enums import FilerProfile, LegName
from auspex.performance.shadow import (
    PRODUCTION_DOMESTIC_WEIGHTS,
    SHADOW_METRIC_TYPE,
    ShadowVariant,
    default_pre_registration,
    run_shadow_comparison,
)

START = date(2026, 1, 5)
HORIZONS = (21, 63, 126)


@dataclass
class _Leg:
    z: str | None


@dataclass
class _Snap:
    security_id: str
    as_of_date: date
    percentile: str | None
    composite: str | None = None
    filer_profile: FilerProfile = FilerProfile.DOMESTIC
    legs: dict = field(default_factory=dict)


class _Repo:
    def __init__(self) -> None:
        self.written: list = []

    async def upsert(self, metric) -> None:
        self.written.append(metric)


def _legs(base: Decimal, *, skip: LegName | None = None) -> dict:
    return {leg: _Leg(z=str(base)) for leg in PRODUCTION_DOMESTIC_WEIGHTS if leg is not skip}


def _snapshots(days: int = 20, names: int = 10) -> list[_Snap]:
    rows: list[_Snap] = []
    for day in range(days):
        for index in range(names):
            wobble = Decimal((index * 3 + day * 7) % 5) / Decimal("100")
            base = Decimal(index) / Decimal("10") + wobble
            rows.append(
                _Snap(
                    security_id=f"S{index:02d}",
                    as_of_date=START + timedelta(days=day),
                    percentile=str(base),
                    composite=str(base),
                    legs=_legs(base),
                )
            )
    return rows


def _forward_return(security_id: str, _as_of: date, _horizon: int) -> Decimal | None:
    return Decimal(int(security_id[1:])) / Decimal("100")


def _no_returns(_security_id: str, _as_of: date, _horizon: int) -> Decimal | None:
    return None


class TestApplicableLegs:
    def test_domestic_filers_carry_every_leg(self) -> None:
        snap = _Snap(security_id="A", as_of_date=START, percentile="1")
        assert applicable_legs_for(snap) == frozenset(LegName)

    def test_fpi_filers_have_no_smart_money_leg(self) -> None:
        """A foreign private issuer files no 13F/13D.

        Treating that structural absence as a weak signal is exactly the bias
        this whole workstream exists to remove, so the denominator must drop
        the leg rather than score it at zero.
        """

        snap = _Snap(security_id="A", as_of_date=START, percentile="1", filer_profile=FilerProfile.FPI)
        legs = applicable_legs_for(snap)
        assert LegName.SMART_MONEY not in legs
        assert LegName.THESIS_LINKAGE in legs


class TestBuildCrossSections:
    def test_composite_zero_is_not_replaced_by_percentile(self) -> None:
        snapshots = [
            _Snap(
                security_id="S00",
                as_of_date=START,
                percentile="95",
                composite="0",
                legs=_legs(Decimal(0)),
            ),
            _Snap(
                security_id="S01",
                as_of_date=START,
                percentile="50",
                composite=None,
                legs=_legs(Decimal(1)),
            ),
        ]

        sections = build_cross_sections(
            snapshots,
            _forward_return,
            HORIZONS,
        )

        assert sections[0].champion_scores_by_security == {
            "S00": Decimal(0)
        }

    def test_groups_snapshots_into_ordered_dates(self) -> None:
        sections = build_cross_sections(_snapshots(days=3), _forward_return, HORIZONS)
        assert len(sections) == 3
        assert [section.as_of_date for section in sections] == sorted(s.as_of_date for s in sections)
        assert set(sections[0].forward_returns_usd_by_horizon) == set(HORIZONS)
        assert len(sections[0].champion_scores_by_security) == 10

    def test_unscored_securities_are_skipped(self) -> None:
        rows = _snapshots(days=1)
        rows[0].percentile = None
        rows[0].composite = None
        sections = build_cross_sections(rows, _forward_return, HORIZONS)
        assert len(sections[0].champion_scores_by_security) == 9

    def test_dates_without_any_matched_return_are_dropped(self) -> None:
        """A date with no realised forward return carries no information.

        Keeping it would inflate the date count in the report without adding a
        single usable IC.
        """

        assert build_cross_sections(_snapshots(days=3), _no_returns, HORIZONS) == []

    def test_legs_without_a_z_score_are_omitted_rather_than_zeroed(self) -> None:
        rows = _snapshots(days=1)
        rows[0].legs[LegName.SMART_MONEY] = _Leg(z=None)
        sections = build_cross_sections(rows, _forward_return, HORIZONS)
        legs = sections[0].leg_z_by_security[rows[0].security_id]
        assert LegName.SMART_MONEY not in legs

    def test_fpi_exclusion_reaches_the_cross_section(self) -> None:
        rows = _snapshots(days=1)
        rows[2].filer_profile = FilerProfile.FPI
        sections = build_cross_sections(rows, _forward_return, HORIZONS)
        assert LegName.SMART_MONEY not in sections[0].applicable_legs(rows[2].security_id)

    def test_no_snapshots_produce_no_sections(self) -> None:
        assert build_cross_sections([], _forward_return, HORIZONS) == []


class TestRenderReport:
    def test_renders_every_variant_and_comparison(self) -> None:
        sections = build_cross_sections(_snapshots(), _forward_return, HORIZONS)
        report = run_shadow_comparison(default_pre_registration(START), sections)
        lines = render_report(report)
        assert lines
        assert all(isinstance(line, str) for line in lines)
        text = "\n".join(lines)
        assert "shadow study shadow-v4.2-neutral-missing-v1" in text
        assert "fingerprint" in text
        assert "champion" in text
        assert "corrected_fixed" in text

    def test_flags_an_underpowered_study(self) -> None:
        sections = build_cross_sections(_snapshots(days=3), _forward_return, HORIZONS)
        report = run_shadow_comparison(default_pre_registration(START), sections)
        assert "UNDERPOWERED" in "\n".join(render_report(report))

    def test_renders_an_empty_report_without_failing(self) -> None:
        report = run_shadow_comparison(default_pre_registration(START), [])
        assert render_report(report)


class TestRunShadowStudy:
    async def test_dry_run_computes_rows_but_writes_nothing(self) -> None:
        """The default must never touch the shared performance surface.

        A shadow study is an experiment; one that publishes by default stops
        being one and starts being a second, unreviewed source of truth.
        """

        repo = _Repo()
        report, metrics = await run_shadow_study(
            _snapshots(),
            _forward_return,
            as_of_date=START,
            performance_repo=repo,
        )
        assert metrics
        assert repo.written == []
        assert report.dates_evaluated == 20

    async def test_publishing_writes_every_row(self) -> None:
        repo = _Repo()
        _report, metrics = await run_shadow_study(
            _snapshots(),
            _forward_return,
            as_of_date=START,
            publish=True,
            performance_repo=repo,
        )
        assert len(repo.written) == len(metrics)
        assert {metric.metric_type for metric in repo.written} == {SHADOW_METRIC_TYPE}

    async def test_publishing_without_a_repository_is_refused(self) -> None:
        with pytest.raises(ValueError):
            await run_shadow_study(_snapshots(days=3), _forward_return, as_of_date=START, publish=True)

    async def test_named_challengers_are_evaluated(self) -> None:
        challenger = ShadowVariant(
            name="equal_weight_legs",
            description="every leg weighted equally",
            weights={leg: Decimal("1") for leg in PRODUCTION_DOMESTIC_WEIGHTS},
        )
        report, _metrics = await run_shadow_study(
            _snapshots(),
            _forward_return,
            as_of_date=START,
            challengers=(challenger,),
        )
        assert "equal_weight_legs" in {result.variant for result in report.results}

    async def test_an_explicit_registration_is_honoured(self) -> None:
        registration = default_pre_registration(date(2026, 2, 2))
        report, _metrics = await run_shadow_study(
            _snapshots(days=3),
            _forward_return,
            registration=registration,
            as_of_date=START,
        )
        assert report.fingerprint == registration.fingerprint
        assert report.registration.registered_on == date(2026, 2, 2)
