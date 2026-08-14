"""FastAPI application factory (arc42 §7 "app-auspex-api").

Route surface:
- ``/healthz`` — unauthenticated liveness-only probe for Container Apps
  (arc42 §7 ingress/probes). No auth, no downstream dependency calls.
- ``/api/*`` — every route, including ``/api/health``, requires a validated
  Entra token (arc42 F-16). Enforced at the router-group level via
  ``dependencies=[Depends(get_current_user)]`` so a future route added under
  ``/api`` cannot accidentally ship unauthenticated.
- everything else — the compiled React 18/Vite SPA (`web/dist`), served from
  this same process so the single production container needs no separate
  static host (arc42 §7 deployment view). See :mod:`auspex.api.static`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI

from auspex.api.auth import get_current_user
from auspex.api.routes import (
    account,
    briefing,
    conversation,
    documents,
    health,
    healthz,
    performance,
    portfolio,
    public,
    recommendations,
    runs,
    scores,
    securities,
)
from auspex.api.static import mount_spa


def create_app() -> FastAPI:
    app = FastAPI(
        title="Auspex API",
        version="4.0.0",
        description="Personal AI financial research assistant — backend API",
    )

    # Unauthenticated liveness-only probe (arc42 §7 Container Apps probes).
    app.include_router(healthz.router)
    app.include_router(public.router)

    # Every /api/* route requires a validated Entra token.
    api_router = APIRouter(prefix="/api", dependencies=[Depends(get_current_user)])
    api_router.include_router(health.router)
    api_router.include_router(account.router)
    api_router.include_router(scores.router)
    api_router.include_router(recommendations.router)
    api_router.include_router(portfolio.router)
    api_router.include_router(performance.router)
    api_router.include_router(conversation.router)
    api_router.include_router(briefing.router)
    api_router.include_router(documents.router)
    api_router.include_router(runs.router)
    api_router.include_router(securities.router)
    app.include_router(api_router)

    # Compiled SPA fallback (no-op if `web/dist` was not built, e.g. tests/dev).
    # Registered last so `/healthz` and `/api/*` above always match first.
    mount_spa(app)

    return app


app = create_app()
