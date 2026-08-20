"""Five non-overlapping investment horizons and safe migration (arc42 §5.7).

The bands partition the timeline exactly once — no gap, no overlap — and
documents written before the split must still load without a data migration
job, because a `user_settings` document that fails validation would lock its
owner out of their own account.
"""

from __future__ import annotations

import pytest

from auspex.models.common import utc_now
from auspex.models.user_settings import (
    HORIZON_UPPER_BOUND_MONTHS,
    LEGACY_INVESTMENT_HORIZONS,
    InvestmentHorizon,
    UserSettings,
    migrate_investment_horizon,
)


def settings_document(horizon: str) -> dict:
    return {
        "id": "user-1",
        "user_id": "user-1",
        "risk_profile": "MODERATE",
        "cash_reserve_chf": "3000",
        "investment_horizon": horizon,
        "investment_objective": "CAPITAL_GROWTH",
        "directional_only_acknowledged": True,
        "no_guarantee_acknowledged": True,
        "not_financial_advice_acknowledged": True,
        "market_loss_acknowledged": True,
        "independent_decision_acknowledged": True,
        "acknowledgement_version": "2026-08-12",
        "acknowledged_at": None,
        "updated_at": utc_now().isoformat(),
    }


class TestBands:
    def test_there_are_exactly_five_bands(self):
        assert len(list(InvestmentHorizon)) == 5

    def test_bands_are_ordered_and_non_overlapping(self):
        bounds = [HORIZON_UPPER_BOUND_MONTHS[member] for member in InvestmentHorizon]
        finite = [bound for bound in bounds if bound is not None]

        # Strictly increasing upper bounds means each band starts exactly where
        # the previous one ends: no overlap, and no uncovered gap.
        assert finite == sorted(finite)
        assert len(set(finite)) == len(finite)
        assert bounds[-1] is None  # the final band is unbounded

    def test_expected_band_names(self):
        assert [member.value for member in InvestmentHorizon] == [
            "SIX_MONTHS",
            "ONE_YEAR",
            "ONE_TO_THREE_YEARS",
            "THREE_TO_SEVEN_YEARS",
            "OVER_SEVEN_YEARS",
        ]


class TestMigration:
    @pytest.mark.parametrize(
        ("legacy", "expected"),
        [
            ("SHORT_TERM", InvestmentHorizon.ONE_TO_THREE_YEARS),
            ("MEDIUM_TERM", InvestmentHorizon.THREE_TO_SEVEN_YEARS),
            ("LONG_TERM", InvestmentHorizon.OVER_SEVEN_YEARS),
        ],
    )
    def test_legacy_documents_load_into_the_new_bands(self, legacy, expected):
        settings = UserSettings.model_validate(settings_document(legacy))

        assert settings.investment_horizon is expected

    def test_migration_table_is_exhaustive_for_the_old_vocabulary(self):
        assert set(LEGACY_INVESTMENT_HORIZONS) == {"SHORT_TERM", "MEDIUM_TERM", "LONG_TERM"}

    def test_new_values_pass_through_untouched(self):
        for member in InvestmentHorizon:
            settings = UserSettings.model_validate(settings_document(member.value))
            assert settings.investment_horizon is member

    def test_unknown_values_still_fail_validation(self):
        """Migration must not become a silent catch-all for corrupt data."""

        with pytest.raises(ValueError):
            UserSettings.model_validate(settings_document("SOMETIME_MAYBE"))

    def test_helper_is_case_insensitive_and_type_preserving(self):
        assert migrate_investment_horizon("long_term") is InvestmentHorizon.OVER_SEVEN_YEARS
        assert migrate_investment_horizon(InvestmentHorizon.SIX_MONTHS) is InvestmentHorizon.SIX_MONTHS
        assert migrate_investment_horizon(None) is None

    def test_default_is_the_longest_band(self):
        document = settings_document("SIX_MONTHS")
        document.pop("investment_horizon")

        assert UserSettings.model_validate(document).investment_horizon is (
            InvestmentHorizon.OVER_SEVEN_YEARS
        )
