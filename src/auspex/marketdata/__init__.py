"""Market-data integrity: diagnosis, idempotent repair and quarantine (arc42 §5.3).

Raw provider observations are immutable. This package only ever rewrites the
*derived* adjusted series, quarantines bars it cannot justify, and records an
auditable, versioned repair manifest.
"""

from __future__ import annotations

from auspex.marketdata.policy import DEFAULT_POLICY, POLICY_VERSION, IntegrityPolicy
from auspex.marketdata.quarantine import exclude_quarantined, is_quarantined
from auspex.marketdata.recompute import RecomputeTarget, targets_from_manifest
from auspex.marketdata.service import MarketDataIntegrityService

__all__ = [
    "DEFAULT_POLICY",
    "POLICY_VERSION",
    "IntegrityPolicy",
    "MarketDataIntegrityService",
    "RecomputeTarget",
    "exclude_quarantined",
    "is_quarantined",
    "targets_from_manifest",
]
