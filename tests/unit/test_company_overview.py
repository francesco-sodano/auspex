"""Current provider metrics and independently verified monetary units."""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from auspex.models.company_overview import CompanyOverviewSnapshot
from auspex.providers.company_overview import (
    OVERVIEW_METRIC_FIELDS,
    build_company_overview,
    needs_income_statement,
)

RETRIEVED_AT = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)
ASML_OVERVIEW = {
    "Symbol": "ASML",
    "Currency": "USD",
    "LatestQuarter": "2026-06-30",
    "RevenueTTM": "35327500000",
    "GrossProfitTTM": "18629200000",
    "QuarterlyRevenueGrowthYOY": "0.213",
    "QuarterlyEarningsGrowthYOY": "0.42",
    "ProfitMargin": "0.29",
    "OperatingMarginTTM": "0.33",
    "ReturnOnEquityTTM": "0.48",
    "ReturnOnAssetsTTM": "0.14",
    "PERatio": "57.81",
    "TrailingPE": "57.81",
    "EVToRevenue": "15.26",
    "EVToEBITDA": "43.12",
    "MarketCapitalization": "541000000000",
    "EPS": "24.10",
    "DilutedEPSTTM": "24.08",
}
ASML_INCOME_STATEMENT = {
    "symbol": "ASML",
    "quarterlyReports": [
        {
            "fiscalDateEnding": end,
            "reportedCurrency": "EUR",
            "totalRevenue": revenue,
            "grossProfit": gross,
        }
        for end, revenue, gross in (
            ("2026-06-30", "9326500000", "5035400000"),
            ("2026-03-31", "8766900000", "4645000000"),
            ("2025-12-31", "9718100000", "5068500000"),
            ("2025-09-30", "7516000000", "3880300000"),
        )
    ],
    "annualReports": [
        {
            "fiscalDateEnding": "2025-12-31",
            "reportedCurrency": "EUR",
            "totalRevenue": "32667300000",
            "grossProfit": "17258000000",
        }
    ],
}


@pytest.fixture
def overview() -> dict:
    return deepcopy(ASML_OVERVIEW)


@pytest.fixture
def statement() -> dict:
    return deepcopy(ASML_INCOME_STATEMENT)


@pytest.fixture
def consensus_data(overview, statement) -> tuple[dict, dict]:
    overview.update(Symbol="USCO", Currency="USD", RevenueTTM="1000", GrossProfitTTM="600")
    statement["symbol"] = "USCO"
    for row in statement["quarterlyReports"] + statement["annualReports"]:
        row.update(reportedCurrency="USD", totalRevenue="1", grossProfit="1")
    return overview, statement


def build(payload: dict, **kwargs) -> CompanyOverviewSnapshot:
    options = {"security_id": "sec-asml", "ticker": "ASML", "retrieved_at": RETRIEVED_AT}
    options.update(kwargs)
    return build_company_overview(payload, **options)


def assert_unverified(snapshot: CompanyOverviewSnapshot, reason: str) -> None:
    assert snapshot.financial_currency is None
    assert reason in snapshot.unavailable_fields["financial_currency"]
    for field in ("RevenueTTM", "GrossProfitTTM"):
        assert "financial currency unverified" in snapshot.unavailable_fields[field]
        assert reason in snapshot.unavailable_fields[field]
    assert snapshot.metrics["PERatio"] == "57.81"
    assert snapshot.metrics["EVToRevenue"] == "15.26"


def test_asml_uses_statement_eur_not_overview_usd(overview, statement):
    snapshot = build(overview, income_statement=statement)

    assert snapshot.id == snapshot.security_id == snapshot.partition_key == "sec-asml"
    assert snapshot.provider == "alpha_vantage"
    assert snapshot.ticker == "ASML"
    assert snapshot.quote_currency == "USD"
    assert snapshot.financial_currency == "EUR"
    assert snapshot.latest_quarter == date(2026, 6, 30)
    assert snapshot.retrieved_at == RETRIEVED_AT
    assert snapshot.metrics["RevenueTTM"] == "35327500000"
    assert snapshot.metrics["GrossProfitTTM"] == "18629200000"
    assert snapshot.metrics["PERatio"] == "57.81"
    assert snapshot.metrics["EVToRevenue"] == "15.26"
    assert snapshot.metrics["QuarterlyRevenueGrowthYOY"] == "0.213"
    assert snapshot.unavailable_fields == {}


def test_currency_consensus_allows_different_totals_without_replacing_provider_values(consensus_data):
    payload, statement = consensus_data

    snapshot = build(payload, ticker="USCO", income_statement=statement)

    assert snapshot.quote_currency == snapshot.financial_currency == "USD"
    assert snapshot.metrics == {field: payload[field] for field in OVERVIEW_METRIC_FIELDS}
    assert snapshot.unavailable_fields == {}
    assert needs_income_statement(payload, snapshot) is False
    refreshed = build(payload, ticker="USCO", previous=snapshot)
    assert refreshed.financial_currency == "USD"
    assert refreshed.metrics == snapshot.metrics
    for field, value in (("RevenueTTM", "1001"), ("GrossProfitTTM", "601"), ("LatestQuarter", "2026-09-30")):
        assert needs_income_statement({**payload, field: value}, snapshot) is True


@pytest.mark.parametrize(
    ("symbol", "latest", "revenue", "quarterly", "annual"),
    [
        (
            "AMZN",
            "2026-06-30",
            "775680033000",
            [
                ("2026-06-30", "200606000000", "None"),
                ("2026-03-31", "181519000000", "USD"),
                ("2025-12-31", "213386000000", "USD"),
                ("2025-09-30", "180169000000", "USD"),
            ],
            [("2025-12-31", None, "USD")],
        ),
        (
            "ARQQ",
            "2026-03-31",
            "1086000",
            [
                ("2026-03-31", "623000", "None"),
                ("2025-09-30", "463000", "None"),
                ("2025-03-31", "67000", "USD"),
            ],
            [("2025-09-30", "530000", "USD")],
        ),
        (
            "LAES",
            "2026-06-30",
            None,
            [
                ("2026-06-30", None, "None"),
                ("2025-12-31", None, "None"),
                ("2025-06-30", None, "None"),
            ],
            [("2025-12-31", None, "USD")],
        ),
    ],
    ids=["AMZN", "ARQQ", "LAES"],
)
def test_live_currency_consensus_handles_missing_labels_and_semiannual_periods(
    symbol,
    latest,
    revenue,
    quarterly,
    annual,
):
    payload = {"Symbol": symbol, "Currency": "USD", "LatestQuarter": latest}
    if revenue is not None:
        payload["RevenueTTM"] = revenue
    statement = {
        "symbol": symbol,
        "quarterlyReports": [
            {"fiscalDateEnding": end, "totalRevenue": amount, "reportedCurrency": currency}
            for end, amount, currency in quarterly
        ],
        "annualReports": [
            {"fiscalDateEnding": end, "totalRevenue": amount, "reportedCurrency": currency}
            for end, amount, currency in annual
        ],
    }

    snapshot = build(payload, security_id=f"sec-{symbol}", ticker=symbol, income_statement=statement)

    assert snapshot.quote_currency == snapshot.financial_currency == "USD"
    assert snapshot.latest_quarter == date.fromisoformat(latest)
    assert snapshot.metrics["RevenueTTM"] == revenue
    assert snapshot.metrics["GrossProfitTTM"] is None
    assert "financial_currency" not in snapshot.unavailable_fields
    if revenue is not None:
        assert "RevenueTTM" not in snapshot.unavailable_fields


@pytest.mark.parametrize("evidence", ["quarterlyReports", "annualReports"])
@pytest.mark.parametrize("missing", [None, "None", "N/A", "null", "", "-", "--", "not available"])
def test_currency_consensus_ignores_missing_labels_with_explicit_recent_backup(consensus_data, evidence, missing):
    payload, statement = consensus_data
    for row in statement["quarterlyReports"] + statement["annualReports"]:
        row["reportedCurrency"] = missing
    statement[evidence][1 if evidence == "quarterlyReports" else 0]["reportedCurrency"] = "USD"

    assert build(payload, ticker="USCO", income_statement=statement).financial_currency == "USD"


def test_currency_consensus_ignores_absent_currency_key_with_recent_backup(consensus_data):
    payload, statement = consensus_data
    del statement["quarterlyReports"][0]["reportedCurrency"]

    assert build(payload, ticker="USCO", income_statement=statement).financial_currency == "USD"


@pytest.mark.parametrize("quarterly", [None, [], "absent"])
def test_currency_consensus_accepts_recent_annual_evidence_without_quarterly_reports(consensus_data, quarterly):
    payload, statement = consensus_data
    if quarterly == "absent":
        del statement["quarterlyReports"]
    else:
        statement["quarterlyReports"] = quarterly

    assert build(payload, ticker="USCO", income_statement=statement).financial_currency == "USD"


@pytest.mark.parametrize("missing", [None, "None", "N/A", "", "null", "-", "--"])
def test_currency_consensus_never_uses_quote_or_root_currency_without_report_evidence(consensus_data, missing):
    payload, statement = consensus_data
    for row in statement["quarterlyReports"] + statement["annualReports"]:
        row["reportedCurrency"] = missing
    statement["Currency"] = statement["reportedCurrency"] = "USD"

    snapshot = build(payload, ticker="USCO", income_statement=statement)

    assert_unverified(snapshot, "no explicit reporting currency within 366 days")
    assert snapshot.metrics["RevenueTTM"] == "1000"
    assert snapshot.metrics["GrossProfitTTM"] == "600"
    assert needs_income_statement(payload, snapshot) is True


def test_currency_consensus_needs_report_evidence_even_when_collections_are_empty(consensus_data):
    payload, statement = consensus_data
    statement.update(quarterlyReports=[], annualReports=[])

    assert_unverified(
        build(payload, ticker="USCO", income_statement=statement),
        "no explicit reporting currency within 366 days",
    )


@pytest.mark.parametrize("evidence", ["quarterlyReports", "annualReports"])
@pytest.mark.parametrize(("age", "verified"), [(365, True), (366, True), (367, False), (730, False)])
def test_currency_consensus_has_an_inclusive_366_day_evidence_window(consensus_data, evidence, age, verified):
    payload, statement = consensus_data
    latest = date.fromisoformat(payload["LatestQuarter"])
    statement["quarterlyReports"] = [{"fiscalDateEnding": latest.isoformat(), "reportedCurrency": "None"}]
    statement["annualReports"] = []
    statement[evidence].append(
        {
            "fiscalDateEnding": (latest - timedelta(days=age)).isoformat(),
            "reportedCurrency": "USD",
        }
    )

    snapshot = build(payload, ticker="USCO", income_statement=statement)

    assert (snapshot.financial_currency == "USD") is verified
    if not verified:
        assert_unverified(snapshot, "no explicit reporting currency within 366 days")


@pytest.mark.parametrize("collection", ["quarterlyReports", "annualReports"])
def test_currency_consensus_rejects_recent_contradictory_reporting_currencies(consensus_data, collection):
    payload, statement = consensus_data
    statement[collection][0]["reportedCurrency"] = "EUR"

    assert_unverified(
        build(payload, ticker="USCO", income_statement=statement),
        "does not agree with quote currency",
    )


@pytest.mark.parametrize("collection", ["quarterlyReports", "annualReports"])
@pytest.mark.parametrize("label", ["US dollars", "usd", "US", "XXX", "XTS", "NaN", 1, True, [], {}, "secret-token"])
def test_currency_consensus_rejects_malformed_recent_labels(consensus_data, collection, label, caplog):
    payload, statement = consensus_data
    statement[collection][0]["reportedCurrency"] = label

    snapshot = build(payload, ticker="USCO", income_statement=statement)

    assert_unverified(snapshot, "malformed recent reportedCurrency")
    assert "secret-token" not in snapshot.model_dump_json()
    assert "secret-token" not in caplog.text


@pytest.mark.parametrize("collection", ["quarterlyReports", "annualReports"])
@pytest.mark.parametrize(
    ("end", "reason"),
    [
        ("2026-07-01", "future period after LatestQuarter"),
        ("2026-12-31", "future"),
        ("2026-02-30", "invalid fiscalDateEnding"),
        ("20260630", "invalid fiscalDateEnding"),
        ("secret-token", "invalid fiscalDateEnding"),
        (None, "missing"),
        ("None", "missing"),
        ({}, "invalid fiscalDateEnding"),
    ],
)
def test_currency_consensus_rejects_future_missing_or_invalid_statement_dates(
    consensus_data,
    collection,
    end,
    reason,
    caplog,
):
    payload, statement = consensus_data
    statement[collection][0]["fiscalDateEnding"] = end

    snapshot = build(payload, ticker="USCO", income_statement=statement)

    assert_unverified(snapshot, reason)
    assert "secret-token" not in snapshot.model_dump_json()
    assert "secret-token" not in caplog.text


@pytest.mark.parametrize("collection", ["quarterlyReports", "annualReports"])
@pytest.mark.parametrize("reports", [{}, "None", [None], [123], [True], ["bad-row"]])
def test_currency_consensus_rejects_malformed_statement_collections(consensus_data, collection, reports):
    payload, statement = consensus_data
    statement[collection] = reports

    assert_unverified(build(payload, ticker="USCO", income_statement=statement), "currency consensus rejected")


def test_currency_consensus_rejects_duplicate_periods_within_a_statement_collection(consensus_data):
    payload, statement = consensus_data
    statement["quarterlyReports"].append(deepcopy(statement["quarterlyReports"][0]))

    assert_unverified(build(payload, ticker="USCO", income_statement=statement), "duplicate")


@pytest.mark.parametrize("label", ["EUR", "malformed", None, {}])
def test_currency_consensus_ignores_labels_outside_the_evidence_window(consensus_data, label):
    payload, statement = consensus_data
    statement["annualReports"].append({"fiscalDateEnding": "2024-12-31", "reportedCurrency": label})

    assert build(payload, ticker="USCO", income_statement=statement).financial_currency == "USD"


@pytest.mark.parametrize("quote", [None, "", "None", "usd", "US dollars", "XXX", "XTS", True, {}])
def test_currency_consensus_requires_a_valid_quote_currency(consensus_data, quote):
    payload, statement = consensus_data
    payload["Currency"] = quote

    assert_unverified(
        build(payload, ticker="USCO", income_statement=statement),
        "consensus requires a valid quote currency",
    )


def test_currency_consensus_requires_same_issuer(consensus_data):
    payload, statement = consensus_data
    statement["symbol"] = "ANOTHER"

    assert_unverified(
        build(payload, ticker="USCO", income_statement=statement),
        "income statement symbol does not match",
    )


@pytest.mark.parametrize("currency", ["EUR", "CHF"])
def test_currency_consensus_also_requires_evidence_for_non_usd_quotes(consensus_data, currency):
    payload, statement = consensus_data
    payload["Currency"] = currency
    for row in statement["quarterlyReports"] + statement["annualReports"]:
        row["reportedCurrency"] = currency

    assert build(payload, ticker="USCO", income_statement=statement).financial_currency == currency


def test_live_sap_revenue_anchor_verifies_eur_without_certifying_gross_total():
    payload = {
        "Symbol": "SAP",
        "Currency": "USD",
        "LatestQuarter": "2026-06-30",
        "RevenueTTM": "38192001000",
        "GrossProfitTTM": "28132000000",
    }
    statement = {
        "symbol": "SAP",
        "quarterlyReports": [
            {
                "fiscalDateEnding": end,
                "reportedCurrency": "EUR",
                "totalRevenue": revenue,
                "grossProfit": gross,
            }
            for end, revenue, gross in (
                ("2026-06-30", "9878000000", "7228000000"),
                ("2026-03-31", "9555000000", "6973000000"),
                ("2025-12-31", "9684000000", "6916000000"),
                ("2025-09-30", "9076000000", "6671000000"),
            )
        ],
    }

    snapshot = build(payload, security_id="sec-sap", ticker="SAP", income_statement=statement)

    assert snapshot.quote_currency == "USD"
    assert snapshot.financial_currency == "EUR"
    assert snapshot.metrics["RevenueTTM"] == "38192001000"
    assert snapshot.metrics["GrossProfitTTM"] == "28132000000"
    assert "RevenueTTM" not in snapshot.unavailable_fields
    assert "GrossProfitTTM" not in snapshot.unavailable_fields
    assert needs_income_statement(payload, snapshot) is False
    cached = build(payload, security_id="sec-sap", ticker="SAP", previous=snapshot)
    assert cached.financial_currency == "EUR"
    assert cached.metrics == snapshot.metrics


@pytest.mark.parametrize("annual", [False, True])
@pytest.mark.parametrize("revenue", ["40000000000", "0"])
def test_valid_revenue_anchor_mismatch_cannot_fall_back_to_matching_gross(overview, statement, annual, revenue):
    if annual:
        overview.update(LatestQuarter="2025-12-31", GrossProfitTTM="17258000000")
        statement["quarterlyReports"] = []
    overview["RevenueTTM"] = revenue

    assert_unverified(build(overview, income_statement=statement), "totals do not match")


@pytest.mark.parametrize("annual", [False, True])
@pytest.mark.parametrize("revenue", [None, "N/A", "NaN", "not-a-number", "missing"])
def test_revenue_unavailable_falls_back_to_gross_without_changing_cache_rules(
    overview,
    statement,
    annual,
    revenue,
):
    if annual:
        overview.update(LatestQuarter="2025-12-31", GrossProfitTTM="17258000000")
        statement["quarterlyReports"] = []
    if revenue == "missing":
        del overview["RevenueTTM"]
    else:
        overview["RevenueTTM"] = revenue

    snapshot = build(overview, income_statement=statement)

    assert snapshot.quote_currency == "USD"
    assert snapshot.financial_currency == "EUR"
    assert snapshot.metrics["RevenueTTM"] is None
    assert snapshot.metrics["GrossProfitTTM"] == overview["GrossProfitTTM"]
    assert "RevenueTTM" in snapshot.unavailable_fields
    assert "GrossProfitTTM" not in snapshot.unavailable_fields
    assert "financial_currency" not in snapshot.unavailable_fields
    assert needs_income_statement(overview, snapshot) is True


@pytest.mark.parametrize("annual", [False, True])
@pytest.mark.parametrize("gross", ["1", None, "N/A", "NaN"])
def test_usable_revenue_anchor_does_not_require_statement_gross(overview, statement, annual, gross):
    if annual:
        overview.update(LatestQuarter="2025-12-31", RevenueTTM="32667300000", GrossProfitTTM="17258000000")
        statement["quarterlyReports"] = []
        statement["annualReports"][0]["grossProfit"] = gross
    else:
        statement["quarterlyReports"][0]["grossProfit"] = gross

    snapshot = build(overview, income_statement=statement)

    assert snapshot.financial_currency == "EUR"
    assert snapshot.metrics["GrossProfitTTM"] == overview["GrossProfitTTM"]
    assert "GrossProfitTTM" not in snapshot.unavailable_fields


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("reportedCurrency", "USD", "consistent reportedCurrency"),
        ("reportedCurrency", None, "consistent reportedCurrency"),
        ("fiscalDateEnding", "2026-06-29", "ending at LatestQuarter"),
        ("fiscalDateEnding", "2025-12-31", "duplicate"),
        ("grossProfit", "1", "totals do not match"),
        ("grossProfit", None, "finite and available"),
        ("grossProfit", "NaN", "finite and available"),
    ],
)
def test_gross_fallback_requires_matching_totals_period_and_currency(overview, statement, field, value, reason):
    overview["RevenueTTM"] = None
    statement["quarterlyReports"][0][field] = value

    assert_unverified(build(overview, income_statement=statement), reason)


@pytest.mark.parametrize(("delta", "verified"), [("-0.1", True), ("0.1", True), ("0.1001", False)])
def test_gross_fallback_keeps_the_same_rounding_tolerance(overview, statement, delta, verified):
    overview.update(RevenueTTM=None, GrossProfitTTM="1000")
    for row in statement["quarterlyReports"]:
        row["grossProfit"] = "250"
    statement["quarterlyReports"][0]["grossProfit"] = str(Decimal("250") + Decimal(delta))

    snapshot = build(overview, income_statement=statement)

    assert (snapshot.financial_currency == "EUR") is verified
    assert snapshot.metrics["RevenueTTM"] is None
    assert snapshot.metrics["GrossProfitTTM"] == "1000"


def test_missing_both_monetary_anchors_cannot_infer_currency(overview, statement):
    overview.update(RevenueTTM=None, GrossProfitTTM=None)

    assert_unverified(
        build(overview, income_statement=statement),
        "RevenueTTM or GrossProfitTTM is required",
    )


def test_supported_metric_allowlist_preserves_provider_values(overview, statement):
    expected = (
        "RevenueTTM",
        "GrossProfitTTM",
        "QuarterlyRevenueGrowthYOY",
        "QuarterlyEarningsGrowthYOY",
        "ProfitMargin",
        "OperatingMarginTTM",
        "ReturnOnEquityTTM",
        "ReturnOnAssetsTTM",
        "PERatio",
        "TrailingPE",
        "EVToRevenue",
        "EVToEBITDA",
        "MarketCapitalization",
        "EPS",
        "DilutedEPSTTM",
    )
    overview["unexpected_metric"] = {"not": "stored"}
    snapshot = build(overview, income_statement=statement)

    assert OVERVIEW_METRIC_FIELDS == expected
    assert tuple(snapshot.metrics) == expected
    assert snapshot.metrics == {field: ASML_OVERVIEW[field] for field in expected}
    assert "unexpected_metric" not in snapshot.model_dump_json()
    assert "available_at" not in snapshot.model_dump()
    assert "filed" not in snapshot.model_dump()


def test_ordinary_usd_issuer_still_requires_currency_proof(overview, statement):
    overview.update(Symbol="USCO", RevenueTTM="1000", GrossProfitTTM="600")
    statement["symbol"] = "USCO"
    for row in statement["quarterlyReports"]:
        row.update(reportedCurrency="USD", totalRevenue="250", grossProfit="150")

    unverified = build(overview, ticker="USCO")
    assert unverified.quote_currency == "USD"
    assert unverified.financial_currency is None
    snapshot = build(overview, ticker="USCO", income_statement=statement)
    assert snapshot.quote_currency == snapshot.financial_currency == "USD"
    assert snapshot.metrics["RevenueTTM"] == "1000"
    assert snapshot.metrics["GrossProfitTTM"] == "600"


def test_no_statement_keeps_provider_values_but_does_not_guess_units(overview):
    snapshot = build(overview)

    assert_unverified(snapshot, "income statement verification is unavailable")
    assert snapshot.quote_currency == "USD"
    assert snapshot.metrics["RevenueTTM"] == "35327500000"
    assert snapshot.metrics["GrossProfitTTM"] == "18629200000"
    assert needs_income_statement(overview, None)
    assert needs_income_statement(overview, snapshot)


def test_full_annual_report_can_prove_ttm_only_at_that_year_end(overview, statement):
    overview.update(LatestQuarter="2025-12-31", RevenueTTM="32667300000", GrossProfitTTM="17258000000")
    statement["quarterlyReports"] = []

    snapshot = build(overview, income_statement=statement)

    assert snapshot.financial_currency == "EUR"
    assert snapshot.latest_quarter == date(2025, 12, 31)
    assert snapshot.metrics["RevenueTTM"] == "32667300000"


def test_previous_fiscal_year_is_not_evidence_for_current_ttm(overview, statement):
    statement["quarterlyReports"] = []
    overview.update(RevenueTTM="32667300000", GrossProfitTTM="17258000000")

    assert_unverified(build(overview, income_statement=statement), "no full annual report ends at LatestQuarter")


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("reportedCurrency", "None", "consistent reportedCurrency"),
        ("reportedCurrency", "US dollars", "consistent reportedCurrency"),
        ("totalRevenue", "33667300000", "totals do not match"),
        ("fiscalDateEnding", "2026-12-31", "future"),
    ],
)
def test_invalid_annual_evidence_is_not_used(overview, statement, field, value, reason):
    overview.update(LatestQuarter="2025-12-31", RevenueTTM="32667300000", GrossProfitTTM="17258000000")
    statement["quarterlyReports"] = []
    statement["annualReports"][0][field] = value

    assert_unverified(build(overview, income_statement=statement), reason)


def test_conflicting_valid_annual_and_quarterly_units_are_rejected(overview, statement):
    statement["annualReports"] = [
        {
            "fiscalDateEnding": "2026-06-30",
            "reportedCurrency": "USD",
            "totalRevenue": overview["RevenueTTM"],
            "grossProfit": overview["GrossProfitTTM"],
        }
    ]

    assert_unverified(build(overview, income_statement=statement), "currency proofs disagree")


def test_quarterly_evidence_can_be_unsorted_and_contain_older_currencies(overview, statement):
    older = {
        "fiscalDateEnding": "2025-06-30",
        "reportedCurrency": "USD",
        "totalRevenue": "1",
        "grossProfit": "1",
    }
    statement["quarterlyReports"] = [older, *reversed(statement["quarterlyReports"])]

    assert build(overview, income_statement=statement).financial_currency == "EUR"


def test_52_53_week_quarters_are_successive(overview, statement):
    overview["LatestQuarter"] = "2026-07-04"
    for row, end in zip(
        statement["quarterlyReports"],
        ("2026-07-04", "2026-03-28", "2025-12-27", "2025-09-27"),
        strict=True,
    ):
        row["fiscalDateEnding"] = end

    assert build(overview, income_statement=statement).financial_currency == "EUR"


@pytest.mark.parametrize(
    ("index", "field", "value", "reason"),
    [
        (0, "reportedCurrency", "USD", "consistent reportedCurrency"),
        (0, "reportedCurrency", None, "consistent reportedCurrency"),
        (0, "reportedCurrency", "eur", "consistent reportedCurrency"),
        (0, "reportedCurrency", "XXX", "consistent reportedCurrency"),
        (0, "fiscalDateEnding", "2026-06-29", "ending at LatestQuarter"),
        (3, "fiscalDateEnding", "2025-06-30", "successive fiscal quarters"),
        (3, "fiscalDateEnding", "2025-12-31", "duplicate"),
        (0, "fiscalDateEnding", "2026-12-31", "future"),
        (0, "fiscalDateEnding", "not-a-date", "invalid fiscalDateEnding"),
        (0, "fiscalDateEnding", None, "missing"),
        (0, "totalRevenue", "932650000", "totals do not match"),
        (0, "totalRevenue", "NaN", "finite and available"),
        (0, "totalRevenue", None, "finite and available"),
    ],
)
def test_invalid_quarter_evidence_keeps_units_unknown(overview, statement, index, field, value, reason):
    statement["quarterlyReports"][index][field] = value

    assert_unverified(build(overview, income_statement=statement), reason)


@pytest.mark.parametrize("count", range(4))
def test_incomplete_quarterly_history_does_not_prove_ttm(overview, statement, count):
    statement["quarterlyReports"] = statement["quarterlyReports"][:count]

    assert build(overview, income_statement=statement).financial_currency is None


@pytest.mark.parametrize("reports", [None, {}, "invalid", [None], ["bad-row"]])
def test_malformed_report_collections_do_not_prove_currency(overview, statement, reports):
    statement["quarterlyReports"] = reports
    statement["annualReports"] = reports

    assert_unverified(build(overview, income_statement=statement), "report")


@pytest.mark.parametrize(
    "statement",
    [
        {},
        [],
        {"Note": "private-apikey"},
        {"Information": "private-apikey"},
        {"Error Message": "private-apikey"},
        {"symbol": "MSFT", "quarterlyReports": ASML_INCOME_STATEMENT["quarterlyReports"]},
        {"quarterlyReports": ASML_INCOME_STATEMENT["quarterlyReports"]},
    ],
)
def test_invalid_statement_envelope_leaves_ratios_usable(overview, statement, caplog):
    snapshot = build(overview, income_statement=statement)

    assert snapshot.financial_currency is None
    assert snapshot.metrics["PERatio"] == "57.81"
    assert "private-apikey" not in snapshot.model_dump_json()
    assert "private-apikey" not in caplog.text
    for field in ("RevenueTTM", "GrossProfitTTM"):
        assert "financial currency unverified" in snapshot.unavailable_fields[field]


def test_rounding_tolerance_never_replaces_provider_ttm(overview, statement):
    overview["RevenueTTM"] = "35328000000"

    snapshot = build(overview, income_statement=statement)

    assert snapshot.financial_currency == "EUR"
    assert snapshot.metrics["RevenueTTM"] == "35328000000"


@pytest.mark.parametrize(("delta", "verified"), [("-0.1", True), ("0.1", True), ("-0.1001", False)])
def test_rounding_tolerance_is_relative_one_in_ten_thousand(overview, statement, delta, verified):
    overview.update(RevenueTTM="1000", GrossProfitTTM="600")
    for row in statement["quarterlyReports"]:
        row.update(totalRevenue="250", grossProfit="150")
    statement["quarterlyReports"][0]["totalRevenue"] = str(Decimal("250") + Decimal(delta))

    snapshot = build(overview, income_statement=statement)

    assert (snapshot.financial_currency == "EUR") is verified
    assert snapshot.metrics["RevenueTTM"] == "1000"


@pytest.mark.parametrize(("quarter_revenue", "verified"), [("0", True), ("0.0000001", False)])
def test_zero_totals_require_exact_zero(overview, statement, quarter_revenue, verified):
    overview.update(RevenueTTM="0", GrossProfitTTM="0")
    for row in statement["quarterlyReports"]:
        row.update(totalRevenue=quarter_revenue, grossProfit="0")

    snapshot = build(overview, income_statement=statement)

    assert (snapshot.financial_currency == "EUR") is verified
    assert snapshot.metrics["RevenueTTM"] == snapshot.metrics["GrossProfitTTM"] == "0"


def test_decimal_cancellation_cannot_falsely_verify_zero(overview, statement):
    overview.update(RevenueTTM="0", GrossProfitTTM="0")
    for row, amount in zip(statement["quarterlyReports"], ("1E+100", "1", "-1E+100", "0"), strict=True):
        row.update(totalRevenue=amount, grossProfit="0")

    assert_unverified(build(overview, income_statement=statement), "totals do not match")


def test_extreme_numeric_span_bounds_currency_comparison_work(overview, statement):
    overview["RevenueTTM"] = "1E+5000"
    statement["quarterlyReports"][0]["totalRevenue"] = "1E+5000"

    snapshot = build(overview, income_statement=statement)

    assert snapshot.metrics["RevenueTTM"] == "1E+5000"
    assert_unverified(snapshot, "supported decimal precision")


def test_negative_gross_profit_can_have_verified_units(overview, statement):
    overview.update(RevenueTTM="1000", GrossProfitTTM="-20")
    for row in statement["quarterlyReports"]:
        row.update(totalRevenue="250", grossProfit="-5")

    snapshot = build(overview, income_statement=statement)

    assert snapshot.financial_currency == "EUR"
    assert snapshot.metrics["GrossProfitTTM"] == "-20"


def test_verified_currency_cache_reused_while_daily_metrics_refresh(overview, statement):
    previous = build(overview, income_statement=statement)
    original = previous.model_dump()
    overview.update(PERatio="60.01", EVToRevenue="16", Currency="EUR")

    assert needs_income_statement(overview, previous) is False
    snapshot = build(overview, previous=previous, retrieved_at=RETRIEVED_AT + timedelta(days=1))

    assert snapshot.id == previous.id
    assert snapshot.financial_currency == "EUR"
    assert snapshot.quote_currency == "EUR"
    assert snapshot.metrics["PERatio"] == "60.01"
    assert snapshot.metrics["EVToRevenue"] == "16"
    assert snapshot.retrieved_at > previous.retrieved_at
    assert snapshot.unavailable_fields == {}
    assert previous.model_dump() == original


def test_currency_cache_survives_json_roundtrip_and_equivalent_decimal_formatting(overview, statement):
    previous = CompanyOverviewSnapshot.model_validate_json(
        build(overview, income_statement=statement).model_dump_json()
    )
    overview.update(RevenueTTM="3.5327500000E10", GrossProfitTTM="18629200000.0")

    assert needs_income_statement(overview, previous) is False
    assert build(overview, previous=previous).financial_currency == "EUR"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("LatestQuarter", "2026-09-30"),
        ("LatestQuarter", None),
        ("RevenueTTM", "35327500001"),
        ("GrossProfitTTM", "18629200001"),
        ("RevenueTTM", None),
        ("GrossProfitTTM", "NaN"),
        ("Symbol", "MSFT"),
    ],
)
def test_changed_quarter_amount_or_symbol_requires_new_proof(overview, statement, field, value):
    previous = build(overview, income_statement=statement)
    overview[field] = value

    assert needs_income_statement(overview, previous) is True
    snapshot = build(
        overview,
        ticker=overview["Symbol"],
        previous=previous,
        retrieved_at=datetime(2026, 10, 20, tzinfo=UTC),
    )
    assert snapshot.financial_currency is None


def test_same_ticker_in_wrong_security_partition_cannot_reuse_cache(overview, statement):
    previous = build(overview, income_statement=statement)

    assert build(overview, security_id="another-security", previous=previous).financial_currency is None


def test_previous_unavailability_reason_prevents_currency_reuse(overview, statement):
    previous = build(overview, income_statement=statement)
    previous.unavailable_fields["RevenueTTM"] = "currency evidence was withdrawn"

    assert needs_income_statement(overview, previous) is True
    assert build(overview, previous=previous).financial_currency is None


def test_explicit_bad_statement_is_not_hidden_by_reusable_cache(overview, statement):
    previous = build(overview, income_statement=statement)
    statement["symbol"] = "MSFT"

    assert_unverified(
        build(overview, income_statement=statement, previous=previous),
        "symbol does not match",
    )


@pytest.mark.parametrize("field", OVERVIEW_METRIC_FIELDS)
@pytest.mark.parametrize("value", [None, "", " None ", "N/A", "NA", "null", "-", "--"])
def test_missing_numeric_values_have_explicit_reasons(overview, field, value):
    overview[field] = value

    snapshot = build(overview)

    assert snapshot.metrics[field] is None
    assert "not supplied" in snapshot.unavailable_fields[field]


def test_missing_all_optional_source_fields_is_not_zero_filled():
    snapshot = build({"Symbol": "ASML"})

    assert set(snapshot.metrics) == set(OVERVIEW_METRIC_FIELDS)
    assert all(value is None for value in snapshot.metrics.values())
    assert all(field in snapshot.unavailable_fields for field in OVERVIEW_METRIC_FIELDS)
    assert snapshot.quote_currency is snapshot.financial_currency is snapshot.latest_quarter is None
    assert "Currency" in snapshot.unavailable_fields
    assert "LatestQuarter" in snapshot.unavailable_fields


def test_missing_metrics_are_not_derived_from_available_metrics(overview, statement):
    for field in ("GrossProfitTTM", "EPS", "PERatio"):
        del overview[field]

    snapshot = build(overview, income_statement=statement)

    assert snapshot.metrics["GrossProfitTTM"] is None
    assert snapshot.metrics["EPS"] is None
    assert snapshot.metrics["PERatio"] is None
    assert snapshot.metrics["DilutedEPSTTM"] == "24.08"
    assert snapshot.metrics["TrailingPE"] == "57.81"
    assert snapshot.financial_currency == "EUR"
    assert snapshot.unavailable_fields["GrossProfitTTM"] == "not supplied by Alpha Vantage"
    assert "financial_currency" not in snapshot.unavailable_fields


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity", float("nan"), float("inf")])
def test_nonfinite_metrics_are_null_and_warn_without_serializing_floats(overview, value, caplog):
    overview["ProfitMargin"] = value
    with caplog.at_level(logging.WARNING, logger="auspex.providers.company_overview"):
        snapshot = build(overview)

    assert snapshot.metrics["ProfitMargin"] is None
    assert "non-finite" in snapshot.unavailable_fields["ProfitMargin"]
    assert "ProfitMargin" in caplog.text
    assert "non-finite" in caplog.text
    assert json.loads(snapshot.model_dump_json())["metrics"]["ProfitMargin"] is None


@pytest.mark.parametrize("value", [True, False, [], {}, "1,000", "5%", "1_000", "bad?apikey=secret-token"])
def test_invalid_numeric_values_warn_without_logging_raw_values(overview, value, caplog):
    overview["ProfitMargin"] = value
    with caplog.at_level(logging.WARNING, logger="auspex.providers.company_overview"):
        snapshot = build(overview)

    assert snapshot.metrics["ProfitMargin"] is None
    assert snapshot.unavailable_fields["ProfitMargin"] == "invalid numeric value"
    assert "ProfitMargin" in caplog.text
    assert "invalid numeric" in caplog.text
    assert "secret-token" not in caplog.text
    assert "secret-token" not in snapshot.model_dump_json()


@pytest.mark.parametrize(
    "value",
    ["0", "-0", "-0.125", "1.2300", "1e-7", 0, 12, 0.213, Decimal("12345678901234567890.123456789")],
)
def test_finite_values_keep_numeric_meaning_and_are_stored_as_strings(overview, value):
    overview["QuarterlyRevenueGrowthYOY"] = value

    snapshot = build(overview)
    stored = snapshot.metrics["QuarterlyRevenueGrowthYOY"]

    assert isinstance(stored, str)
    assert Decimal(stored) == Decimal(str(value))
    assert "QuarterlyRevenueGrowthYOY" not in snapshot.unavailable_fields
    serialized = json.loads(snapshot.model_dump_json())["metrics"]
    assert all(number is None or isinstance(number, str) for number in serialized.values())


@pytest.mark.parametrize(
    "field",
    [
        "ProfitMargin",
        "OperatingMarginTTM",
        "ReturnOnEquityTTM",
        "ReturnOnAssetsTTM",
        "QuarterlyRevenueGrowthYOY",
        "QuarterlyEarningsGrowthYOY",
        "EPS",
        "DilutedEPSTTM",
        "EVToEBITDA",
    ],
)
def test_legitimate_negative_values_are_not_treated_as_missing(overview, field):
    overview[field] = "-0.25"

    snapshot = build(overview)

    assert snapshot.metrics[field] == "-0.25"
    assert field not in snapshot.unavailable_fields


@pytest.mark.parametrize("field", ["PERatio", "TrailingPE"])
@pytest.mark.parametrize("value", ["0", "-0", "-10", 0, -2])
def test_nonpositive_pe_is_not_meaningful(overview, field, value):
    overview[field] = value

    snapshot = build(overview)

    assert snapshot.metrics[field] is None
    assert snapshot.unavailable_fields[field] == "non-positive P/E is not meaningful"


@pytest.mark.parametrize("value", [None, "", "None", "US", "USDCHF", "GBp", "XXX", 1, {}])
def test_quote_currency_unknown_never_prevents_independent_financial_proof(overview, statement, value):
    overview["Currency"] = value

    snapshot = build(overview, income_statement=statement)

    assert snapshot.quote_currency is None
    assert "Currency" in snapshot.unavailable_fields
    assert snapshot.financial_currency == "EUR"


@pytest.mark.parametrize("value", [None, "", "N/A"])
def test_missing_latest_quarter_cannot_verify_units(overview, statement, value):
    overview["LatestQuarter"] = value

    snapshot = build(overview, income_statement=statement)

    assert snapshot.latest_quarter is None
    assert "LatestQuarter" in snapshot.unavailable_fields
    assert_unverified(snapshot, "LatestQuarter is unavailable")
    assert needs_income_statement(overview, None)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"Name": "ASML"},
        {"Symbol": ""},
        {"Symbol": None},
        {"Symbol": []},
        {"Symbol": "AS ML"},
        {"Error Message": "secret-token"},
        {"Note": "secret-token"},
        {"Information": "secret-token"},
        {**ASML_OVERVIEW, "Note": "secret-token"},
    ],
)
def test_invalid_overview_envelopes_are_rejected_without_leaking_payloads(payload, caplog):
    with pytest.raises(ValueError) as build_error:
        build(payload)
    with pytest.raises(ValueError) as needs_error:
        needs_income_statement(payload, None)

    assert "secret-token" not in str(build_error.value)
    assert "secret-token" not in str(needs_error.value)
    assert "secret-token" not in caplog.text


def test_wrong_ticker_is_rejected(overview):
    with pytest.raises(ValueError, match="symbol does not match"):
        build(overview, ticker="MSFT")


def test_tickers_normalize_to_uppercase_before_matching(overview, statement):
    overview["Symbol"] = "asml"
    statement["symbol"] = "asml"

    snapshot = build(overview, ticker=" asml ", income_statement=statement)

    assert snapshot.ticker == "ASML"
    assert snapshot.financial_currency == "EUR"


@pytest.mark.parametrize("value", ["20260630", "2026-W27-2", "2026-02-30", "secret-token", 123, {}])
def test_invalid_latest_quarter_is_rejected(overview, value):
    overview["LatestQuarter"] = value

    with pytest.raises(ValueError, match="LatestQuarter must be an ISO") as error:
        build(overview)
    assert "secret-token" not in str(error.value)


def test_future_latest_quarter_is_rejected(overview):
    with pytest.raises(ValueError, match="after the retrieval date"):
        build(overview, retrieved_at=datetime(2026, 6, 29, tzinfo=UTC))


def test_period_on_retrieval_date_is_allowed(overview):
    assert build(overview, retrieved_at=datetime(2026, 6, 30)).latest_quarter == date(2026, 6, 30)


def test_retrieval_requires_datetime(overview):
    with pytest.raises(ValueError, match="retrieved_at must be a datetime"):
        build(overview, retrieved_at="2026-09-20")


def test_inputs_are_not_mutated(overview, statement):
    original_overview, original_statement = deepcopy(overview), deepcopy(statement)

    build(overview, income_statement=statement)

    assert overview == original_overview
    assert statement == original_statement


@pytest.mark.parametrize("value", ["", " ", "a b", "a/b", r"a\b", "a?b", "a#b", "\x00id"])
def test_model_rejects_invalid_document_identifiers(overview, value):
    with pytest.raises(ValidationError, match="identifiers"):
        build(overview, security_id=value)


def test_model_requires_id_to_equal_security_id(overview):
    document = build(overview).model_dump()
    document["id"] = "another-id"

    with pytest.raises(ValidationError, match="id must equal security_id"):
        CompanyOverviewSnapshot.model_validate(document)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "garbage", 0.5, 1, True])
def test_model_itself_rejects_nonfinite_or_non_string_metrics(overview, value):
    document = build(overview).model_dump()
    document["metrics"]["ProfitMargin"] = value

    with pytest.raises(ValidationError):
        CompanyOverviewSnapshot.model_validate(document)


def test_model_itself_rejects_future_period(overview):
    document = build(overview).model_dump()
    document["latest_quarter"] = date(2026, 12, 31)

    with pytest.raises(ValidationError, match="after the retrieval date"):
        CompanyOverviewSnapshot.model_validate(document)


@pytest.mark.parametrize("field", ["id", "security_id", "ticker", "retrieved_at"])
def test_model_required_fields(overview, field):
    document = build(overview).model_dump()
    del document[field]

    with pytest.raises(ValidationError):
        CompanyOverviewSnapshot.model_validate(document)


@pytest.mark.parametrize(
    ("field", "value"),
    [("provider", "edgar"), ("financial_currency", "eur"), ("quote_currency", "US dollars")],
)
def test_model_rejects_wrong_provider_or_invalid_currency(overview, field, value):
    document = build(overview).model_dump()
    document[field] = value

    with pytest.raises(ValidationError):
        CompanyOverviewSnapshot.model_validate(document)


def test_unavailable_field_defaults_are_not_shared():
    first = CompanyOverviewSnapshot(id="a", security_id="a", ticker="A", retrieved_at=RETRIEVED_AT)
    second = CompanyOverviewSnapshot(id="b", security_id="b", ticker="B", retrieved_at=RETRIEVED_AT)
    first.unavailable_fields["EPS"] = "not supplied"

    assert second.unavailable_fields == {}
