"""Shared test doubles/helpers for API route unit tests (arc42 §11).

`FakeCosmosRepository` mimics the narrow async surface routes depend on
(:class:`auspex.persistence.repositories.CosmosRepository`: ``get``,
``upsert``, ``query``) without touching Cosmos DB. Its ``query`` parses just
enough of the handful of query shapes every route in this package actually
issues — ``c.field OP @param`` comparisons (``=``, ``>=``, ``<=``, ``<``,
``>``), ``ORDER BY c.field ASC|DESC``, and ``TOP n`` / ``TOP @limit`` — so
route-specific tests can assert real filtering/ordering behaviour instead of
only "the handler didn't crash". It also records every call so a test can
assert a route scoped its query by the authenticated `user_id` rather than a
request-supplied one (arc42 §11).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from auspex.api.auth import get_current_user
from auspex.models.security import Security

T = TypeVar("T")

_TOP_RE = re.compile(r"TOP\s+(\d+|@\w+)", re.IGNORECASE)
_ORDER_RE = re.compile(r"ORDER BY\s+c\.(\w+)\s+(ASC|DESC)", re.IGNORECASE)
_FILTER_RE = re.compile(r"c\.(\w+)\s*(>=|<=|=|<|>)\s*@(\w+)")


@dataclass
class RecordedQuery:
    query: str
    parameters: list[dict] | None
    partition_key: str | None


@dataclass
class FakeUniverse:
    """Stand-in for `auspex.config.loader.Universe` with a handful of known securities."""

    securities: list[Security]

    def by_id(self) -> dict[str, Security]:
        return {s.id: s for s in self.securities}

    def by_ticker(self) -> dict[str, Security]:
        return {s.ticker: s for s in self.securities}


def _matches(query: str, item: Any, parameters: list[dict] | None) -> bool:
    param_values = {p["name"].lstrip("@"): p["value"] for p in (parameters or [])}
    for attr, op, param_name in _FILTER_RE.findall(query):
        if param_name not in param_values or not hasattr(item, attr):
            continue
        left, right = str(getattr(item, attr)), str(param_values[param_name])
        if op == "=" and left != right:
            return False
        if op == ">=" and not left >= right:
            return False
        if op == "<=" and not left <= right:
            return False
        if op == "<" and not left < right:
            return False
        if op == ">" and not left > right:
            return False
    return True


class FakeCosmosRepository(Generic[T]):
    def __init__(self, items: list[T] | None = None) -> None:
        self.items: list[T] = list(items or [])
        self.queries: list[RecordedQuery] = []
        self.upserted: list[T] = []

    async def get(self, id_: str, partition_key: str | None = None) -> T | None:
        for item in self.items:
            if getattr(item, "id", None) == id_:
                return item
        return None

    async def upsert(self, item: T) -> None:
        self.upserted.append(item)
        for i, existing in enumerate(self.items):
            if getattr(existing, "id", None) == getattr(item, "id", None):
                self.items[i] = item
                break
        else:
            self.items.append(item)

    async def query(
        self, query: str, parameters: list[dict] | None = None, partition_key: str | None = None
    ) -> list[T]:
        self.queries.append(RecordedQuery(query=query, parameters=parameters, partition_key=partition_key))

        matched = [item for item in self.items if _matches(query, item, parameters)]

        order_match = _ORDER_RE.search(query)
        if order_match:
            attr, direction = order_match.group(1), order_match.group(2).upper()
            matched.sort(key=lambda item: getattr(item, attr, None), reverse=(direction == "DESC"))

        top_match = _TOP_RE.search(query)
        if top_match:
            token = top_match.group(1)
            if token.startswith("@"):
                count = next((p["value"] for p in (parameters or []) if p["name"] == token), None)
            else:
                count = int(token)
            if count is not None:
                matched = matched[: int(count)]

        return matched


def make_router_app(router: APIRouter, overrides: dict) -> TestClient:
    """Mount ``router`` exactly as ``app.py`` will: under ``/api``, behind auth.

    Lets route-specific tests exercise the router object app.py must
    include (arc42 §7 "every /api/* route requires a validated Entra
    token") without app.py itself having to list the new router yet.
    """

    app = FastAPI()
    api_router = APIRouter(prefix="/api", dependencies=[Depends(get_current_user)])
    api_router.include_router(router)
    app.include_router(api_router)
    app.dependency_overrides.update(overrides)
    return TestClient(app)
