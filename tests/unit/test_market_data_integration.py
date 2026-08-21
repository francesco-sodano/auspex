"""Integration seams: collector ingest hook and ``market-data-*`` CLI wiring."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from auspex.cli.main import _build_arg_parser
from auspex.cli.market_data import _resolve_security_ids, _summarise
from auspex.collectors.price_collector import PriceCollector
from auspex.marketdata.quarantine import exclude_quarantined, is_quarantined, quarantined_only
from auspex.models.market_integrity import (
    IntegrityCode,
    IntegrityFinding,
    IntegritySeverity,
    MarketDataRepairManifest,
    SecurityIntegrityReport,
)
from auspex.persistence.memory import InMemoryPriceSink, InMemoryWatermarkStore
from auspex.providers.base import PriceBarDTO


def dto(day: int, close: str = "100", **overrides) -> PriceBarDTO:
    payload: dict[str, object] = {
        "ticker": "TEST",
        "session_date": date(2024, 1, day),
        "open_raw": Decimal(close),
        "high_raw": Decimal(close),
        "low_raw": Decimal(close),
        "close_raw": Decimal(close),
        "volume": 1_000,
        "close_adjusted": Decimal(close),
        "adjustment_factor": Decimal("1"),
        "split_factor": Decimal("1"),
        "dividend_amount": Decimal("0"),
    }
    payload.update(overrides)
    return PriceBarDTO(**payload)


class StubProvider:
    def __init__(self, bars: list[PriceBarDTO]) -> None:
        self._bars = bars
        self.calls: list[tuple[str, date]] = []

    async def get_daily_prices(self, ticker: str, since: date) -> list[PriceBarDTO]:
        self.calls.append((ticker, since))
        return self._bars


async def collect(bars: list[PriceBarDTO]):
    sink = InMemoryPriceSink()
    collector = PriceCollector(StubProvider(bars), sink, InMemoryWatermarkStore())
    result = await collector.collect("sec-1", "TEST", date(2024, 1, 1))
    return result, sink


# --------------------------------------------------------------------------
# collector
# --------------------------------------------------------------------------


async def test_collector_stores_raw_provider_values_verbatim() -> None:
    result, sink = await collect([dto(2, "123.45")])

    assert result.items_written == 1
    assert result.items_quarantined == 0
    bar = sink.raw_all()[0]
    assert bar.close_raw == "123.45"
    assert bar.close_adjusted == "123.45"
    assert bar.quarantined is False


async def test_collector_quarantines_impossible_bars_at_ingest() -> None:
    result, sink = await collect(
        [dto(2), dto(3, "100", high_raw=Decimal("1"), low_raw=Decimal("200"))]
    )

    assert result.items_seen == 2
    assert result.items_written == 2
    assert result.items_quarantined == 1
    quarantined = quarantined_only(sink.raw_all())
    assert len(quarantined) == 1
    assert IntegrityCode.IMPOSSIBLE_OHLC.value in quarantined[0].quarantine_codes
    # The bad bar is stored but invisible to scoring/performance reads.
    assert [bar.session_date for bar in sink.all()] == [date(2024, 1, 2)]


async def test_collector_quarantines_non_positive_prices() -> None:
    result, sink = await collect([dto(2, "0")])
    assert result.items_quarantined == 1
    assert IntegrityCode.NON_POSITIVE_PRICE.value in sink.raw_all()[0].quarantine_codes


async def test_collector_counts_duplicate_sessions_in_one_batch() -> None:
    result, sink = await collect([dto(2), dto(2), dto(3)])

    assert result.items_seen == 3
    assert result.items_skipped_duplicate == 1
    # Same deterministic id, so the later bar simply overwrites the earlier one.
    assert len(sink.raw_all()) == 2


async def test_collector_advances_the_watermark_to_the_latest_session() -> None:
    sink = InMemoryPriceSink()
    watermarks = InMemoryWatermarkStore()
    collector = PriceCollector(StubProvider([dto(2), dto(5)]), sink, watermarks)

    await collector.collect("sec-1", "TEST", date(2024, 1, 1))

    assert await watermarks.get_watermark("price:sec-1") == "2024-01-05"


async def test_collector_degrades_on_provider_failure() -> None:
    class Boom:
        async def get_daily_prices(self, ticker: str, since: date) -> list[PriceBarDTO]:
            raise RuntimeError("provider down")

    collector = PriceCollector(Boom(), InMemoryPriceSink(), InMemoryWatermarkStore())
    result = await collector.collect("sec-1", "TEST", date(2024, 1, 1))

    assert result.degraded is True
    assert "provider down" in (result.error or "")
    assert result.items_written == 0


# --------------------------------------------------------------------------
# quarantine helpers
# --------------------------------------------------------------------------


async def test_quarantine_helpers_partition_a_series() -> None:
    _, sink = await collect(
        [dto(2), dto(3, "100", high_raw=Decimal("1"), low_raw=Decimal("200"))]
    )
    bars = sink.raw_all()

    assert len(exclude_quarantined(bars)) == 1
    assert len(quarantined_only(bars)) == 1
    assert is_quarantined(quarantined_only(bars)[0]) is True


def test_is_quarantined_tolerates_objects_without_the_flag() -> None:
    @dataclass
    class Legacy:
        security_id: str

    assert is_quarantined(Legacy("sec-1")) is False


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_market_data_diagnose_is_registered() -> None:
    args = _build_arg_parser().parse_args(
        ["market-data-diagnose", "--ticker", "DZSI", "--ticker", "AAPL", "--json"]
    )
    assert args.command == "market-data-diagnose"
    assert args.ticker == ["DZSI", "AAPL"]
    assert args.json is True


def test_market_data_repair_is_registered() -> None:
    args = _build_arg_parser().parse_args(["market-data-repair", "--dry-run"])
    assert args.command == "market-data-repair"
    assert args.dry_run is True
    assert args.ticker == []
    assert args.json is False


@dataclass
class StubSecurity:
    id: str
    ticker: str


@dataclass
class StubUniverse:
    securities: list[StubSecurity]


UNIVERSE = StubUniverse([StubSecurity("sec-1", "DZSI"), StubSecurity("sec-2", "AAPL")])


def test_resolve_security_ids_defaults_to_the_whole_universe() -> None:
    ids, tickers = _resolve_security_ids(UNIVERSE, None)
    assert ids == ["sec-1", "sec-2"]
    assert tickers == {"sec-1": "DZSI", "sec-2": "AAPL"}


def test_resolve_security_ids_matches_tickers_case_insensitively() -> None:
    ids, _ = _resolve_security_ids(UNIVERSE, ["dzsi"])
    assert ids == ["sec-1"]


def test_resolve_security_ids_rejects_unknown_tickers() -> None:
    with pytest.raises(SystemExit) as excinfo:
        _resolve_security_ids(UNIVERSE, ["DZSI", "NOPE"])
    assert "NOPE" in str(excinfo.value)


def test_summarise_counts_findings_by_severity() -> None:
    report = SecurityIntegrityReport(
        security_id="sec-1",
        ticker="DZSI",
        findings=[
            IntegrityFinding(
                security_id="sec-1",
                session_date=date(2024, 1, 2),
                code=IntegrityCode.IMPOSSIBLE_OHLC,
                severity=IntegritySeverity.ERROR,
                detail="bad",
            ),
            IntegrityFinding(
                security_id="sec-1",
                session_date=date(2024, 1, 3),
                code=IntegrityCode.ADJUSTED_SERIES_INCONSISTENT,
                severity=IntegritySeverity.WARNING,
                detail="drift",
            ),
        ],
    )
    manifest = MarketDataRepairManifest(
        id=MarketDataRepairManifest.make_id(4),
        revision=4,
        created_at=datetime(2024, 1, 3, tzinfo=UTC),
        fingerprint="abc",
        policy_version="v1",
        securities=[report],
    )

    summary = _summarise(manifest)

    assert summary["revision"] == 4
    assert summary["error_findings"] == 1
    assert summary["warning_findings"] == 1
