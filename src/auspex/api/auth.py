"""Entra External ID JWT validation (arc42 F-16, TC-04 — no shared secrets).

Tokens are RS256-signed; the corresponding public keys are fetched from the
tenant's JWKS endpoint and cached. No client secret or connection string is
involved — the API only ever validates tokens the caller already obtained
from Entra.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import jwt
from fastapi import Header, HTTPException, status
from jwt import PyJWKClient

from auspex.identity import compatible_user_id
from auspex.settings import Settings, get_settings

JWKS_CACHE_TTL_SECONDS = 3600


@dataclass
class AuthenticatedUser:
    user_id: str
    claims: dict


class EntraTokenValidator:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._jwk_client: PyJWKClient | None = None
        self._jwk_client_created_at: float = 0.0

    def _get_jwk_client(self) -> PyJWKClient:
        now = time.monotonic()
        if self._jwk_client is None or (now - self._jwk_client_created_at) > JWKS_CACHE_TTL_SECONDS:
            self._jwk_client = PyJWKClient(self._settings.entra_jwks_url)
            self._jwk_client_created_at = now
        return self._jwk_client

    def validate(self, token: str) -> AuthenticatedUser:
        try:
            signing_key = self._get_jwk_client().get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._settings.entra_audience,
                issuer=self._settings.entra_issuer,
            )
        except jwt.PyJWTError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"invalid token: {exc}") from exc

        # Prefer the tenant-stable object ID. App-scoped `sub` values can map
        # the same person to a different owner partition in another client.
        provider_user_id = claims.get("oid") or claims.get("sub")
        if not provider_user_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="token missing subject/oid claim")
        configured_owner = self._settings.owner_provider_user_id
        if self._settings.environment != "local" and not configured_owner:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Auspex owner identity is not configured",
            )
        if configured_owner and str(provider_user_id) != configured_owner:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="authenticated principal is not the configured Auspex owner",
            )
        return AuthenticatedUser(user_id=compatible_user_id(str(provider_user_id)), claims=claims)


_validator: EntraTokenValidator | None = None


def get_validator() -> EntraTokenValidator:
    global _validator
    if _validator is None:
        _validator = EntraTokenValidator(get_settings())
    return _validator


async def get_current_user(authorization: str | None = Header(default=None)) -> AuthenticatedUser:
    """FastAPI dependency extracting and validating the Entra bearer token."""

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    token = authorization.split(" ", 1)[1]
    return get_validator().validate(token)
