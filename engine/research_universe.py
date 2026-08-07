"""Deterministic research-universe membership independent of portfolio ownership."""

from __future__ import annotations

from dataclasses import dataclass


UNIVERSE_TIERS = {"held", "watchlist", "eligible", "excluded"}


@dataclass(frozen=True)
class ResearchSecurity:
    security_sk: int
    ticker: str
    is_active: bool
    is_resolved: bool
    is_price_covered: bool
    theme_ids: tuple[str, ...]
    is_held: bool = False
    is_watchlisted: bool = False
    is_excluded: bool = False


@dataclass(frozen=True)
class ResearchUniverseMember:
    security_sk: int
    ticker: str
    included: bool
    tier: str
    theme_ids: tuple[str, ...]
    reasons: tuple[str, ...]


def resolve_research_universe(
    securities: list[ResearchSecurity],
) -> list[ResearchUniverseMember]:
    security_keys = [security.security_sk for security in securities]
    if len(security_keys) != len(set(security_keys)):
        raise ValueError("research universe contains duplicate securities")
    members = []
    for security in sorted(securities, key=lambda row: (row.ticker.upper(), row.security_sk)):
        if security.security_sk <= 0 or not security.ticker.strip():
            raise ValueError("research universe security identity is incomplete")
        themes = tuple(sorted(set(theme.strip() for theme in security.theme_ids if theme.strip())))
        reasons = []
        if not security.is_active:
            reasons.append("inactive")
        if not security.is_resolved:
            reasons.append("unresolved")
        if not security.is_price_covered:
            reasons.append("missing_price_coverage")
        if not themes:
            reasons.append("missing_theme")
        if security.is_excluded:
            reasons.append("explicitly_excluded")

        if security.is_held:
            included = True
            tier = "held"
            reasons = [reason for reason in reasons if reason != "explicitly_excluded"]
            if security.is_excluded:
                reasons.append("held_override")
        elif security.is_excluded or any(
            reason in {"inactive", "unresolved", "missing_price_coverage", "missing_theme"}
            for reason in reasons
        ):
            included = False
            tier = "excluded"
        elif security.is_watchlisted:
            included = True
            tier = "watchlist"
        else:
            included = True
            tier = "eligible"
        members.append(ResearchUniverseMember(
            security_sk=security.security_sk,
            ticker=security.ticker.strip().upper(),
            included=included,
            tier=tier,
            theme_ids=themes,
            reasons=tuple(sorted(set(reasons))),
        ))
    return members