from pathlib import Path
import unittest

from api.auspex_api.company_packages import (
    CompanyPackageNotFoundError,
    CompanyPackageService,
    InMemoryCompanyPackageRepository,
)


ROOT = Path(__file__).resolve().parents[1]


class FakeIdentity:
    def __init__(self):
        self.calls = []

    def product_user(self, principal):
        self.calls.append(principal)
        return object()


def package(security_sk, ticker, score, **overrides):
    values = {
        "id": "current",
        "document_type": "current",
        "package_fingerprint": f"fingerprint-{ticker}",
        "security_sk": security_sk,
        "ticker": ticker,
        "company_name": f"{ticker} Inc.",
        "as_of": "2026-08-07",
        "outlook_horizon_days": 90,
        "outlook_direction": "ACCELERATING",
        "theme_id": "theme",
        "classification_provenance": "curated_v1",
        "candidate_count": 3,
        "coverage_status": "READY",
        "coverage_reasons": [],
        "opportunity_score_raw": 0.5,
        "opportunity_score": score,
        "model_version": "company_opportunity_v1",
        "weight_version": "fresh_balanced_v1",
        "max_knowledge_date": "2026-08-07",
        "legs": [],
        "evidence": [],
        "narrative": {"summary": "Cited current outlook."},
    }
    values.update(overrides)
    return values


class CompanyPackagesApiTests(unittest.TestCase):
    def test_list_is_authenticated_ranked_and_excludes_history(self):
        identity = FakeIdentity()
        repository = InMemoryCompanyPackageRepository([
            package(1, "LOW", 20),
            package(2, "HIGH", 80),
            {**package(2, "HIGH", 70), "id": "package:history", "document_type": "revision"},
        ])
        service = CompanyPackageService(identity, repository)

        result = service.list_opportunities("principal", limit=20)

        self.assertEqual(identity.calls, ["principal"])
        self.assertEqual([row["ticker"] for row in result["opportunities"]], ["HIGH", "LOW"])
        self.assertTrue(all(row["research_only"] for row in result["opportunities"]))
        self.assertIn("narrative", result["opportunities"][0])

    def test_filters_and_limit_are_validated(self):
        service = CompanyPackageService(FakeIdentity(), InMemoryCompanyPackageRepository([
            package(1, "ONE", 80, theme_id="one", coverage_status="PARTIAL"),
            package(2, "TWO", 70, theme_id="two"),
        ]))

        result = service.list_opportunities(
            "principal", limit=1, theme_id="one", coverage_status="PARTIAL"
        )

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["opportunities"][0]["ticker"], "ONE")
        with self.assertRaisesRegex(ValueError, "limit"):
            service.list_opportunities("principal", limit=201)
        with self.assertRaisesRegex(ValueError, "coverage_status"):
            service.list_opportunities("principal", limit=10, coverage_status="BAD")

    def test_missing_current_package_is_not_found(self):
        service = CompanyPackageService(FakeIdentity(), InMemoryCompanyPackageRepository())

        with self.assertRaises(CompanyPackageNotFoundError):
            service.get_opportunity("principal", 42)

    def test_function_routes_and_container_setting_are_registered(self):
        function_app = (ROOT / "api" / "function_app.py").read_text(encoding="utf-8")
        bicep = (ROOT / "infra" / "modules" / "functionapp.bicep").read_text(encoding="utf-8")

        self.assertIn('route="opportunities"', function_app)
        self.assertIn('route="opportunities/{security_sk}"', function_app)
        self.assertIn("_identity_service()", function_app)
        self.assertIn("COMPANY_PACKAGES_CONTAINER", function_app)
        self.assertIn("name: 'COMPANY_PACKAGES_CONTAINER'", bicep)


if __name__ == "__main__":
    unittest.main()
