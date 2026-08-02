import base64
import datetime as dt
import json
import unittest

from api.auspex_api.app_users import InMemoryAppUserRepository
from api.auspex_api.auth import AuthenticationError, parse_swa_principal
from api.auspex_api.models import AppUser, RegistrationAcknowledgments


VERSIONS = {
    "adult_declaration": "2026-01",
    "risk_disclosure": "2026-01",
    "advisory_disclaimer": "2026-01",
    "terms": "2026-01",
    "privacy": "2026-01",
}
NOW = dt.datetime(2026, 7, 22, 10, 0, tzinfo=dt.timezone.utc)


def _header(provider="aad", user_id="msa-user-1", roles=None, details="user@outlook.com"):
    payload = {
        "identityProvider": provider,
        "userId": user_id,
        "userDetails": details,
        "userRoles": roles or ["anonymous", "authenticated"],
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _acknowledgments(**overrides):
    values = {
        "adult_confirmed": True,
        "risk_disclosure_accepted": True,
        "advisory_disclaimer_accepted": True,
        "terms_accepted": True,
        "privacy_acknowledged": True,
    }
    values.update(overrides)
    return RegistrationAcknowledgments.from_payload(values)


class E19SwaRegistrationTests(unittest.TestCase):
    def test_microsoft_authenticated_principal_is_stable(self):
        first = parse_swa_principal(_header())
        replay = parse_swa_principal(_header())

        self.assertEqual(first.identity_provider, "aad")
        self.assertEqual(first.user_sk, replay.user_sk)
        self.assertEqual(first.user_details, "user@outlook.com")

    def test_non_microsoft_or_unauthenticated_principal_is_rejected(self):
        with self.assertRaises(AuthenticationError):
            parse_swa_principal(_header(provider="github"))
        with self.assertRaises(AuthenticationError):
            parse_swa_principal(_header(roles=["anonymous"]))

    def test_registration_requires_every_acknowledgment(self):
        principal = parse_swa_principal(_header())

        with self.assertRaisesRegex(ValueError, "privacy_acknowledged"):
            AppUser.pending_registration(
                principal,
                _acknowledgments(privacy_acknowledged=False),
                VERSIONS,
                now=NOW,
            )

    def test_registration_is_pending_and_versioned(self):
        principal = parse_swa_principal(_header())
        user = AppUser.pending_registration(principal, _acknowledgments(), VERSIONS, now=NOW)

        self.assertEqual(user.status, "pending")
        self.assertIsNone(user.role)
        self.assertFalse(user.onboarded)
        self.assertEqual(user.adult_declaration_version, "2026-01")
        self.assertEqual(user.terms_accepted_at, NOW.isoformat())

    def test_registration_repository_is_idempotent(self):
        principal = parse_swa_principal(_header())
        repository = InMemoryAppUserRepository(clock=lambda: NOW)

        first, created = repository.create_pending(principal, _acknowledgments(), VERSIONS)
        replay, replay_created = repository.create_pending(principal, _acknowledgments(), VERSIONS)

        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(first.user_sk, replay.user_sk)

    def test_swa_admin_bootstrap_and_review_history(self):
        admin_principal = parse_swa_principal(_header(
            user_id="admin-1",
            roles=["anonymous", "authenticated", "admin"],
        ))
        user_principal = parse_swa_principal(_header(user_id="user-1"))
        admin = AppUser.bootstrap_admin(admin_principal, now=NOW)
        pending = AppUser.pending_registration(user_principal, _acknowledgments(), VERSIONS, now=NOW)

        approved = pending.review("approve", admin.user_sk, note="Approved", now=NOW)
        suspended = approved.review("suspend", admin.user_sk, now=NOW)
        restored = suspended.review("restore", admin.user_sk, now=NOW)

        self.assertEqual(admin.role, "admin")
        self.assertEqual(approved.role, "user")
        self.assertEqual(restored.status, "active")
        self.assertEqual([event["action"] for event in restored.review_history], [
            "approve", "suspend", "restore",
        ])


if __name__ == "__main__":
    unittest.main()