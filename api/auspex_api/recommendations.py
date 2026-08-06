from decimal import Decimal
from typing import Protocol, TYPE_CHECKING

from .recommender.policy import (
    CandidateSignal,
    FinancingPolicy,
    PortfolioContext,
    build_recommendations,
    financing_policy_from_environment,
    recommendation_payload,
)

if TYPE_CHECKING:
    from azure.cosmos import ContainerProxy


class OpportunitySignalRepository(Protocol):
    def list_current(self) -> list[dict]: ...


class InMemoryOpportunitySignalRepository:
    def __init__(self, signals: list[dict] | None = None) -> None:
        self._signals = list(signals or [])

    def list_current(self) -> list[dict]:
        return list(self._signals)


class CosmosOpportunitySignalRepository:
    def __init__(self, container: "ContainerProxy") -> None:
        self._container = container

    def list_current(self) -> list[dict]:
        return list(self._container.query_items(
            query="SELECT * FROM c WHERE c.kind = 'opportunity_score'",
            enable_cross_partition_query=True,
        ))


class RecommendationService:
    def __init__(
        self,
        identity,
        portfolio,
        signals: OpportunitySignalRepository,
        financing_policy: FinancingPolicy | None = None,
    ) -> None:
        self._identity = identity
        self._portfolio = portfolio
        self._signals = signals
        self._financing_policy = financing_policy or financing_policy_from_environment()

    def recommendations(self, principal_header) -> dict:
        user = self._identity.product_user(principal_header)
        summary = self._portfolio.portfolio_summary(principal_header)
        if summary["status"] not in {"ready", "stale"}:
            return {
                "status": "withheld",
                "as_of": summary.get("valuation_as_of"),
                "risk_profile": user.risk_profile,
                "base_currency": user.base_currency,
                "reasons": ["portfolio_valuation_incomplete"],
                "recommendations": [],
            }
        signal_rows = self._signals.list_current()
        if not signal_rows:
            return {
                "status": "withheld",
                "as_of": summary.get("valuation_as_of"),
                "risk_profile": user.risk_profile,
                "base_currency": user.base_currency,
                "reasons": ["opportunity_scores_unavailable"],
                "recommendations": [],
            }
        as_of = max(str(row["as_of"]) for row in signal_rows)
        current_signals = [row for row in signal_rows if str(row["as_of"]) == as_of]
        signals_by_security = {
            int(row["security_sk"]): row
            for row in current_signals
        }
        holdings = {
            int(row["security_sk"]): row
            for row in summary["holdings"]
            if row.get("security_sk") is not None
        }
        candidates = []
        signal_security_sks = set()
        for row in current_signals:
            security_sk = int(row["security_sk"])
            signal_security_sks.add(security_sk)
            holding = holdings.get(security_sk, {})
            candidates.append(CandidateSignal(
                security_sk=security_sk,
                ticker=str(row["ticker"]).upper(),
                opportunity_score=Decimal(str(row["opportunity_score"])),
                opportunity_score_raw=(
                    Decimal(str(row["opportunity_score_raw"]))
                    if row.get("opportunity_score_raw") is not None
                    else None
                ),
                coverage_status=str(row["coverage_status"]),
                current_value_base=Decimal(str(holding.get("market_value_base") or "0")),
                current_weight=Decimal(str(holding.get("weight") or "0")),
                country=row.get("country") or holding.get("country"),
                spread_bps=Decimal(str(row.get("spread_bps") or "5")),
                theme_id=row.get("theme_id"),
                coverage_reasons=tuple(row.get("coverage_reasons") or ()),
                financing_record_available=(
                    str(row.get("financing_coverage_status") or "").upper() == "READY"
                ),
                diluted_share_growth_yoy=(
                    Decimal(str(row["diluted_share_growth_yoy"]))
                    if row.get("diluted_share_growth_yoy") is not None else None
                ),
                cash_runway_years=(
                    Decimal(str(row["cash_runway_years"]))
                    if row.get("cash_runway_years") is not None else None
                ),
                is_burning_cash=row.get("is_burning_cash"),
                days_since_shelf_filing=(
                    int(row["days_since_shelf_filing"])
                    if row.get("days_since_shelf_filing") is not None else None
                ),
                shelf_form=row.get("shelf_form"),
            ))
        for security_sk, holding in holdings.items():
            if security_sk in signal_security_sks:
                continue
            candidates.append(CandidateSignal(
                security_sk=security_sk,
                ticker=str(holding["ticker"]).upper(),
                opportunity_score=Decimal("0"),
                coverage_status="WITHHELD",
                current_value_base=Decimal(str(holding.get("market_value_base") or "0")),
                current_weight=Decimal(str(holding.get("weight") or "0")),
                country=holding.get("country"),
                theme_id=holding.get("theme_id"),
                coverage_reasons=("missing:opportunity_score",),
            ))
        trade_count = self._portfolio.annual_trade_count(
            principal_header,
            int(as_of[:4]),
        )
        recommendations = build_recommendations(
            PortfolioContext(
                total_value_base=Decimal(summary["total_value_base"]),
                cash_base=Decimal(summary["total_cash_base"]),
                risk_profile=user.risk_profile,
                base_currency=user.base_currency,
                annual_trade_count=trade_count,
                financing_policy=self._financing_policy,
            ),
            candidates,
            as_of=as_of,
        )
        recommendation_rows = []
        for recommendation in recommendations:
            if recommendation.action == "HOLD" and recommendation.security_sk not in holdings:
                continue
            payload = recommendation_payload(recommendation)
            signal = signals_by_security.get(recommendation.security_sk)
            holding = holdings.get(recommendation.security_sk, {})
            payload["opportunity_score"] = (
                str(signal.get("opportunity_score")) if signal else None
            )
            payload["opportunity_score_raw"] = (
                str(signal.get("opportunity_score_raw"))
                if signal and signal.get("opportunity_score_raw") is not None
                else None
            )
            payload["candidate_count"] = (
                int(signal["candidate_count"])
                if signal and signal.get("candidate_count") is not None
                else None
            )
            payload["classification_provenance"] = (
                signal.get("classification_provenance") if signal else None
            )
            payload["theme_id"] = (
                signal.get("theme_id") if signal else holding.get("theme_id")
            )
            payload["coverage_status"] = (
                signal.get("coverage_status") if signal else "WITHHELD"
            )
            payload["coverage_reasons"] = list(
                signal.get("coverage_reasons") or ["missing:opportunity_score"]
            ) if signal else ["missing:opportunity_score"]
            payload["attribution"] = list(signal.get("attribution") or []) if signal else []
            recommendation_rows.append(payload)
        recommendation_rows.sort(key=lambda row: (
            row["action"] == "HOLD",
            -abs(Decimal(str(row["suggested_amount_base"]))),
            row["ticker"],
        ))
        return {
            "status": summary["status"],
            "as_of": as_of,
            "risk_profile": user.risk_profile,
            "base_currency": user.base_currency,
            "reasons": [],
            "recommendations": recommendation_rows,
            "disclaimer": "Research only; not financial or tax advice. You decide and execute.",
        }
