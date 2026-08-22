"""Claim handling in `EntraTokenValidator` (arc42 F-16).

Tenant-type compatibility (workforce vs. Entra External ID) and the
migration window live in `test_auth_tenant_compatibility.py`; this module
covers which claim wins when several are present.
"""

from __future__ import annotations

from unittest.mock import Mock

from auspex.api.auth import EntraTokenValidator
from auspex.identity import compatible_user_id
from auspex.settings import Settings

ISSUER = "https://login.microsoftonline.com/tenant-id/v2.0"
JWKS_URL = "https://login.microsoftonline.com/tenant-id/discovery/v2.0/keys"


def make_validator(monkeypatch, claims: dict, **settings_overrides) -> EntraTokenValidator:
    """A validator whose signature check and key fetch are stubbed out.

    Only claim handling is under test here, so the token is never really
    signed; `iss` must still be present because the validator selects the
    signing keys by issuer.
    """

    monkeypatch.setattr("auspex.api.auth.jwt.decode", lambda *args, **kwargs: claims)
    settings = Settings(
        entra_audience="audience",
        entra_issuer=ISSUER,
        entra_jwks_url=JWKS_URL,
        **settings_overrides,
    )
    jwk_client = Mock()
    jwk_client.get_signing_key_from_jwt.return_value.key = object()
    monkeypatch.setattr("auspex.api.auth.PyJWKClient", lambda *args, **kwargs: jwk_client)
    return EntraTokenValidator(settings)


def test_token_validation_prefers_stable_oid_over_app_scoped_sub(monkeypatch) -> None:
    claims = {
        "iss": ISSUER,
        "oid": "00000000-0000-0000-7923-aede4905697c",
        "sub": "app-scoped-subject",
    }

    user = make_validator(monkeypatch, claims).validate("token")

    assert user.user_id == compatible_user_id(claims["oid"])


def test_token_validation_admits_any_tenant_principal_for_registration(monkeypatch) -> None:
    """Multi-user: authentication no longer decides authorisation.

    A valid tenant principal who is not the pre-existing owner must be
    admitted as an *identity* so they can reach the registration endpoint.
    Whether they may read or write anything is decided separately against
    the `app_users` lifecycle record (see `auspex.api.access`), which is
    exactly what keeps an unapproved principal out of the product.
    """

    claims = {
        "iss": ISSUER,
        "oid": "00000000-0000-0000-0000-000000000099",
        "sub": "app-scoped-subject",
        "email": "someone.else@example.test",
    }

    user = make_validator(
        monkeypatch, claims, owner_provider_user_id="00000000-0000-0000-0000-000000000011"
    ).validate("token")

    assert user.user_id == compatible_user_id(claims["oid"])
    assert user.provider_user_id == claims["oid"]
    assert user.email == "someone.else@example.test"
    assert user.email_verified is False


def test_production_token_validation_no_longer_requires_configured_owner(monkeypatch) -> None:
    """A production deployment without a configured legacy owner is valid.

    The owner setting now only pins a pre-existing ledger partition; it is
    not an allow-list, so its absence must not take authentication down.
    """

    claims = {
        "iss": ISSUER,
        "oid": "00000000-0000-0000-0000-000000000099",
        "sub": "app-scoped-subject",
    }

    user = make_validator(monkeypatch, claims, environment="production").validate("token")

    assert user.user_id == compatible_user_id(claims["oid"])


def test_email_claim_precedence_prefers_verified_email(monkeypatch) -> None:
    claims = {
        "iss": ISSUER,
        "oid": "00000000-0000-0000-0000-0000000000aa",
        "preferred_username": "fallback@example.test",
        "email": "Primary@Example.test",
        "name": "Primary Person",
    }

    user = make_validator(monkeypatch, claims).validate("token")

    assert user.email == "primary@example.test"
    assert user.display_name == "Primary Person"


def test_token_validation_applies_configured_clock_skew(monkeypatch) -> None:
    claims = {
        "iss": ISSUER,
        "oid": "00000000-0000-0000-0000-0000000000aa",
    }
    decode = Mock(return_value=claims)
    monkeypatch.setattr("auspex.api.auth.jwt.decode", decode)
    validator = EntraTokenValidator(
        Settings(
            entra_audience="audience",
            entra_issuer=ISSUER,
            entra_jwks_url=JWKS_URL,
            jwt_clock_skew_seconds=75,
        )
    )
    jwk_client = Mock()
    jwk_client.get_signing_key_from_jwt.return_value.key = object()
    monkeypatch.setattr(
        "auspex.api.auth.PyJWKClient",
        lambda *args, **kwargs: jwk_client,
    )

    validator.validate("token")

    assert decode.call_args_list[-1].kwargs["leeway"] == 75
