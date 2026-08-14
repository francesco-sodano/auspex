"""Suggestion hit rate and disposition outcome (arc42 §5.8).

- **Suggestion hit rate**: fraction of BUY suggestions outperforming the
  cohort median over 126 days.
- **Disposition outcome**: same, split by accepted vs. rejected, so the
  owner's overrides are measured too.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

OUTCOME_HORIZON_SESSIONS = 126
OUTCOME_MATURITY_CALENDAR_DAYS = 180


@dataclass(frozen=True)
class SuggestionOutcome:
    security_return_usd: Decimal
    cohort_median_return_usd: Decimal

    @property
    def outperformed(self) -> bool:
        return self.security_return_usd > self.cohort_median_return_usd


def suggestion_hit_rate(outcomes: list[SuggestionOutcome]) -> Decimal | None:
    if not outcomes:
        return None
    hits = sum(1 for o in outcomes if o.outperformed)
    return Decimal(hits) / Decimal(len(outcomes))


@dataclass(frozen=True)
class DispositionOutcome(SuggestionOutcome):
    accepted: bool = True


def disposition_hit_rate(outcomes: list[DispositionOutcome], *, accepted: bool) -> Decimal | None:
    subset = [o for o in outcomes if o.accepted == accepted]
    return suggestion_hit_rate(subset)
