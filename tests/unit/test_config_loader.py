"""Unit tests for config loading (arc42 §5.2 universe, §5.11 config_versions)."""

from __future__ import annotations

from decimal import Decimal

from auspex.config.loader import build_config_version, load_universe, load_weights
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


class TestConfigVersion:
    def test_build_config_version_has_stable_fingerprint(self):
        now = utc_now()
        v1 = build_config_version("test-a", now)
        v2 = build_config_version("test-b", now)
        assert v1.fingerprint == v2.fingerprint  # same underlying config -> same fingerprint

    def test_fingerprint_is_sha256_prefixed(self):
        v = build_config_version("test-a", utc_now())
        assert v.fingerprint.startswith("sha256:")
