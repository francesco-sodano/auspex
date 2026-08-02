import base64
import datetime as dt
import json
import unittest

from api.auspex_api.app_users import InMemoryAppUserRepository
from api.auspex_api.auth import AuthenticationError, parse_swa_principal
from api.auspex_api.owner_scoped import OwnerScope, OwnerScopedCosmosContainer
from api.auspex_api.services import (
    AuthorizationError,
    DOCUMENT_VERSIONS,
    IdentityService,
    InvalidTransitionError,
    RegistrationRequiredError,
)


NOW = dt.datetime(2026, 7, 22, 10, 0, tzinfo=dt.timezone.utc)


def principal_header(user_id="user-1", roles=None, provider="aad", details="user@outlook.com"):
    payload = {
        "identityProvider": provider,
        "userId": user_id,
        "userDetails": details,
        "userRoles": roles or ["anonymous", "authenticated"],
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def registration_payload(**overrides):
    payload = {
        "adult_confirmed": True,
        "risk_disclosure_accepted": True,
        "advisory_disclaimer_accepted": True,
        "terms_accepted": True,
        "privacy_acknowledged": True,
    }
    payload.update(overrides)
    return payload


class _PartitionedContainer:
    def __init__(self):
        self.documents = {}

    def create_item(self, body):
        self.documents[(body["owner_user_sk"], body["id"])] = dict(body)
        return dict(body)

    def read_item(self, item, partition_key):
        try:
            return dict(self.documents[(partition_key, item)])
        except KeyError as exc:
            error = RuntimeError("not found")
            error.status_code = 404
            raise error from exc

    def replace_item(self, item, body):
        key = (body["owner_user_sk"], item)
        if key not in self.documents:
            error = RuntimeError("not found")
            error.status_code = 404
            raise error
        self.documents[key] = dict(body)
        return dict(body)

    def delete_item(self, item, partition_key):
        try:
            del self.documents[(partition_key, item)]
        except KeyError as exc:
            error = RuntimeError("not found")
            error.status_code = 404
            raise error from exc


class E19IdentityTests(unittest.TestCase):
    def setUp(self):
        self.repository = InMemoryAppUserRepository(clock=lambda: NOW)
        self.service = IdentityService(self.repository)
        self.user_header = principal_header()
        self.admin_header = principal_header(
            user_id="admin-1",
            details="admin@outlook.com",
            roles=["anonymous", "authenticated", "admin"],
        )

    def test_unregistered_identity_is_not_automatically_accepted(self):
        with self.assertRaises(RegistrationRequiredError):
            self.service.me(self.user_header)

    def test_registration_is_pending_idempotent_and_versioned(self):
        first, created = self.service.register(self.user_header, registration_payload())
        replay, replay_created = self.service.register(self.user_header, registration_payload())

        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(first.user_sk, replay.user_sk)
        self.assertEqual(first.status, "pending")
        self.assertIsNone(first.role)
        self.assertEqual(first.terms_version, DOCUMENT_VERSIONS["terms"])

    def test_registration_rejects_missing_acknowledgment(self):
        with self.assertRaisesRegex(ValueError, "adult_confirmed"):
            self.service.register(
                self.user_header,
                registration_payload(adult_confirmed=False),
            )

    def test_only_swa_admin_can_review(self):
        pending, _ = self.service.register(self.user_header, registration_payload())

        with self.assertRaises(AuthorizationError):
            self.service.review_user(self.user_header, pending.user_sk, "approve")

    def test_admin_bootstrap_and_complete_review_lifecycle(self):
        admin = self.service.me(self.admin_header)
        pending, _ = self.service.register(self.user_header, registration_payload())
        approved = self.service.review_user(
            self.admin_header, pending.user_sk, "approve", note="Approved"
        )
        suspended = self.service.review_user(
            self.admin_header, pending.user_sk, "suspend"
        )
        restored = self.service.review_user(
            self.admin_header, pending.user_sk, "restore"
        )

        self.assertEqual(admin.role, "admin")
        self.assertEqual(admin.public_profile()["capabilities"], ["product", "admin"])
        self.assertEqual(self.service.product_user(self.admin_header), admin)
        self.assertEqual(approved.role, "user")
        self.assertEqual(approved.public_profile()["capabilities"], ["product"])
        current_user = self.service.product_user(self.user_header)
        self.assertEqual(current_user.user_sk, approved.user_sk)
        self.assertEqual(current_user.status, "active")
        self.assertEqual(current_user.public_profile()["capabilities"], ["product"])
        self.assertEqual(restored.status, "active")
        self.assertEqual([event["action"] for event in restored.review_history], [
            "approve", "suspend", "restore",
        ])

        rejected_header = principal_header(user_id="user-2")
        rejected, _ = self.service.register(rejected_header, registration_payload())
        rejected = self.service.review_user(
            self.admin_header, rejected.user_sk, "reject", note="Not approved"
        )
        self.assertEqual(rejected.status, "rejected")
        self.assertIsNone(rejected.role)
        self.assertEqual(rejected.public_profile()["capabilities"], [])

    def test_invalid_transition_fails_closed(self):
        pending, _ = self.service.register(self.user_header, registration_payload())
        self.service.me(self.admin_header)

        with self.assertRaises(InvalidTransitionError):
            self.service.review_user(self.admin_header, pending.user_sk, "restore")

    def test_non_microsoft_principal_is_rejected(self):
        with self.assertRaises(AuthenticationError):
            parse_swa_principal(principal_header(provider="github"))

    def test_owner_scoped_storage_blocks_cross_user_operations(self):
        repository = OwnerScopedCosmosContainer(_PartitionedContainer())
        owner_a = OwnerScope("owner-a")
        owner_b = OwnerScope("owner-b")
        repository.create(owner_b, {"id": "position-1", "value": 10})

        self.assertIsNone(repository.read(owner_a, "position-1"))
        self.assertIsNone(repository.replace(owner_a, "position-1", {"value": 99}))
        self.assertFalse(repository.delete(owner_a, "position-1"))
        self.assertEqual(repository.read(owner_b, "position-1")["value"], 10)


if __name__ == "__main__":
    unittest.main()