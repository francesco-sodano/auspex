"""Unit tests for the daily portfolio projection (arc42 §5.7 "Daily projection").

Every gate depends only on quantity and cash; richer fields degrade to
unavailable (never estimated) when the source ledger doesn't supply them.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from auspex.portfolio.port import Holding, PortfolioSnapshot
from auspex.portfolio.projection import project_portfolio


def make_snapshot(holdings: list[Holding], cash_chf: str = "4179") -> PortfolioSnapshot:
    return PortfolioSnapshot(holdings=holdings, cash_chf=Decimal(cash_chf), as_of=date(2026, 8, 8), lot_level=True)


class TestBasicProjection:
    def test_market_value_and_weight_computed_with_full_data(self):
        holding = Holding(
            ticker="AMD",
            quantity=Decimal("42"),
            cost_basis_usd=Decimal("19000"),
            cost_basis_chf=Decimal("16213"),
            open_date=date(2026, 6, 28),
            lot_id="lot-1",
            fx_rate_at_open=Decimal("0.86"),
        )
        snapshot = make_snapshot([holding], cash_chf="4179")
        result = project_portfolio(snapshot, {"AMD": Decimal("486.10")}, Decimal("0.88"), date(2026, 8, 8))

        position = result.positions[0]
        assert position.ticker == "AMD"
        assert position.market_value_usd == Decimal("20416.20")
        assert position.market_value_chf == Decimal("17966.26")
        assert position.unrealised_chf == Decimal("17966.26") - Decimal("16213")
        assert position.degraded_fields == []
        assert position.weight is not None

    def test_total_value_includes_cash(self):
        holding = Holding(ticker="NVDA", quantity=Decimal("10"))
        snapshot = make_snapshot([holding], cash_chf="1000")
        result = project_portfolio(snapshot, {"NVDA": Decimal("100")}, Decimal("1"), date(2026, 8, 8))
        assert result.total_value_chf == Decimal("1000.00") + Decimal("1000")

    def test_portfolio_at_glance_metrics_follow_ledger_formula(self):
        holding = Holding(
            ticker="NVDA",
            quantity=Decimal("10"),
            cost_basis_chf=Decimal("800"),
        )
        snapshot = PortfolioSnapshot(
            holdings=[holding],
            cash_chf=Decimal("250"),
            as_of=date(2026, 8, 8),
            lot_level=True,
            dividends_chf=Decimal("50"),
            expenses_chf=Decimal("20"),
            withdrawals_chf=Decimal("100"),
            contributed_capital_chf=Decimal("1000"),
        )

        result = project_portfolio(
            snapshot,
            {"NVDA": Decimal("100")},
            Decimal("1"),
            date(2026, 8, 8),
        )

        assert result.total_value_chf == Decimal("1250.00")
        assert result.invested_chf == Decimal("1000.00")
        assert result.total_gain_chf == Decimal("250.00")
        assert result.dividends_chf == Decimal("50")
        assert result.expenses_chf == Decimal("20")

    def test_weight_sums_close_to_one_across_positions(self):
        holdings = [
            Holding(ticker="NVDA", quantity=Decimal("10")),
            Holding(ticker="AMD", quantity=Decimal("20")),
        ]
        snapshot = make_snapshot(holdings, cash_chf="0")
        result = project_portfolio(
            snapshot, {"NVDA": Decimal("100"), "AMD": Decimal("50")}, Decimal("1"), date(2026, 8, 8)
        )
        total_weight = sum((p.weight for p in result.positions if p.weight is not None), Decimal(0))
        assert abs(total_weight - Decimal(1)) < Decimal("0.0001")


class TestGracefulDegradation:
    def test_missing_price_degrades_market_value_not_dropped(self):
        holding = Holding(ticker="NVDA", quantity=Decimal("10"))
        snapshot = make_snapshot([holding], cash_chf="1000")
        result = project_portfolio(snapshot, {}, Decimal("0.88"), date(2026, 8, 8))
        position = result.positions[0]
        assert position.market_value_usd is None
        assert position.market_value_chf is None
        assert "market_value" in position.degraded_fields
        assert position.quantity == Decimal("10")  # position is never dropped

    def test_missing_cost_basis_degrades_unrealised_only(self):
        holding = Holding(ticker="NVDA", quantity=Decimal("10"))  # no cost_basis_chf
        snapshot = make_snapshot([holding])
        result = project_portfolio(snapshot, {"NVDA": Decimal("100")}, Decimal("0.88"), date(2026, 8, 8))
        position = result.positions[0]
        assert position.market_value_chf is not None  # still computed
        assert position.cost_basis_chf is None
        assert position.unrealised_chf is None
        assert "cost_basis_chf" in position.degraded_fields
        assert "unrealised_chf" in position.degraded_fields

    def test_missing_open_date_degrades_holding_period_only(self):
        holding = Holding(ticker="NVDA", quantity=Decimal("10"), cost_basis_chf=Decimal("900"))
        snapshot = make_snapshot([holding])
        result = project_portfolio(snapshot, {"NVDA": Decimal("100")}, Decimal("0.88"), date(2026, 8, 8))
        position = result.positions[0]
        assert position.holding_period_days is None
        assert "holding_period_days" in position.degraded_fields
        # unrelated fields still computed
        assert position.market_value_chf is not None

    def test_missing_fx_rate_at_open_degrades_fx_effect_only(self):
        holding = Holding(
            ticker="NVDA", quantity=Decimal("10"), cost_basis_usd=Decimal("900"), cost_basis_chf=Decimal("800")
        )
        snapshot = make_snapshot([holding])
        result = project_portfolio(snapshot, {"NVDA": Decimal("100")}, Decimal("0.88"), date(2026, 8, 8))
        position = result.positions[0]
        assert position.fx_effect_chf is None
        assert "fx_effect_chf" in position.degraded_fields

    def test_top_level_degraded_fields_is_union_of_positions(self):
        holdings = [
            Holding(ticker="NVDA", quantity=Decimal("10")),  # missing cost basis
            Holding(ticker="AMD", quantity=Decimal("5")),  # missing price below
        ]
        snapshot = make_snapshot(holdings)
        result = project_portfolio(snapshot, {"NVDA": Decimal("100")}, Decimal("0.88"), date(2026, 8, 8))
        assert "market_value" in result.degraded_fields  # from AMD (no price)
        assert "cost_basis_chf" in result.degraded_fields  # from both


class TestAggregation:
    def test_multiple_lots_of_same_ticker_aggregate_into_one_position(self):
        holdings = [
            Holding(ticker="NVDA", quantity=Decimal("10"), lot_id="lot-1", cost_basis_chf=Decimal("800")),
            Holding(ticker="NVDA", quantity=Decimal("5"), lot_id="lot-2", cost_basis_chf=Decimal("500")),
        ]
        snapshot = make_snapshot(holdings)
        result = project_portfolio(snapshot, {"NVDA": Decimal("100")}, Decimal("1"), date(2026, 8, 8))
        assert len(result.positions) == 1
        assert result.positions[0].quantity == Decimal("15")
        assert result.positions[0].cost_basis_chf == Decimal("1300")

    def test_lot_level_flag_passed_through(self):
        snapshot = PortfolioSnapshot(
            holdings=[Holding(ticker="NVDA", quantity=Decimal("1"))],
            cash_chf=Decimal("0"),
            as_of=date(2026, 8, 8),
            lot_level=False,
        )
        result = project_portfolio(snapshot, {}, Decimal("1"), date(2026, 8, 8))
        assert result.lot_level is False


class TestNeverWritesSource:
    def test_projection_is_a_pure_function_of_its_inputs(self):
        """Sanity check that project_portfolio takes no I/O-capable dependency —
        it is a pure function over already-fetched data (arc42 §5.7)."""

        import inspect

        sig = inspect.signature(project_portfolio)
        param_names = set(sig.parameters)
        assert param_names == {"snapshot", "prices_usd", "fx_rate_chf_per_usd", "as_of"}
