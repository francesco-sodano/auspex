"""Cosmos-backed retrieval adapters for the grounded conversational assistant."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from functools import lru_cache

from auspex.api.deps import (
    get_fundamental_repo,
    get_performance_repo,
    get_portfolio_projection_repo,
    get_price_sink,
    get_recommendation_repo,
    get_score_repo,
    get_universe,
)
from auspex.api.repos import get_digest_repo, get_document_repo, get_leg_change_repo
from auspex.api.routes.securities import _fundamentals
from auspex.assistant.retrieval import DataClassRepos, RetrievalFetcher, RetrievedItem
from auspex.config.loader import Universe
from auspex.models.common import AuspexModel, utc_now
from auspex.models.conversation import RetrievalPlan
from auspex.persistence.blob_client import get_blob_context
from auspex.persistence.repositories import CosmosRepository


def _latest_date_rows(rows: list[AuspexModel], field: str) -> list[AuspexModel]:
    if not rows:
        return []
    latest = max(getattr(row, field) for row in rows)
    return [row for row in rows if getattr(row, field) == latest]


async def _latest_available_date(
    repo: CosmosRepository,
    field: str,
    end: date,
    *,
    before: date | None = None,
) -> date | None:
    operator = "<" if before is not None else "<="
    cutoff = before or end
    rows = await repo.raw_query(
        (
            f"SELECT TOP 1 VALUE c.{field} FROM c "
            f"WHERE c.{field} {operator} @cutoff ORDER BY c.{field} DESC"
        ),
        parameters=[{"name": "@cutoff", "value": cutoff.isoformat()}],
    )
    return date.fromisoformat(str(rows[0])) if rows else None


class ChatGrounding:
    def __init__(self, universe: Universe) -> None:
        self._universe = universe
        self._security_by_id = universe.by_id()
        self._security_by_ticker = universe.by_ticker()

    def _security_ids(self, plan: RetrievalPlan) -> set[str]:
        ids: set[str] = set()
        for value in plan.securities:
            normalized = value.strip().upper()
            if normalized in self._security_by_ticker:
                ids.add(self._security_by_ticker[normalized].id)
                continue
            if value in self._security_by_id:
                ids.add(value)
                continue
            for security in self._universe.securities:
                if security.name.upper() == normalized:
                    ids.add(security.id)
                    break
        return ids

    def _content(self, row: AuspexModel) -> dict:
        content = row.model_dump(mode="json")
        security_id = content.get("security_id")
        security = self._security_by_id.get(str(security_id))
        if security is not None:
            content["ticker"] = security.ticker
            content["company_name"] = security.name
        return content

    @staticmethod
    def _item(
        data_class: str,
        row: AuspexModel,
        content: dict,
        rank: int,
        *,
        document_id: str | None = None,
        source_url: str | None = None,
    ) -> RetrievedItem:
        ticker = str(content.get("ticker") or content.get("security_id") or "portfolio")
        as_of = str(
            content.get("as_of_date")
            or content.get("filed")
            or content.get("knowledge_date")
            or "current"
        )
        if data_class == "leg_changes":
            citation_id = f"leg:{ticker}:{content.get('leg', 'change')}:{as_of}"
        elif data_class == "recommendations":
            citation_id = f"recommendation:{ticker}:{as_of}"
        elif data_class == "portfolio_state":
            citation_id = f"portfolio:{as_of}"
        elif data_class == "performance":
            citation_id = f"performance:{content.get('metric_type', rank)}:{as_of}"
        else:
            citation_id = f"{data_class}:{ticker}:{as_of}"
        return RetrievedItem(
            data_class=data_class,
            content=content,
            security_id=getattr(row, "security_id", None),
            document_id=document_id or citation_id,
            source_url=source_url,
            retrieved_at=utc_now(),
            relevance_rank=rank,
        )

    async def score_snapshot(self, plan: RetrievalPlan, _user_id: str) -> list[RetrievedItem]:
        end = plan.date_range_end or date.today()
        ids = sorted(self._security_ids(plan))
        repo = get_score_repo()
        latest = await _latest_available_date(repo, "as_of_date", end)
        if latest is None:
            return []
        prior = await _latest_available_date(repo, "as_of_date", end, before=latest)
        dates = [latest.isoformat()]
        if prior is not None:
            dates.append(prior.isoformat())
        query = "SELECT * FROM c WHERE ARRAY_CONTAINS(@dates, c.as_of_date)"
        parameters: list[dict] = [{"name": "@dates", "value": dates}]
        if ids:
            query += " AND ARRAY_CONTAINS(@security_ids, c.security_id)"
            parameters.append({"name": "@security_ids", "value": ids})
        rows = await repo.query(query=query, parameters=parameters)
        selected = sorted(rows, key=lambda row: row.as_of_date, reverse=True)
        if not ids and prior is not None:
            latest_by_security = {
                row.security_id: row
                for row in selected
                if row.as_of_date == latest and row.percentile is not None
            }
            prior_by_security = {
                row.security_id: row
                for row in selected
                if row.as_of_date == prior and row.percentile is not None
            }
            changes = [
                (security_id, current.percentile - prior_by_security[security_id].percentile)
                for security_id, current in latest_by_security.items()
                if security_id in prior_by_security
            ]
            mover_ids = {
                security_id
                for security_id, _change in [
                    *sorted(changes, key=lambda item: item[1], reverse=True)[:3],
                    *sorted(changes, key=lambda item: item[1])[:3],
                ]
            }
            selected = [row for row in selected if row.security_id in mover_ids]
        return [
            self._item("score_snapshot", row, self._content(row), rank)
            for rank, row in enumerate(selected)
        ]

    async def leg_history(self, plan: RetrievalPlan, user_id: str) -> list[RetrievedItem]:
        return await self.score_snapshot(plan, user_id)

    async def narrative_history(self, plan: RetrievalPlan, user_id: str) -> list[RetrievedItem]:
        rows = await self.score_snapshot(plan, user_id)
        return [
            RetrievedItem(
                **{
                    **item.__dict__,
                    "data_class": "narrative_history",
                    "content": {
                        key: value
                        for key, value in item.content.items()
                        if key
                        in {
                            "security_id",
                            "ticker",
                            "company_name",
                            "as_of_date",
                            "percentile",
                            "direction",
                            "narrative",
                            "max_knowledge_date",
                        }
                    },
                }
            )
            for item in rows
        ]

    async def leg_changes(self, plan: RetrievalPlan, _user_id: str) -> list[RetrievedItem]:
        end = plan.date_range_end or date.today()
        ids = sorted(self._security_ids(plan))
        repo = get_leg_change_repo()
        latest = await _latest_available_date(repo, "as_of_date", end)
        if latest is None:
            return []
        query = "SELECT * FROM c WHERE c.as_of_date=@latest"
        parameters: list[dict] = [{"name": "@latest", "value": latest.isoformat()}]
        if ids:
            query += " AND ARRAY_CONTAINS(@security_ids, c.security_id)"
            parameters.append({"name": "@security_ids", "value": ids})
        selected = await repo.query(query=query, parameters=parameters)
        if not ids:
            selected = sorted(
                selected,
                key=lambda row: abs(float(row.delta_z or "0")),
                reverse=True,
            )[:18]
        return [
            self._item("leg_changes", row, self._content(row), rank)
            for rank, row in enumerate(selected)
        ]

    async def recommendations(self, plan: RetrievalPlan, user_id: str) -> list[RetrievedItem]:
        end = plan.date_range_end or date.today()
        rows = await get_recommendation_repo().query(
            query=(
                "SELECT TOP 200 * FROM c WHERE c.user_id=@user_id "
                "AND c.as_of_date<=@end ORDER BY c.as_of_date DESC"
            ),
            parameters=[
                {"name": "@user_id", "value": user_id},
                {"name": "@end", "value": end.isoformat()},
            ],
            partition_key=user_id,
        )
        selected = _latest_date_rows(rows, "as_of_date")
        requested_ids = self._security_ids(plan)
        if requested_ids:
            selected = [row for row in selected if row.security_id in requested_ids]
        else:
            actionable = [
                row for row in selected if not row.action.value.startswith("HOLD")
            ]
            blocked_candidates = sorted(
                (
                    row
                    for row in selected
                    if row.action.value.startswith("HOLD")
                    and float(row.current_weight_pct or "0") == 0
                ),
                key=lambda row: float(row.target_weight_pct or "0"),
                reverse=True,
            )[:5]
            selected = list(
                {
                    row.id: row
                    for row in [*actionable, *blocked_candidates]
                }.values()
            )
        return [
            self._item(
                "recommendations",
                row,
                {
                    **self._content(row),
                    "action_is_final": True,
                    "gate_trace_note": (
                        "The gate trace includes earlier action branches. Failed ADD/BUY gates "
                        "do not block a final TRIM or SELL action."
                    ),
                },
                rank,
            )
            for rank, row in enumerate(selected)
        ]

    async def portfolio_state(self, plan: RetrievalPlan, user_id: str) -> list[RetrievedItem]:
        end = plan.date_range_end or date.today()
        rows = await get_portfolio_projection_repo().query(
            query=(
                "SELECT TOP 1 * FROM c WHERE c.user_id=@user_id "
                "AND c.as_of_date<=@end ORDER BY c.as_of_date DESC"
            ),
            parameters=[
                {"name": "@user_id", "value": user_id},
                {"name": "@end", "value": end.isoformat()},
            ],
            partition_key=user_id,
        )
        return [
            self._item("portfolio_state", row, row.model_dump(mode="json"), rank)
            for rank, row in enumerate(rows)
        ]

    async def fundamentals(self, plan: RetrievalPlan, _user_id: str) -> list[RetrievedItem]:
        ids = sorted(self._security_ids(plan))
        if not ids:
            return []
        end = plan.date_range_end or date.today()
        items: list[RetrievedItem] = []
        for security_id in ids:
            rows = await get_fundamental_repo().query(
                query=(
                    "SELECT TOP 12 * FROM c WHERE c.security_id=@security_id "
                    "AND c.filed<=@end ORDER BY c.filed DESC"
                ),
                parameters=[
                    {"name": "@security_id", "value": security_id},
                    {"name": "@end", "value": end.isoformat()},
                ],
                partition_key=security_id,
            )
            if not rows:
                continue
            prices = await get_price_sink().history_as_of(security_id, end, 1)
            current_price = (
                Decimal(prices[-1].close_adjusted) if prices else None
            )
            security = self._security_by_id[security_id]
            items.append(
                self._item(
                    "fundamentals",
                    rows[0],
                    {
                        "security_id": security_id,
                        "ticker": security.ticker,
                        "company_name": security.name,
                        "as_of_date": end.isoformat(),
                        "metrics": [
                            metric.model_dump(mode="json")
                            for metric in _fundamentals(
                                rows,
                                end,
                                current_price,
                            )
                        ],
                    },
                    len(items),
                )
            )
        return items

    async def document_digest(self, plan: RetrievalPlan, _user_id: str) -> list[RetrievedItem]:
        ids = sorted(self._security_ids(plan))
        if not ids:
            return []
        documents = []
        digests = []
        for security_id in ids:
            security_documents = await get_document_repo().query(
                query=(
                    "SELECT TOP 50 * FROM c WHERE c.security_id=@security_id "
                    "ORDER BY c.knowledge_date DESC"
                ),
                parameters=[{"name": "@security_id", "value": security_id}],
                partition_key=security_id,
            )
            security = self._security_by_id[security_id]
            title_terms = {
                security.ticker.lower(),
                *[
                    token
                    for token in re.sub(
                        r"[^a-z0-9]+",
                        " ",
                        security.name.lower(),
                    ).split()
                    if len(token) >= 4
                ][:2],
            }
            relevant_news = [
                document
                for document in security_documents
                if document.document_type.value == "NEWS"
                and any(
                    re.search(
                        rf"\b{re.escape(term)}\b",
                        document.title or "",
                        flags=re.IGNORECASE,
                    )
                    for term in title_terms
                )
            ][:3]
            non_news = [
                document
                for document in security_documents
                if document.document_type.value != "NEWS"
            ][:8]
            documents.extend([*relevant_news, *non_news])
            digests.extend(
                await get_digest_repo().query(
                    query="SELECT * FROM c WHERE c.security_id=@security_id",
                    parameters=[{"name": "@security_id", "value": security_id}],
                    partition_key=security_id,
                )
            )
        digest_by_document_id = {row.document_id: row for row in digests}
        items: list[RetrievedItem] = []
        for rank, document in enumerate(documents[:30]):
            digest = digest_by_document_id.get(document.id)
            content = {
                "security_id": document.security_id,
                "ticker": self._security_by_id[document.security_id].ticker,
                "company_name": self._security_by_id[document.security_id].name,
                "document_id": document.id,
                "document_type": document.document_type.value,
                "form_type": document.form_type,
                "knowledge_date": document.knowledge_date.isoformat(),
                "headline": digest.headline if digest is not None else document.title,
                "digest": (
                    digest.digest
                    if digest is not None
                    else document.content_excerpt
                ),
                "comparative": (
                    digest.comparative.model_dump(mode="json")
                    if digest is not None and digest.comparative is not None
                    else None
                ),
                "source_url": document.url,
            }
            items.append(
                self._item(
                    "document_digest",
                    document,
                    content,
                    rank,
                    document_id=(
                        f"document:{self._security_by_id[document.security_id].ticker}:"
                        f"{document.id}"
                    ),
                    source_url=document.url,
                )
            )
        return items

    async def risk_diff(self, plan: RetrievalPlan, user_id: str) -> list[RetrievedItem]:
        items = await self.document_digest(plan, user_id)
        return [
            RetrievedItem(
                **{
                    **item.__dict__,
                    "data_class": "risk_diff",
                    "content": {
                        key: value
                        for key, value in item.content.items()
                        if key in {"security_id", "ticker", "company_name", "document_id", "headline", "comparative"}
                    },
                }
            )
            for item in items
            if item.content.get("comparative")
        ]

    async def document_section(
        self,
        plan: RetrievalPlan,
        _user_id: str,
    ) -> list[RetrievedItem]:
        ids = sorted(self._security_ids(plan))
        if not ids:
            return []
        requested_item = plan.structured_filters.get("item")
        blob = get_blob_context()
        items: list[RetrievedItem] = []
        for security_id in ids:
            documents = await get_document_repo().query(
                query=(
                    "SELECT TOP 10 * FROM c WHERE c.security_id=@security_id "
                    "ORDER BY c.knowledge_date DESC"
                ),
                parameters=[{"name": "@security_id", "value": security_id}],
                partition_key=security_id,
            )
            for document in documents:
                paths = document.section_blob_paths.items()
                if requested_item is not None:
                    paths = [
                        (item, path)
                        for item, path in paths
                        if item.lower() == requested_item.lower()
                    ]
                for item_name, blob_path in paths:
                    container, _, path = blob_path.partition("/")
                    text = await blob.download_text(container, path)
                    ticker = self._security_by_id[security_id].ticker
                    items.append(
                        RetrievedItem(
                            data_class="document_section",
                            content={
                                "ticker": ticker,
                                "company_name": self._security_by_id[security_id].name,
                                "document_id": document.id,
                                "form": document.form_type,
                                "item": item_name,
                                "text": text,
                            },
                            security_id=security_id,
                            document_id=f"document:{ticker}:{document.id}",
                            source_url=document.url,
                            retrieved_at=utc_now(),
                            relevance_rank=len(items),
                        )
                    )
                    if len(items) >= 3:
                        return items
        return items

    async def insider_activity(self, plan: RetrievalPlan, user_id: str) -> list[RetrievedItem]:
        items = await self.score_snapshot(plan, user_id)
        return [
            RetrievedItem(
                **{
                    **item.__dict__,
                    "data_class": "insider_activity",
                    "content": {
                        "security_id": item.content.get("security_id"),
                        "ticker": item.content.get("ticker"),
                        "company_name": item.content.get("company_name"),
                        "as_of_date": item.content.get("as_of_date"),
                        "smart_money": (item.content.get("legs") or {}).get("smart_money"),
                    },
                }
            )
            for item in items
        ]

    async def performance(self, _plan: RetrievalPlan, _user_id: str) -> list[RetrievedItem]:
        rows = await get_performance_repo().query(query="SELECT * FROM c")
        rows = sorted(rows, key=lambda row: row.as_of_date, reverse=True)[:50]
        return [
            self._item("performance", row, row.model_dump(mode="json"), rank)
            for rank, row in enumerate(rows)
        ]

    def fetcher(self) -> RetrievalFetcher:
        repos = DataClassRepos(
            score_snapshot=self.score_snapshot,
            leg_history=self.leg_history,
            leg_changes=self.leg_changes,
            document_digest=self.document_digest,
            document_section=self.document_section,
            risk_diff=self.risk_diff,
            fundamentals=self.fundamentals,
            insider_activity=self.insider_activity,
            portfolio_state=self.portfolio_state,
            recommendations=self.recommendations,
            narrative_history=self.narrative_history,
            performance=self.performance,
        )
        return RetrievalFetcher(repos)


@lru_cache
def get_chat_fetcher() -> RetrievalFetcher:
    return ChatGrounding(get_universe()).fetcher()
