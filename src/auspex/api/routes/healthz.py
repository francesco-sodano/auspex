"""Unauthenticated liveness probe for Container Apps (arc42 §7).

`/healthz` is deliberately outside the `/api` prefix and carries no auth
dependency: Container Apps liveness/readiness probes cannot present an Entra
bearer token, and a liveness check must never depend on downstream identity
providers being reachable. It answers process-liveness only — no Cosmos/Blob/
Key Vault/Azure OpenAI calls — so a probe failure reliably means "this
process is not serving requests," nothing more.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["healthz"])


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}
