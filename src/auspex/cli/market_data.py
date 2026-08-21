"""``market-data-*`` CLI commands (arc42 §5.3).

Operator entry points for the market-data integrity workstream:

``market-data-diagnose``
    Read-only. Reports duplicate bars, impossible values, implausible jumps,
    split-factor discontinuities and forward-return anomalies. Writes nothing.

``market-data-repair``
    Idempotently rebuilds the *derived* adjusted series from authoritative
    split/dividend/raw fields, quarantines what it cannot justify, releases
    bars that are clean again, and appends a versioned manifest revision.

Raw provider observations are never mutated or deleted by either command.
"""

from __future__ import annotations

import json
import logging

from auspex.models.market_integrity import IntegritySeverity, MarketDataRepairManifest

logger = logging.getLogger(__name__)


def _resolve_security_ids(universe, tickers: list[str] | None) -> tuple[list[str], dict[str, str]]:
    """Map ``--ticker`` values onto universe security ids (case-insensitive)."""

    ticker_by_security = {
        security.id: security.ticker for security in universe.securities
    }
    if not tickers:
        return sorted(ticker_by_security), ticker_by_security

    wanted = {ticker.strip().upper() for ticker in tickers if ticker.strip()}
    selected = [
        security_id
        for security_id, ticker in ticker_by_security.items()
        if ticker.upper() in wanted
    ]
    unknown = wanted - {ticker_by_security[sid].upper() for sid in selected}
    if unknown:
        raise SystemExit(f"unknown ticker(s): {', '.join(sorted(unknown))}")
    return sorted(selected), ticker_by_security


async def _build_service(tickers: list[str] | None):
    from auspex.config import load_universe
    from auspex.marketdata.service import MarketDataIntegrityService
    from auspex.persistence.cosmos_client import get_cosmos_context
    from auspex.persistence.repositories import (
        CosmosPriceIntegrityStore,
        CosmosRepairManifestStore,
    )

    universe = load_universe()
    security_ids, ticker_by_security = _resolve_security_ids(universe, tickers)
    cosmos = get_cosmos_context()
    service = MarketDataIntegrityService(
        CosmosPriceIntegrityStore(cosmos),
        CosmosRepairManifestStore(cosmos),
        ticker_by_security=ticker_by_security,
    )
    return service, security_ids, cosmos


def _summarise(manifest: MarketDataRepairManifest) -> dict[str, object]:
    errors = sum(report.error_count for report in manifest.securities)
    warnings = sum(
        1
        for report in manifest.securities
        for finding in report.findings
        if finding.severity is IntegritySeverity.WARNING
    )
    return {
        "revision": manifest.revision,
        "fingerprint": manifest.fingerprint,
        "policy_version": manifest.policy_version,
        "dry_run": manifest.dry_run,
        "securities_examined": manifest.securities_examined,
        "bars_examined": manifest.bars_examined,
        "bars_repaired": manifest.bars_repaired,
        "bars_quarantined": manifest.bars_quarantined,
        "bars_released": manifest.bars_released,
        "error_findings": errors,
        "warning_findings": warnings,
    }


def _print_report(manifest: MarketDataRepairManifest, *, as_json: bool) -> None:
    if as_json:
        payload = _summarise(manifest)
        payload["securities"] = [
            report.model_dump(mode="json")
            for report in manifest.securities
            if report.findings or report.has_changes
        ]
        payload["recompute_targets"] = [
            target.as_dict() for target in _recompute_targets(manifest)
        ]
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    summary = _summarise(manifest)
    print(
        "market-data revision={revision} policy={policy_version} dry_run={dry_run}".format(**summary)
    )
    print(
        "  securities={securities_examined} bars={bars_examined} "
        "repaired={bars_repaired} quarantined={bars_quarantined} "
        "released={bars_released}".format(**summary)
    )
    print(
        "  findings: errors={error_findings} warnings={warning_findings}".format(**summary)
    )
    for report in manifest.securities:
        if not (report.findings or report.has_changes):
            continue
        label = report.ticker or report.security_id
        print(
            f"  [{label}] convention={report.convention} bars={report.bars_examined} "
            f"repairs={len(report.repairs)} quarantined={len(report.quarantined_dates)} "
            f"released={len(report.released_dates)}"
        )
        for finding in report.findings[:20]:
            session = finding.session_date.isoformat() if finding.session_date else "-"
            print(f"      {finding.severity.value:<7} {session} {finding.code.value}: {finding.detail}")
        if len(report.findings) > 20:
            print(f"      ... {len(report.findings) - 20} more finding(s)")
    for target in _recompute_targets(manifest):
        print(
            f"  recompute {target.security_id} {target.start_date.isoformat()}"
            f"..{target.end_date.isoformat()} ({', '.join(target.reasons)})"
        )


def _recompute_targets(manifest: MarketDataRepairManifest):
    from auspex.marketdata.recompute import targets_from_manifest

    return targets_from_manifest(manifest)


async def market_data_diagnose_command(
    tickers: list[str] | None = None, *, as_json: bool = False
) -> int:
    """Report integrity findings. Exit code 1 when any ERROR finding exists."""

    service, security_ids, cosmos = await _build_service(tickers)
    try:
        manifest = await service.diagnose(security_ids)
        _print_report(manifest, as_json=as_json)
        return (
            1
            if any(report.error_count for report in manifest.securities)
            else 0
        )
    finally:
        await cosmos.aclose()


async def market_data_repair_command(
    tickers: list[str] | None = None, *, dry_run: bool = False, as_json: bool = False
) -> int:
    """Apply idempotent repairs and record a manifest revision."""

    service, security_ids, cosmos = await _build_service(tickers)
    try:
        manifest = await service.repair(
            security_ids,
            dry_run=dry_run,
        )
        _print_report(manifest, as_json=as_json)
        return 0
    finally:
        await cosmos.aclose()
