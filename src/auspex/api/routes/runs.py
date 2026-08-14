"""Run manifest endpoint (arc42 §11 `GET /api/runs?limit=`, §6.1)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from auspex.api.auth import AuthenticatedUser, get_current_user
from auspex.api.deps import get_run_repo
from auspex.models.run import RunManifest
from auspex.persistence.repositories import CosmosRepository

router = APIRouter(prefix="/runs", tags=["runs"])


@router.get("", response_model=list[RunManifest])
async def list_runs(
    limit: int = Query(default=20, ge=1, le=200),
    user: AuthenticatedUser = Depends(get_current_user),
    repo: CosmosRepository = Depends(get_run_repo),
) -> list[RunManifest]:
    return await repo.query(
        query="SELECT TOP @limit * FROM c ORDER BY c.started_at DESC",
        parameters=[{"name": "@limit", "value": limit}],
        partition_key=None,
    )
