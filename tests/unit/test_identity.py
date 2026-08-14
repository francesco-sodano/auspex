"""Unit tests for the stable owner-identity partition mapping."""

from __future__ import annotations

from auspex.identity import (
    USER_NAMESPACE,
    compatible_identity_key,
    compatible_user_id,
    resolve_owner_user_id,
)


class TestCompatibleIdentityKey:
    def test_matches_sha256_formula(self):
        import hashlib

        expected = hashlib.sha256(b"aad\0provider-user-1").hexdigest()
        assert compatible_identity_key("provider-user-1") == expected

    def test_default_identity_provider_is_aad(self):
        assert compatible_identity_key("x", identity_provider="aad") == compatible_identity_key("x")

    def test_different_identity_provider_changes_the_key(self):
        assert compatible_identity_key("x", identity_provider="aad") != compatible_identity_key(
            "x", identity_provider="other"
        )


class TestCompatibleUserId:
    def test_matches_known_stable_value(self):
        assert compatible_user_id("provider-user-1") == "354accb7-c30c-5c3a-b23e-b9af5223ddf3"

    def test_uses_the_fixed_namespace(self):
        assert str(USER_NAMESPACE) == "b7301e2f-0b55-49e4-91bd-9dfdc2ae73e7"

    def test_stable_across_calls(self):
        assert compatible_user_id("provider-user-1") == compatible_user_id("provider-user-1")

    def test_distinct_users_get_distinct_ids(self):
        assert compatible_user_id("provider-user-1") != compatible_user_id("provider-user-2")


class TestResolveOwnerUserId:
    def test_returns_default_when_unconfigured(self):
        assert resolve_owner_user_id(None) == "owner"
        assert resolve_owner_user_id("") == "owner"

    def test_custom_default(self):
        assert resolve_owner_user_id(None, default="fallback") == "fallback"

    def test_returns_compatible_mapping_when_configured(self):
        assert resolve_owner_user_id("provider-user-1") == compatible_user_id("provider-user-1")

    def test_matches_what_the_api_would_resolve_for_the_same_oid(self):
        """The whole point: the pipeline's resolved user_id for a configured
        owner must equal what auspex.api.auth resolves for that same oid."""

        from auspex.api.auth import compatible_user_id as auth_compatible_user_id

        oid = "00000000-1111-2222-3333-444444444444"
        assert resolve_owner_user_id(oid) == auth_compatible_user_id(oid)
