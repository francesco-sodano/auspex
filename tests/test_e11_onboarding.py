import base64
import datetime as dt
import json
from pathlib import Path
import unittest

from api.auspex_api.app_users import InMemoryAppUserRepository
from api.auspex_api.services import AuthorizationError, IdentityService


NOW = dt.datetime(2026, 7, 22, 11, 0, tzinfo=dt.timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def principal_header(user_id="user-1", roles=None, details="user@outlook.com"):
    payload = {
        "identityProvider": "aad",
        "userId": user_id,
        "userDetails": details,
        "userRoles": roles or ["anonymous", "authenticated"],
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def registration_payload():
    return {
        "adult_confirmed": True,
        "risk_disclosure_accepted": True,
        "advisory_disclaimer_accepted": True,
        "terms_accepted": True,
        "privacy_acknowledged": True,
    }


def onboarding_payload(**overrides):
    payload = {
        "risk_profile": "Balanced",
        "base_currency": "CHF",
        "investment_horizon": "long",
        "suitability_acknowledged": True,
    }
    payload.update(overrides)
    return payload


class E11OnboardingTests(unittest.TestCase):
    def test_onboarding_route_is_exposed(self):
        function_app = (ROOT / "api" / "function_app.py").read_text(encoding="utf-8")
        self.assertIn('route="onboarding"', function_app)

    def setUp(self):
        self.repository = InMemoryAppUserRepository(clock=lambda: NOW)
        self.service = IdentityService(self.repository, clock=lambda: NOW)
        self.user_header = principal_header()
        self.admin_header = principal_header(
            user_id="admin-1",
            roles=["anonymous", "authenticated", "admin", "user"],
            details="admin@outlook.com",
        )

    def approve_user(self):
        pending, _ = self.service.register(self.user_header, registration_payload())
        self.service.me(self.admin_header)
        return self.service.review_user(self.admin_header, pending.user_sk, "approve")

    def test_active_user_completes_owner_scoped_onboarding(self):
        approved = self.approve_user()

        onboarded = self.service.onboard(self.user_header, onboarding_payload())
        admin = self.service.me(self.admin_header)

        self.assertEqual(onboarded.user_sk, approved.user_sk)
        self.assertTrue(onboarded.onboarded)
        self.assertEqual(onboarded.risk_profile, "Balanced")
        self.assertEqual(onboarded.base_currency, "CHF")
        self.assertEqual(onboarded.investment_horizon, "long")
        self.assertEqual(onboarded.suitability_acknowledged_at, NOW.isoformat())
        self.assertFalse(admin.onboarded)
        self.assertIsNone(admin.investment_horizon)

    def test_admin_can_onboard_as_product_user(self):
        self.service.me(self.admin_header)

        onboarded = self.service.onboard(
            self.admin_header,
            onboarding_payload(risk_profile="Growth", base_currency="USD", investment_horizon="12m"),
        )

        self.assertEqual(onboarded.role, "admin")
        self.assertEqual(onboarded.risk_profile, "Growth")
        self.assertEqual(onboarded.public_profile()["capabilities"], ["product", "admin"])

    def test_pending_user_cannot_onboard(self):
        self.service.register(self.user_header, registration_payload())

        with self.assertRaises(AuthorizationError):
            self.service.onboard(self.user_header, onboarding_payload())

    def test_onboarding_rejects_unknown_or_unacknowledged_values(self):
        self.approve_user()
        invalid_payloads = [
            onboarding_payload(risk_profile="Speculative"),
            onboarding_payload(base_currency="BTC"),
            onboarding_payload(investment_horizon="forever"),
            onboarding_payload(suitability_acknowledged=False),
        ]

        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.service.onboard(self.user_header, payload)


if __name__ == "__main__":
    unittest.main()
