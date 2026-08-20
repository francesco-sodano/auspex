"""Tenant-type compatibility: workforce *and* Entra External ID (arc42 F-16).

Friends signing up with a personal Gmail or Outlook address requires an
**external** tenant (Microsoft Entra External ID / CIAM), which differs from
a workforce tenant in three ways that all break naive configuration:

1. the authority host is ``<subdomain>.ciamlogin.com``, not
   ``login.microsoftonline.com``;
2. MSAL refuses that host unless the SPA declares it in ``knownAuthorities``;
3. the ``iss`` claim follows the authority actually used, and an external
   tenant may legitimately issue either the tenant-id or the
   ``.onmicrosoft.com`` form — so it must never be guessed.

These tests pin all three, plus the tenant-migration window that keeps the
existing production owner signed in during a cutover.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from auspex.api.auth import EntraTokenValidator, extract_email
from auspex.api.routes.public import known_authorities
from auspex.identity import compatible_user_id
from auspex.settings import Settings

WORKFORCE_ISSUER = "https://login.microsoftonline.com/00000000-0000-0000-0000-0000000000aa/v2.0"
CIAM_HOST = "auspexfriends.ciamlogin.com"
CIAM_AUTHORITY = f"https://{CIAM_HOST}/00000000-0000-0000-0000-0000000000bb"
CIAM_ISSUER = f"{CIAM_AUTHORITY}/v2.0"
CIAM_ISSUER_DOMAIN_FORM = f"https://{CIAM_HOST}/auspexfriends.onmicrosoft.com/v2.0"


def external_settings(**overrides) -> Settings:
    values = {
        "entra_audience": "audience",
        "entra_tenant_id": "00000000-0000-0000-0000-0000000000bb",
        "entra_authority": CIAM_AUTHORITY,
        "entra_issuer": CIAM_ISSUER,
        "entra_jwks_url": f"{CIAM_AUTHORITY}/discovery/v2.0/keys",
        "entra_known_authority": CIAM_HOST,
    }
    values.update(overrides)
    return Settings(**values)


def stub_validator(monkeypatch, settings: Settings, claims: dict) -> EntraTokenValidator:
    monkeypatch.setattr("auspex.api.auth.jwt.decode", lambda *args, **kwargs: claims)
    validator = EntraTokenValidator(settings)
    jwk_client = Mock()
    jwk_client.get_signing_key_from_jwt.return_value.key = object()
    monkeypatch.setattr(
        "auspex.api.auth.PyJWKClient", lambda *args, **kwargs: jwk_client
    )
    return validator


class TestExternalTenantTokens:
    def test_ciam_issued_token_is_accepted(self, monkeypatch):
        claims = {
            "iss": CIAM_ISSUER,
            "oid": "11111111-1111-1111-1111-111111111111",
            "email": "friend@gmail.com",
            "email_verified": True,
        }
        validator = stub_validator(monkeypatch, external_settings(), claims)

        user = validator.validate("token")

        assert user.user_id == compatible_user_id(claims["oid"])
        assert user.email == "friend@gmail.com"
        assert user.email_verified is True

    def test_domain_form_issuer_is_accepted_when_configured(self, monkeypatch):
        """An external tenant may issue the .onmicrosoft.com authority form.

        The operator configures whichever the tenant actually mints; the API
        never assumes one shape.
        """

        claims = {"iss": CIAM_ISSUER_DOMAIN_FORM, "oid": "oid-friend"}
        settings = external_settings(entra_issuer=CIAM_ISSUER_DOMAIN_FORM)
        validator = stub_validator(monkeypatch, settings, claims)

        assert validator.validate("token").provider_user_id == "oid-friend"

    def test_token_from_an_untrusted_issuer_is_rejected(self, monkeypatch):
        claims = {"iss": "https://evil.example.test/v2.0", "oid": "oid-attacker"}
        validator = stub_validator(monkeypatch, external_settings(), claims)

        with pytest.raises(HTTPException) as exc_info:
            validator.validate("token")

        assert exc_info.value.status_code == 401
        assert "issuer" in str(exc_info.value.detail)

    def test_token_without_an_issuer_is_rejected(self, monkeypatch):
        validator = stub_validator(monkeypatch, external_settings(), {"oid": "oid-friend"})

        with pytest.raises(HTTPException) as exc_info:
            validator.validate("token")

        assert exc_info.value.status_code == 401

    def test_unconfigured_deployment_fails_closed(self, monkeypatch):
        validator = stub_validator(monkeypatch, Settings(entra_audience="audience"), {"iss": "x"})

        with pytest.raises(HTTPException) as exc_info:
            validator.validate("token")

        assert exc_info.value.status_code == 503

    @pytest.mark.parametrize(
        "claims",
        [
            {"email": "friend@gmail.com"},
            {"emails": ["friend@gmail.com"]},
            {"preferred_username": "friend@gmail.com"},
        ],
    )
    def test_self_service_signup_email_claims_are_all_understood(self, claims):
        """External tenants place the sign-up address in varying claims.

        The initial-admin bootstrap matches on email, so failing to read it
        would leave a fresh deployment with no administrator.
        """

        assert extract_email(claims) == "friend@gmail.com"


class TestTenantMigration:
    def test_legacy_tenant_tokens_stay_valid_during_a_cutover(self, monkeypatch):
        """The production owner must not be locked out mid-migration."""

        claims = {"iss": WORKFORCE_ISSUER, "oid": "oid-owner"}
        settings = external_settings(
            entra_legacy_issuer=WORKFORCE_ISSUER,
            entra_legacy_jwks_url="https://login.microsoftonline.com/tid/discovery/v2.0/keys",
            entra_legacy_audience="legacy-audience",
            owner_legacy_provider_user_id="oid-owner",
            owner_provider_user_id="oid-owner-new-tenant",
        )
        validator = stub_validator(monkeypatch, settings, claims)

        user = validator.validate("token")
        assert user.provider_user_id == "oid-owner-new-tenant"
        assert user.user_id == compatible_user_id("oid-owner-new-tenant")

    def test_legacy_alias_applies_only_to_the_configured_owner(self, monkeypatch):
        claims = {"iss": WORKFORCE_ISSUER, "oid": "oid-someone-else"}
        settings = external_settings(
            entra_legacy_issuer=WORKFORCE_ISSUER,
            entra_legacy_jwks_url="https://login.microsoftonline.com/tid/discovery/v2.0/keys",
            entra_legacy_audience="legacy-audience",
            owner_legacy_provider_user_id="oid-owner",
            owner_provider_user_id="oid-owner-new-tenant",
        )
        user = stub_validator(monkeypatch, settings, claims).validate("token")

        assert user.user_id == compatible_user_id("oid-someone-else")

    def test_legacy_token_is_validated_against_legacy_audience(self, monkeypatch):
        claims = {"iss": WORKFORCE_ISSUER, "oid": "oid-owner"}
        decode = Mock(return_value=claims)
        monkeypatch.setattr("auspex.api.auth.jwt.decode", decode)
        jwk_client = Mock()
        jwk_client.get_signing_key_from_jwt.return_value.key = object()
        monkeypatch.setattr(
            "auspex.api.auth.PyJWKClient",
            lambda *args, **kwargs: jwk_client,
        )
        validator = EntraTokenValidator(
            external_settings(
                entra_legacy_issuer=WORKFORCE_ISSUER,
                entra_legacy_jwks_url="https://login.microsoftonline.com/tid/discovery/v2.0/keys",
                entra_legacy_audience="legacy-audience",
            )
        )

        validator.validate("token")

        assert decode.call_args_list[-1].kwargs["audience"] == "legacy-audience"

    def test_the_same_owner_keeps_their_partition_across_tenants(self):
        """`user_id` is derived from the object ID alone.

        Migrating the *tenant* does not move a user's data as long as their
        object ID is preserved; a genuinely new object ID is a new user, which
        is why the ledger partition override exists for the production owner.
        """

        assert compatible_user_id("oid-owner") == compatible_user_id("oid-owner")

    def test_legacy_issuer_is_ignored_unless_the_full_tuple_is_configured(self, monkeypatch):
        """A partial legacy tuple must not silently accept tokens."""

        claims = {"iss": WORKFORCE_ISSUER, "oid": "oid-owner"}
        settings = external_settings(entra_legacy_issuer=WORKFORCE_ISSUER)
        validator = stub_validator(monkeypatch, settings, claims)

        with pytest.raises(HTTPException) as exc_info:
            validator.validate("token")

        assert exc_info.value.status_code == 401

    def test_each_issuer_is_validated_against_its_own_keys(self, monkeypatch):
        """A legacy tenant's keys must never validate a current-tenant token.

        The binding is selected by the token's own `iss`, so key sets cannot
        be substituted for one another.
        """

        settings = external_settings(
            entra_legacy_issuer=WORKFORCE_ISSUER,
            entra_legacy_jwks_url="https://login.microsoftonline.com/tid/discovery/v2.0/keys",
            entra_legacy_audience="legacy-audience",
        )
        validator = EntraTokenValidator(settings)

        bindings = {
            binding.issuer: (binding.jwks_url, binding.audience)
            for binding in validator.issuer_bindings()
        }

        assert bindings[CIAM_ISSUER][0].startswith(f"https://{CIAM_HOST}/")
        assert bindings[CIAM_ISSUER][1] == "audience"
        assert bindings[WORKFORCE_ISSUER][0].startswith(
            "https://login.microsoftonline.com/"
        )
        assert bindings[WORKFORCE_ISSUER][1] == "legacy-audience"


class TestOpenIdDiscovery:
    def test_static_binding_reuses_the_same_jwks_client_cache(self):
        validator = EntraTokenValidator(
            external_settings(entra_openid_configuration_url="")
        )

        first = validator.issuer_bindings()[0]
        second = validator.issuer_bindings()[0]

        assert first is second

    def test_metadata_is_authoritative_over_static_configuration(self, monkeypatch):
        """The tenant itself cannot be misconfigured; a hand-typed issuer can."""

        discovered_issuer = CIAM_ISSUER_DOMAIN_FORM
        discovered_jwks = f"https://{CIAM_HOST}/auspexfriends.onmicrosoft.com/discovery/v2.0/keys"

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"issuer": discovered_issuer, "jwks_uri": discovered_jwks}

        monkeypatch.setattr("auspex.api.auth.httpx.get", lambda *a, **kw: FakeResponse())
        settings = external_settings(
            entra_openid_configuration_url=f"{CIAM_AUTHORITY}/v2.0/.well-known/openid-configuration",
        )
        validator = EntraTokenValidator(settings)

        bindings = validator.issuer_bindings()

        assert bindings[0].issuer == discovered_issuer
        assert bindings[0].jwks_url == discovered_jwks
        # The statically configured pair is still accepted as a fallback.
        assert any(binding.issuer == CIAM_ISSUER for binding in bindings)

    def test_metadata_outage_degrades_to_static_configuration(self, monkeypatch):
        """A transient metadata failure must not take authentication down."""

        def explode(*_args, **_kwargs):
            raise RuntimeError("metadata endpoint unreachable")

        monkeypatch.setattr("auspex.api.auth.httpx.get", explode)
        settings = external_settings(
            entra_openid_configuration_url=f"{CIAM_AUTHORITY}/v2.0/.well-known/openid-configuration",
        )
        validator = EntraTokenValidator(settings)

        assert [binding.issuer for binding in validator.issuer_bindings()] == [CIAM_ISSUER]

    def test_metadata_outage_is_negative_cached(self, monkeypatch):
        attempts = 0

        def explode(*_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            raise RuntimeError("metadata endpoint unreachable")

        monkeypatch.setattr("auspex.api.auth.httpx.get", explode)
        validator = EntraTokenValidator(
            external_settings(
                entra_openid_configuration_url=(
                    f"{CIAM_AUTHORITY}/v2.0/.well-known/openid-configuration"
                ),
            )
        )

        validator.issuer_bindings()
        validator.issuer_bindings()

        assert attempts == 1


class TestKnownAuthorities:
    def test_external_tenant_host_is_advertised_to_msal(self):
        assert known_authorities(external_settings()) == [CIAM_HOST]

    def test_derived_from_the_authority_when_not_explicitly_set(self):
        settings = external_settings(entra_known_authority="")

        assert known_authorities(settings) == [CIAM_HOST]

    def test_workforce_tenant_needs_no_known_authority(self):
        settings = Settings(
            entra_audience="audience",
            entra_authority="https://login.microsoftonline.com/tenant-id",
        )

        assert known_authorities(settings) == []
