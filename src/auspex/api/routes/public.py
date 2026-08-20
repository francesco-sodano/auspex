"""Unauthenticated runtime configuration required before MSAL can initialize.

The SPA cannot be built with tenant details baked in — the same image is
deployed against different tenants — so it fetches them here at startup.

This endpoint is deliberately tenant-type aware. Against a **workforce**
tenant the authority is a `login.microsoftonline.com` URL, which MSAL trusts
implicitly. Against an **external** tenant (Microsoft Entra External ID /
CIAM), which is what allows sign-up with a personal Gmail or Outlook address
through a sign-up/sign-in user flow, the authority is
`https://<subdomain>.ciamlogin.com/...` — a host MSAL rejects unless it is
declared in `knownAuthorities`. Serving that host from here means switching
tenant types is a configuration change, never a frontend change.
"""

from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException

from auspex.settings import Settings, get_settings

router = APIRouter(tags=["public"])

#: Hosts MSAL already trusts. Anything else — notably `*.ciamlogin.com` — has
#: to be advertised explicitly or the SPA fails to initialise.
IMPLICITLY_TRUSTED_HOSTS = ("login.microsoftonline.com",)


def known_authorities(settings: Settings) -> list[str]:
    """Authority hosts the SPA must declare to MSAL.

    Derived from the configured authority so an operator cannot forget it,
    with an explicit override for unusual topologies.
    """

    configured = (settings.entra_known_authority or "").strip()
    if configured:
        return [configured]
    host = urlparse(settings.entra_authority).netloc
    if not host or host in IMPLICITLY_TRUSTED_HOSTS:
        return []
    return [host]


@router.get("/auth-config.json", include_in_schema=False)
async def auth_config() -> dict:
    settings = get_settings()
    if not settings.entra_audience or not settings.entra_authority:
        raise HTTPException(
            status_code=503,
            detail="Microsoft Entra authentication is not configured",
        )
    return {
        "client_id": settings.entra_audience,
        "authority": settings.entra_authority.rstrip("/"),
        # Empty for a workforce tenant; the ciamlogin.com host for an external
        # tenant. MSAL accepts an empty array, so the SPA can pass it through
        # unconditionally.
        "known_authorities": known_authorities(settings),
        "tenant_id": settings.entra_tenant_id,
        # Scope the SPA should request for the API audience. Empty means "use
        # the default", which is correct for a single-registration deployment.
        "api_scope": settings.entra_api_scope,
    }
