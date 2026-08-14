"""Score snapshot endpoints (arc42 §5.10 "score_snapshot", "leg_history")."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from auspex.api.auth import AuthenticatedUser, get_current_user
from auspex.api.deps import get_score_repo, get_universe
from auspex.config.loader import Universe
from auspex.models.scoring import ScoreSnapshot
from auspex.persistence.repositories import CosmosRepository

router = APIRouter(prefix="/scores", tags=["scores"])


@router.get("/{ticker}/{as_of_date}", response_model=ScoreSnapshot)
async def get_score(
    ticker: str,
    as_of_date: str,
    user: AuthenticatedUser = Depends(get_current_user),
    universe: Universe = Depends(get_universe),
    repo: CosmosRepository = Depends(get_score_repo),
) -> ScoreSnapshot:
    security = universe.by_ticker().get(ticker.upper())
    if security is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown ticker {ticker!r}")
    score = await repo.get(f"{security.id}:{as_of_date}", partition_key=security.id)
    if score is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no score for this security/date")
    return score
