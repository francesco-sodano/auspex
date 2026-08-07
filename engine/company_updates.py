"""Deterministic planning for incremental company and theme updates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .company_package import (
    CompanyOpportunityPackage,
    package_changed,
    package_fingerprint,
)


@dataclass(frozen=True)
class DirtyCompanyChange:
    change_id: str
    security_sk: int
    source_class: str
    source_id: str
    knowledge_date: date


@dataclass(frozen=True)
class CompanyUpdate:
    security_sk: int
    change_ids: tuple[str, ...]
    source_classes: tuple[str, ...]
    max_knowledge_date: date
    theme_ids: tuple[str, ...]


@dataclass(frozen=True)
class IncrementalUpdatePlan:
    companies: tuple[CompanyUpdate, ...]
    dirty_theme_ids: tuple[str, ...]
    unclassified_security_sks: tuple[int, ...]

    @property
    def is_noop(self) -> bool:
        return not self.companies


@dataclass(frozen=True)
class PackagePublicationDecision:
    package_fingerprint: str
    publish_revision: bool
    complete_change_ids: tuple[str, ...]


def plan_company_updates(
    changes: list[DirtyCompanyChange],
    *,
    theme_ids_by_security: dict[int, tuple[str, ...]],
) -> IncrementalUpdatePlan:
    change_ids = [change.change_id for change in changes]
    if len(change_ids) != len(set(change_ids)):
        raise ValueError("incremental update contains duplicate change ids")
    grouped: dict[int, list[DirtyCompanyChange]] = {}
    for change in changes:
        if (
            change.security_sk <= 0
            or not change.change_id.strip()
            or not change.source_class.strip()
            or not change.source_id.strip()
        ):
            raise ValueError("dirty company change identity is incomplete")
        grouped.setdefault(change.security_sk, []).append(change)

    company_updates = []
    dirty_themes = set()
    unclassified = []
    for security_sk in sorted(grouped):
        company_changes = grouped[security_sk]
        themes = tuple(sorted(set(
            theme.strip()
            for theme in theme_ids_by_security.get(security_sk, ())
            if theme.strip()
        )))
        if themes:
            dirty_themes.update(themes)
        else:
            unclassified.append(security_sk)
        company_updates.append(CompanyUpdate(
            security_sk=security_sk,
            change_ids=tuple(sorted(change.change_id for change in company_changes)),
            source_classes=tuple(sorted(set(
                change.source_class for change in company_changes
            ))),
            max_knowledge_date=max(
                change.knowledge_date for change in company_changes
            ),
            theme_ids=themes,
        ))
    return IncrementalUpdatePlan(
        companies=tuple(company_updates),
        dirty_theme_ids=tuple(sorted(dirty_themes)),
        unclassified_security_sks=tuple(unclassified),
    )


def decide_package_publication(
    previous: CompanyOpportunityPackage | None,
    current: CompanyOpportunityPackage,
    *,
    change_ids: tuple[str, ...],
) -> PackagePublicationDecision:
    if not change_ids or len(change_ids) != len(set(change_ids)):
        raise ValueError("package publication requires unique change ids")
    fingerprint = package_fingerprint(current)
    return PackagePublicationDecision(
        package_fingerprint=fingerprint,
        publish_revision=package_changed(previous, current),
        complete_change_ids=tuple(sorted(change_ids)),
    )