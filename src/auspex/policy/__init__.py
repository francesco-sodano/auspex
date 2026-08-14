"""Deterministic gate cascade producing BUY/ADD/HOLD/TRIM/SELL (arc42 §5.6)."""

from __future__ import annotations

from auspex.policy.assertions import AssertionViolation, run_post_run_assertions
from auspex.policy.cost import estimate_commission_usd, estimate_fx_conversion_spread_usd, estimate_total_cost_usd
from auspex.policy.engine import PolicyThresholds, evaluate_action, load_policy_thresholds
from auspex.policy.gates import PolicyContext
from auspex.policy.target_weight import target_weight_pct

__all__ = [
    "AssertionViolation",
    "run_post_run_assertions",
    "estimate_commission_usd",
    "estimate_fx_conversion_spread_usd",
    "estimate_total_cost_usd",
    "PolicyThresholds",
    "evaluate_action",
    "load_policy_thresholds",
    "PolicyContext",
    "target_weight_pct",
]
