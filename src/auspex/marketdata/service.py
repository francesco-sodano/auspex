"""Market-data integrity orchestration (arc42 §5.3).

Ties the pure detectors/planners to storage:

1. read every bar of a security with an explicit partition query (quarantined
   bars included — they must be re-examined so they can be released),
2. plan the minimal set of derived-field rewrites and quarantine transitions,
3. write only the bars that actually changed,
4. persist an append-only, fingerprinted manifest revision in ``config_versions``.

The fingerprint makes the pass idempotent at the manifest level: a run whose
plan is byte-identical to the latest stored revision writes no new revision.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Protocol

from auspex.marketdata.policy import DEFAULT_POLICY, POLICY_VERSION, IntegrityPolicy
from auspex.marketdata.recompute import RecomputeTarget, targets_from_manifest
from auspex.marketdata.repair import SecurityRepairPlan, plan_security_repair
from auspex.models.common import sha256_hex, utc_now
from auspex.models.market import PriceBar
from auspex.models.market_integrity import (
    MANIFEST_CONFIG_TYPE,
    MarketDataRepairManifest,
    SecurityIntegrityReport,
)


class PriceIntegrityStore(Protocol):
    """Unfiltered access to price bars, including quarantined rows."""

    async def security_ids(self) -> list[str]: ...

    async def bars_for_security(self, security_id: str) -> list[PriceBar]: ...

    async def upsert_bar(self, bar: PriceBar) -> None: ...


class RepairManifestStore(Protocol):
    """Append-only manifest revisions."""

    async def latest(self) -> MarketDataRepairManifest | None: ...

    async def history(self, limit: int = 20) -> list[MarketDataRepairManifest]: ...

    async def upsert(self, manifest: MarketDataRepairManifest) -> None: ...


def plan_fingerprint(plans: Sequence[SecurityRepairPlan], policy_version: str) -> str:
    """Stable hash of what a pass *would* change, ignoring time and revision."""

    payload = {
        "policy_version": policy_version,
        "securities": [
            {
                "security_id": plan.security_id,
                "convention": plan.report.convention,
                "repairs": [
                    {
                        "session_date": repair.session_date.isoformat(),
                        "field": repair.field_name,
                        "repaired": repair.repaired,
                    }
                    for repair in plan.report.repairs
                ],
                "quarantined": [d.isoformat() for d in plan.report.quarantined_dates],
                "released": [d.isoformat() for d in plan.report.released_dates],
            }
            for plan in sorted(plans, key=lambda item: item.security_id)
            if plan.report.has_changes
        ],
    }
    return sha256_hex(json.dumps(payload, sort_keys=True, separators=(",", ":")))


class MarketDataIntegrityService:
    """Diagnose and idempotently repair the stored adjusted price series."""

    def __init__(
        self,
        prices: PriceIntegrityStore,
        manifests: RepairManifestStore,
        *,
        policy: IntegrityPolicy = DEFAULT_POLICY,
        clock: Callable[[], datetime] = utc_now,
        ticker_by_security: dict[str, str] | None = None,
    ) -> None:
        self._prices = prices
        self._manifests = manifests
        self._policy = policy
        self._clock = clock
        self._tickers = dict(ticker_by_security or {})

    @property
    def policy(self) -> IntegrityPolicy:
        return self._policy

    async def _resolve_security_ids(self, security_ids: Sequence[str] | None) -> list[str]:
        if security_ids:
            return sorted({sid for sid in security_ids if sid})
        return sorted(set(await self._prices.security_ids()))

    async def _build_plans(
        self, security_ids: Sequence[str], revision: int, now: datetime
    ) -> list[SecurityRepairPlan]:
        plans: list[SecurityRepairPlan] = []
        for security_id in security_ids:
            bars = await self._prices.bars_for_security(security_id)
            if not bars:
                plans.append(
                    SecurityRepairPlan(
                        security_id=security_id,
                        report=SecurityIntegrityReport(
                            security_id=security_id, ticker=self._tickers.get(security_id)
                        ),
                    )
                )
                continue
            plans.append(
                plan_security_repair(
                    security_id,
                    bars,
                    revision=revision,
                    now=now,
                    policy=self._policy,
                    ticker=self._tickers.get(security_id),
                )
            )
        return plans

    def _assemble(
        self,
        plans: Sequence[SecurityRepairPlan],
        *,
        revision: int,
        now: datetime,
        dry_run: bool,
    ) -> MarketDataRepairManifest:
        reports = [plan.report for plan in plans]
        return MarketDataRepairManifest(
            id=MarketDataRepairManifest.make_id(revision),
            config_type=MANIFEST_CONFIG_TYPE,
            revision=revision,
            created_at=now,
            fingerprint=plan_fingerprint(plans, POLICY_VERSION),
            policy_version=POLICY_VERSION,
            dry_run=dry_run,
            securities=reports,
            securities_examined=len(reports),
            bars_examined=sum(report.bars_examined for report in reports),
            bars_repaired=sum(len(report.repairs) for report in reports),
            bars_quarantined=sum(len(report.quarantined_dates) for report in reports),
            bars_released=sum(len(report.released_dates) for report in reports),
        )

    async def diagnose(
        self, security_ids: Sequence[str] | None = None
    ) -> MarketDataRepairManifest:
        """Report what a repair would change. Writes nothing."""

        now = self._clock()
        latest = await self._manifests.latest()
        revision = (latest.revision if latest else 0) + 1
        targets = await self._resolve_security_ids(security_ids)
        plans = await self._build_plans(targets, revision, now)
        return self._assemble(plans, revision=revision, now=now, dry_run=True)

    async def repair(
        self, security_ids: Sequence[str] | None = None, *, dry_run: bool = False
    ) -> MarketDataRepairManifest:
        """Apply repairs and quarantine transitions, then record a manifest revision.

        A pass whose plan matches the latest stored revision is a no-op: no bar
        is rewritten and no revision is appended.
        """

        now = self._clock()
        latest = await self._manifests.latest()
        revision = (latest.revision if latest else 0) + 1
        targets = await self._resolve_security_ids(security_ids)
        plans = await self._build_plans(targets, revision, now)
        manifest = self._assemble(plans, revision=revision, now=now, dry_run=dry_run)

        has_updates = any(plan.updates for plan in plans)
        if latest is not None and not latest.dry_run and not has_updates:
            return latest

        if dry_run:
            return manifest

        for plan in plans:
            for bar in plan.updates:
                await self._prices.upsert_bar(bar)

        if latest is None or latest.fingerprint != manifest.fingerprint:
            await self._manifests.upsert(manifest)
            return manifest
        return latest

    async def recompute_targets(
        self, manifest: MarketDataRepairManifest | None = None
    ) -> list[RecomputeTarget]:
        """Windows downstream consumers must recompute, without re-reading bars."""

        source = manifest or await self._manifests.latest()
        if source is None:
            return []
        return targets_from_manifest(source, policy=self._policy)
