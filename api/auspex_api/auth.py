import base64
import binascii
import json

from .models import AuthenticatedPrincipal


class AuthenticationError(ValueError):
    pass


def parse_swa_principal(encoded_principal: str | None) -> AuthenticatedPrincipal:
    if not encoded_principal:
        raise AuthenticationError("SWA client principal is required")
    try:
        payload = json.loads(
            base64.b64decode(encoded_principal, validate=True).decode("utf-8")
        )
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthenticationError("SWA client principal is invalid") from exc
    if not isinstance(payload, dict):
        raise AuthenticationError("SWA client principal is invalid")

    identity_provider = str(payload.get("identityProvider") or "").strip().lower()
    if identity_provider != "aad":
        raise AuthenticationError("Only Microsoft personal accounts are supported")
    provider_user_id = str(payload.get("userId") or "").strip()
    if not provider_user_id:
        raise AuthenticationError("SWA client principal has no stable user ID")
    roles = frozenset(
        str(role).strip().lower()
        for role in payload.get("userRoles") or []
        if str(role).strip()
    )
    if "authenticated" not in roles:
        raise AuthenticationError("SWA principal is not authenticated")
    return AuthenticatedPrincipal(
        identity_provider=identity_provider,
        provider_user_id=provider_user_id,
        user_details=str(payload.get("userDetails") or "").strip() or None,
        swa_roles=roles,
    )