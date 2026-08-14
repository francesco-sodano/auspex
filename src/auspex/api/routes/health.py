"""Authenticated API health/readiness endpoint, mounted at `/api/health`.

Distinct from `/healthz` (arc42 §7): this route lives under the `/api`
prefix and therefore requires a validated Entra token like every other
`/api/*` route — it answers "is the authenticated API surface healthy?",
not "is the process alive?".
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from auspex.api.auth import AuthenticatedUser, get_current_user

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(user: AuthenticatedUser = Depends(get_current_user)) -> dict:
    return {"status": "ok"}
