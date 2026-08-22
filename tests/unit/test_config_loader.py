"""Unit tests for config loading (arc42 §5.2 universe, §5.11 config_versions)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from auspex.config.loader import (
    ConfigValidationError,
    _validate_fpi_redistribution,
    build_config_version,
    load_universe,
    load_weights,
)
from auspex.models.common import utc_now
from auspex.models.enums import FilerProfile


class TestUniverseLoading:
    def test_universe_has_104_securities(self):
        universe = load_universe()
        assert len(universe.securities) == 104

    def test_all_securities_investable(self):
        universe = load_universe()
        assert all(s.investable for s in universe.securities)

    def test_ticker_lookup_works(self):
        universe = load_universe()
        by_ticker = universe.by_ticker()
        assert "NVDA" in by_ticker
        assert by_ticker["NVDA"].cohort == "semi-compute"

    def test_ids_are_stable_across_reloads(self):
        universe1 = load_universe()
        universe2 = load_universe()
        assert universe1.by_ticker()["NVDA"].id == universe2.by_ticker()["NVDA"].id

    def test_foreign_private_issuers_match_universe_yaml(self):
        # Verified from the live EDGAR bulk submissions history at bootstrap.
        universe = load_universe()
        fpi = [s for s in universe.securities if s.filer_profile == FilerProfile.FPI]
        fpi_tickers = {s.ticker for s in fpi}
        assert fpi_tickers == {
            "ARM",
            "ASML",
            "CAMT",
            "NVMI",
            "POET",
            "ARQQ",
            "LAES",
            "SAP",
        }

    def test_eight_cohorts_with_correct_sizes(self):
        universe = load_universe()
        expected_sizes = {
            "semi-compute": 15,
            "semi-analog-mixed": 12,
            "semi-cap-equipment": 15,
            "optical-networking": 12,
            "datacenter-power-cooling": 13,
            "ai-software-platforms": 13,
            "emerging-ai-compute": 12,
            "large-cap-digital-platforms": 12,
        }
        for cohort, expected_size in expected_sizes.items():
            assert len(universe.cohort_members(cohort)) == expected_size, cohort

    def test_all_ciks_are_ten_digit_zero_padded(self):
        universe = load_universe()
        for sec in universe.securities:
            assert len(sec.cik) == 10
            assert sec.cik.isdigit()

    def test_no_duplicate_tickers(self):
        universe = load_universe()
        tickers = [s.ticker for s in universe.securities]
        assert len(tickers) == len(set(tickers))


class TestWeightsLoading:
    def test_domestic_weights_sum_to_one(self):
        weights = load_weights()
        total = sum(Decimal(v) for v in weights["domestic"].values())
        assert total == Decimal("1.00")

    def test_fpi_weights_sum_to_one(self):
        weights = load_weights()
        total = sum(Decimal(v) for v in weights["fpi"].values())
        assert total == Decimal("1.0000")

    def test_fpi_has_no_smart_money_weight(self):
        weights = load_weights()
        assert "smart_money" not in weights["fpi"]

    def test_committed_fpi_weights_are_the_proportional_redistribution(self):
        weights = load_weights()
        surviving = Decimal(1) - Decimal(weights["domestic"]["smart_money"])
        for leg, configured in weights["fpi"].items():
            expected = Decimal(weights["domestic"][leg]) / surviving
            assert Decimal(configured) == expected.quantize(Decimal(configured))


class TestFpiRedistributionValidation:
    """arc42 §5.2: an FPI files no Form 4, so ``smart_money``'s weight is
    redistributed across the other five legs *in proportion*.

    ``config/weights.yaml`` has always claimed the loader enforces this. It did
    not, so a future edit could have scored foreign private issuers on a
    quietly different model to their domestic peers while both rows kept citing
    the same ``config_version_id``.
    """

    BASE = {
        "domestic": {
            "thesis_linkage": "0.20",
            "attention_acceleration": "0.15",
            "narrative_premium": "0.10",
            "smart_money": "0.20",
            "fundamental_health": "0.20",
            "valuation_brake": "0.15",
        },
        "fpi": {
            "thesis_linkage": "0.25",
            "attention_acceleration": "0.1875",
            "narrative_premium": "0.125",
            "fundamental_health": "0.25",
            "valuation_brake": "0.1875",
        },
    }

    def _weights(self, **fpi_overrides) -> dict:
        weights = {"domestic": dict(self.BASE["domestic"]), "fpi": dict(self.BASE["fpi"])}
        weights["fpi"].update(fpi_overrides)
        return weights

    def test_the_correct_redistribution_is_accepted(self):
        _validate_fpi_redistribution(self._weights())

    def test_a_disproportionate_weight_is_rejected(self):
        # Both still sum to 1.0 — only the *proportions* are wrong, which a
        # sum-to-one check would happily wave through.
        weights = self._weights(thesis_linkage="0.30", fundamental_health="0.20")
        with pytest.raises(ConfigValidationError, match="proportional redistribution"):
            _validate_fpi_redistribution(weights)

    def test_a_smart_money_weight_on_an_fpi_is_rejected(self):
        weights = self._weights(smart_money="0.20")
        with pytest.raises(ConfigValidationError, match="smart_money"):
            _validate_fpi_redistribution(weights)

    def test_a_leg_missing_from_fpi_is_rejected(self):
        weights = self._weights()
        del weights["fpi"]["valuation_brake"]
        with pytest.raises(ConfigValidationError, match="valuation_brake"):
            _validate_fpi_redistribution(weights)

    def test_an_unknown_fpi_leg_is_rejected(self):
        weights = self._weights(momentum="0.10")
        with pytest.raises(ConfigValidationError, match="momentum"):
            _validate_fpi_redistribution(weights)

    def test_a_missing_section_is_rejected(self):
        with pytest.raises(ConfigValidationError):
            _validate_fpi_redistribution({"domestic": self.BASE["domestic"]})

    def test_documented_rounding_precision_is_honoured(self):
        """Values are committed to 4 dp; the check compares at the precision
        each value is written to rather than demanding infinite digits."""

        weights = {
            "domestic": {"a": "0.30", "b": "0.30", "smart_money": "0.40"},
            "fpi": {"a": "0.5", "b": "0.5"},
        }
        _validate_fpi_redistribution(weights)

    def test_redistributing_the_whole_weight_is_rejected(self):
        weights = {"domestic": {"a": "0.0", "smart_money": "1.0"}, "fpi": {"a": "1.0"}}
        with pytest.raises(ConfigValidationError, match="no weight to redistribute"):
            _validate_fpi_redistribution(weights)


class TestConfigVersion:
    def test_build_config_version_has_stable_fingerprint(self):
        now = utc_now()
        v1 = build_config_version("test-a", now)
        v2 = build_config_version("test-b", now)
        assert v1.fingerprint == v2.fingerprint  # same underlying config -> same fingerprint

    def test_fingerprint_is_sha256_prefixed(self):
        v = build_config_version("test-a", utc_now())
        assert v.fingerprint.startswith("sha256:")
