"""Compact active-data windows for incremental company opportunity packages."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ActiveWindowPolicy:
    source_class: str
    lookback_days: int | None
    snapshot_count: int | None
    legs: tuple[str, ...]


ACTIVE_WINDOW_POLICIES = {
    "prices": ActiveWindowPolicy(
        source_class="prices",
        lookback_days=30,
        snapshot_count=None,
        legs=("valuation_brake",),
    ),
    "news": ActiveWindowPolicy(
        source_class="news",
        lookback_days=60,
        snapshot_count=None,
        legs=("attention_acceleration",),
    ),
    "insider_transactions": ActiveWindowPolicy(
        source_class="insider_transactions",
        lookback_days=90,
        snapshot_count=None,
        legs=("smart_money",),
    ),
    "contracts": ActiveWindowPolicy(
        source_class="contracts",
        lookback_days=90,
        snapshot_count=None,
        legs=("smart_money",),
    ),
    "ownership_events": ActiveWindowPolicy(
        source_class="ownership_events",
        lookback_days=90,
        snapshot_count=None,
        legs=("smart_money",),
    ),
    "institutional_holdings": ActiveWindowPolicy(
        source_class="institutional_holdings",
        lookback_days=None,
        snapshot_count=2,
        legs=("smart_money", "crowding_positioning"),
    ),
    "fundamentals": ActiveWindowPolicy(
        source_class="fundamentals",
        lookback_days=None,
        snapshot_count=8,
        legs=("fundamental_health", "valuation_brake"),
    ),
    "theme_holdings": ActiveWindowPolicy(
        source_class="theme_holdings",
        lookback_days=None,
        snapshot_count=1,
        legs=("thesis_linkage",),
    ),
    "broad_market_holdings": ActiveWindowPolicy(
        source_class="broad_market_holdings",
        lookback_days=None,
        snapshot_count=1,
        legs=("thesis_linkage",),
    ),
    "classification": ActiveWindowPolicy(
        source_class="classification",
        lookback_days=None,
        snapshot_count=1,
        legs=("thesis_linkage",),
    ),
    "material_events": ActiveWindowPolicy(
        source_class="material_events",
        lookback_days=90,
        snapshot_count=None,
        legs=("smart_money",),
    ),
}


def validate_active_window_policies() -> None:
    for source_class, policy in ACTIVE_WINDOW_POLICIES.items():
        if policy.source_class != source_class or not policy.legs:
            raise ValueError("active window policy identity is invalid")
        configured = sum(
            value is not None
            for value in (policy.lookback_days, policy.snapshot_count)
        )
        if configured != 1:
            raise ValueError("active window policy requires one window type")
        if policy.lookback_days is not None and policy.lookback_days < 1:
            raise ValueError("active lookback must be positive")
        if policy.snapshot_count is not None and policy.snapshot_count < 1:
            raise ValueError("active snapshot count must be positive")