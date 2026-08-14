"""Unauthenticated runtime configuration required before MSAL can initialize."""

from fastapi import APIRouter, HTTPException

from auspex.settings import get_settings

router = APIRouter(tags=["public"])


@router.get("/auth-config.json", include_in_schema=False)
async def auth_config() -> dict[str, str]:
    settings = get_settings()
    if not settings.entra_audience or not settings.entra_authority:
        raise HTTPException(
            status_code=503,
            detail="Microsoft Entra authentication is not configured",
        )
    return {
        "client_id": settings.entra_audience,
        "authority": settings.entra_authority.rstrip("/"),
    }
