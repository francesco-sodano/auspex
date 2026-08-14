from __future__ import annotations

from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from auspex.api.auth import EntraTokenValidator
from auspex.identity import compatible_user_id
from auspex.settings import Settings


def test_token_validation_prefers_stable_oid_over_app_scoped_sub(monkeypatch) -> None:
    claims = {
        "oid": "00000000-0000-0000-7923-aede4905697c",
        "sub": "app-scoped-subject",
    }
    monkeypatch.setattr("auspex.api.auth.jwt.decode", lambda *args, **kwargs: claims)
    validator = EntraTokenValidator(
        Settings(
            entra_audience="audience",
            entra_issuer="issuer",
            entra_jwks_url="https://example.test/keys",
        )
    )
    jwk_client = Mock()
    jwk_client.get_signing_key_from_jwt.return_value.key = object()
    monkeypatch.setattr(validator, "_get_jwk_client", lambda: jwk_client)

    user = validator.validate("token")

    assert user.user_id == compatible_user_id(claims["oid"])


def test_token_validation_rejects_other_tenant_principals(monkeypatch) -> None:
    claims = {
        "oid": "00000000-0000-0000-0000-000000000099",
        "sub": "app-scoped-subject",
    }
    monkeypatch.setattr("auspex.api.auth.jwt.decode", lambda *args, **kwargs: claims)
    validator = EntraTokenValidator(
        Settings(
            entra_audience="audience",
            entra_issuer="issuer",
            entra_jwks_url="https://example.test/keys",
            owner_provider_user_id="00000000-0000-0000-0000-000000000011",
        )
    )
    jwk_client = Mock()
    jwk_client.get_signing_key_from_jwt.return_value.key = object()
    monkeypatch.setattr(validator, "_get_jwk_client", lambda: jwk_client)

    with pytest.raises(HTTPException) as exc_info:
        validator.validate("token")

    assert exc_info.value.status_code == 403


def test_production_token_validation_requires_configured_owner(monkeypatch) -> None:
    claims = {
        "oid": "00000000-0000-0000-0000-000000000099",
        "sub": "app-scoped-subject",
    }
    monkeypatch.setattr("auspex.api.auth.jwt.decode", lambda *args, **kwargs: claims)
    validator = EntraTokenValidator(
        Settings(
            environment="production",
            entra_audience="audience",
            entra_issuer="issuer",
            entra_jwks_url="https://example.test/keys",
        )
    )
    jwk_client = Mock()
    jwk_client.get_signing_key_from_jwt.return_value.key = object()
    monkeypatch.setattr(validator, "_get_jwk_client", lambda: jwk_client)

    with pytest.raises(HTTPException) as exc_info:
        validator.validate("token")

    assert exc_info.value.status_code == 503
