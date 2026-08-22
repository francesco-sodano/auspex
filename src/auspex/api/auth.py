"""Entra JWT validation (arc42 F-16, TC-04 — no shared secrets).

Tokens are RS256-signed; the corresponding public keys are fetched from the
tenant's JWKS endpoint and cached. No client secret or connection string is
involved — the API only ever validates tokens the caller already obtained
from Entra.

**Tenant-type agnostic.** Auspex is deployed against either a *workforce*
tenant (`login.microsoftonline.com`) or an *external* tenant — Microsoft
Entra External ID / CIAM, `<subdomain>.ciamlogin.com` — which is what allows
friends to sign up with a personal Gmail or Outlook address through a
sign-up/sign-in user flow. The two tenant types issue different ``iss``
values and expose different JWKS hosts, and an external tenant may issue
either the tenant-id or the ``.onmicrosoft.com`` authority form. None of
that is guessed here: the issuer and JWKS URI are configured explicitly and,
where an OpenID Connect metadata URL is available, read from the tenant
itself, which is the only source that cannot be misconfigured.

**Migration-safe.** A deployment moving between tenants can declare a legacy
issuer/JWKS/audience tuple and the old object ID of its pre-existing owner.
Tokens from either tenant then validate against their own issuer, keys and
audience during the cutover, while that one old principal resolves to the
new owner's application account. No general email- or subject-based aliasing
is permitted.

Validation proves *identity only*. It deliberately does not decide whether
the principal may use Auspex: that is a database-backed lifecycle decision
made in :mod:`auspex.api.access` against the ``app_users`` container. Any
valid tenant principal may therefore reach the registration/session
endpoints, and nothing else, until an administrator approves them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import httpx
import jwt
from fastapi import Header, HTTPException, status
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError

from auspex.identity import compatible_user_id
from auspex.settings import Settings, get_settings

logger = logging.getLogger("auspex.api.auth")

JWKS_CACHE_TTL_SECONDS = 3600
OPENID_METADATA_TTL_SECONDS = 3600
OPENID_METADATA_TIMEOUT_SECONDS = 5.0

#: Claims Entra may use to carry a verified email address, in preference order.
#: External tenants commonly place a self-service sign-up address in `email`
#: or in the `emails` array rather than in `upn`.
EMAIL_CLAIMS = ("email", "preferred_username", "upn", "unique_name")
NAME_CLAIMS = ("name", "given_name")


@dataclass
class AuthenticatedUser:
    user_id: str
    claims: dict
    provider_user_id: str = ""
    email: str | None = None
    email_verified: bool = False
    display_name: str | None = None
    identity_provider: str = "aad"


@dataclass
class IssuerBinding:
    """One acceptable token issuer and the JWKS that signs for it."""

    issuer: str
    jwks_url: str
    audience: str = ""
    legacy: bool = False
    _client: PyJWKClient | None = field(default=None, repr=False)
    _created_at: float = field(default=0.0, repr=False)

    def jwk_client(self) -> PyJWKClient:
        now = time.monotonic()
        if self._client is None or (now - self._created_at) > JWKS_CACHE_TTL_SECONDS:
            self._client = PyJWKClient(self.jwks_url)
            self._created_at = now
        return self._client


def _first_claim(claims: dict, names: tuple[str, ...]) -> str | None:
    for name in names:
        value = claims.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def extract_email(claims: dict) -> str | None:
    """The principal's email as asserted by the identity provider.

    Only used to match ``Settings.initial_admin_email`` at first-admin
    bootstrap and to show an administrator who is asking for access; never as
    a durable authority key (that is the immutable ``oid``).
    """

    candidate = _first_claim(claims, EMAIL_CLAIMS)
    if candidate is None:
        emails = claims.get("emails")
        if isinstance(emails, list):
            for value in emails:
                if isinstance(value, str) and value.strip():
                    candidate = value.strip()
                    break
    return candidate.lower() if candidate else None



class EntraTokenValidator:
    """Validates a bearer token against every issuer this deployment trusts."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._discovered: IssuerBinding | None = None
        self._discovered_at: float = 0.0
        self._binding_cache: dict[tuple[str, str, str, bool], IssuerBinding] = {}

    def _binding(
        self,
        issuer: str,
        jwks_url: str,
        *,
        audience: str | None = None,
        legacy: bool = False,
    ) -> IssuerBinding:
        resolved_audience = audience or self._settings.entra_audience
        key = (issuer, jwks_url, resolved_audience, legacy)
        binding = self._binding_cache.get(key)
        if binding is None:
            binding = IssuerBinding(
                issuer=issuer,
                jwks_url=jwks_url,
                audience=resolved_audience,
                legacy=legacy,
            )
            self._binding_cache[key] = binding
        return binding

    # ------------------------------------------------------------- discovery

    def _discover(self) -> IssuerBinding | None:
        """Read ``issuer``/``jwks_uri`` from the tenant's OpenID metadata.

        This is the only configuration source that cannot be wrong: workforce
        and external tenants disagree about the issuer string, and an external
        tenant's authority may be expressed with either the tenant id or the
        ``.onmicrosoft.com`` domain. Asking the tenant removes the guess.

        A failure here is not fatal — the statically configured issuer/JWKS
        remain in force — because a transient metadata outage must not take
        authentication down.
        """

        url = self._settings.entra_openid_configuration_url
        if not url:
            return None
        now = time.monotonic()
        if self._discovered_at and (now - self._discovered_at) < OPENID_METADATA_TTL_SECONDS:
            return self._discovered
        try:
            response = httpx.get(url, timeout=OPENID_METADATA_TIMEOUT_SECONDS)
            response.raise_for_status()
            document = response.json()
            issuer, jwks_uri = document["issuer"], document["jwks_uri"]
        except Exception:  # noqa: BLE001 - degrade to static configuration
            self._discovered_at = now
            logger.warning("could not read OpenID configuration from %s", url, exc_info=True)
            return self._discovered
        self._discovered = self._binding(str(issuer), str(jwks_uri))
        self._discovered_at = now
        return self._discovered

    def issuer_bindings(self) -> list[IssuerBinding]:
        """Every issuer this deployment accepts, most-authoritative first.

        Discovered metadata wins over static configuration; the optional
        legacy pair is kept last so a tenant migration does not lock the
        existing owner out mid-cutover.
        """

        bindings: list[IssuerBinding] = []
        discovered = self._discover()
        if discovered is not None:
            bindings.append(discovered)
        if self._settings.entra_issuer and self._settings.entra_jwks_url:
            if not any(item.issuer == self._settings.entra_issuer for item in bindings):
                bindings.append(
                    self._binding(
                        self._settings.entra_issuer,
                        self._settings.entra_jwks_url,
                    )
                )
        if (
            self._settings.entra_legacy_issuer
            and self._settings.entra_legacy_jwks_url
            and self._settings.entra_legacy_audience
        ):
            bindings.append(
                self._binding(
                    self._settings.entra_legacy_issuer,
                    self._settings.entra_legacy_jwks_url,
                    audience=self._settings.entra_legacy_audience,
                    legacy=True,
                )
            )
        return bindings

    def _binding_for(self, token: str) -> IssuerBinding:
        """Select the binding matching the token's own ``iss``.

        Choosing the key set by issuer — rather than trying each in turn — is
        what stops a legacy issuer's keys from being used to accept a token
        that claims to come from the current tenant.
        """

        bindings = self.issuer_bindings()
        if not bindings:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Entra token validation is not configured",
            )
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=f"invalid token: {exc}"
            ) from exc
        issuer = unverified.get("iss") if isinstance(unverified, dict) else None
        for binding in bindings:
            if binding.issuer == issuer:
                return binding
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token issuer is not trusted by this deployment",
        )

    # ------------------------------------------------------------ validation

    def validate(self, token: str) -> AuthenticatedUser:
        binding = self._binding_for(token)
        try:
            signing_key = binding.jwk_client().get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=binding.audience,
                issuer=binding.issuer,
                leeway=self._settings.jwt_clock_skew_seconds,
            )
        except PyJWKClientConnectionError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="identity signing keys are temporarily unavailable",
            ) from exc
        except jwt.PyJWTError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"invalid token: {exc}") from exc

        # Prefer the tenant-stable object ID. App-scoped `sub` values can map
        # the same person to a different owner partition in another client.
        # In an external tenant a self-service sign-up account still receives a
        # stable `oid`, so a Gmail-backed user partitions exactly like any other.
        provider_user_id = claims.get("oid") or claims.get("sub")
        if not provider_user_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="token missing subject/oid claim")
        # Authorisation is deliberately *not* decided here. Any valid tenant
        # principal gets an identity; whether that identity may read or write
        # anything is settled against `app_users` in `auspex.api.access`.
        resolved_provider_user_id = str(provider_user_id)
        if (
            binding.legacy
            and resolved_provider_user_id
            == self._settings.owner_legacy_provider_user_id
            and self._settings.owner_provider_user_id
        ):
            resolved_provider_user_id = self._settings.owner_provider_user_id
        return AuthenticatedUser(
            user_id=compatible_user_id(resolved_provider_user_id),
            claims=claims,
            provider_user_id=resolved_provider_user_id,
            email=extract_email(claims),
            email_verified=claims.get("email_verified") is True
            or str(claims.get("email_verified", "")).lower() == "true",
            display_name=_first_claim(claims, NAME_CLAIMS),
        )


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
    return await asyncio.to_thread(get_validator().validate, token)
