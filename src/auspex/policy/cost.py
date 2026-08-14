"""Estimated trade cost (arc42 §5.6 "Cost and outcome overlay").

Uses the configured broker fee schedule (``config/fees.yaml``), evaluated
through the Decimal-only currency AST so estimates are reproducible.
"""

from __future__ import annotations

from decimal import Decimal

from auspex.currency.ast import evaluate


def estimate_commission_usd(notional_usd: Decimal, fees_config: dict) -> Decimal:
    formula = fees_config["commission"]["formula"]
    return evaluate(formula, {"notional_usd": notional_usd})


def estimate_fx_conversion_spread_usd(notional_usd: Decimal, fees_config: dict) -> Decimal:
    formula = fees_config["fx_conversion_spread"]["formula"]
    return evaluate(formula, {"notional_usd": notional_usd})


def estimate_total_cost_usd(notional_usd: Decimal, fees_config: dict) -> Decimal:
    return estimate_commission_usd(notional_usd, fees_config) + estimate_fx_conversion_spread_usd(
        notional_usd, fees_config
    )
