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

from auspex.api.auth import AuthenticatedUser, get_current_user
from auspex.api.deps import get_app_user_service
from auspex.models.app_user import AppUser, AppUserSummary, UserRole, UserStatus
from auspex.models.common import utc_now
from auspex.models.security import Security
from auspex.users.service import AppUserService

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


class InMemoryAppUserRepository:
    """Point-read/upsert store standing in for a ``/user_id``-partitioned container."""

    def __init__(self, items: list | None = None) -> None:
        self.items: dict[tuple[str, str], object] = {}
        for item in items or []:
            self.items[(item.id, item.partition_key)] = item

    async def get(self, id_: str, partition_key: str):
        return self.items.get((id_, partition_key))

    async def upsert(self, item) -> None:
        self.items[(item.id, item.partition_key)] = item

    async def query(self, query: str, parameters: list[dict] | None = None, partition_key: str | None = None):
        param_values = {p["name"].lstrip("@"): p["value"] for p in (parameters or [])}
        results = []
        for (_, partition), item in self.items.items():
            if partition_key is not None and partition != partition_key:
                continue
            if not _matches(query, item, parameters):
                continue
            if "kind" in param_values and getattr(item, "kind", "user") != param_values["kind"]:
                continue
            results.append(item)
        return results

    async def delete(self, id_: str, partition_key: str) -> bool:
        return self.items.pop((id_, partition_key), None) is not None

    async def partition_ids(self, partition_key: str) -> list[str]:
        return [key[0] for key in self.items if key[1] == partition_key]

    async def purge_partition(self, partition_key: str) -> int:
        keys = [key for key in self.items if key[1] == partition_key]
        for key in keys:
            del self.items[key]
        return len(keys)

    async def count_partition(self, partition_key: str) -> int:
        return sum(1 for key in self.items if key[1] == partition_key)


def make_app_user(
    user_id: str = "user-1",
    *,
    status: UserStatus = UserStatus.ACTIVE,
    role: UserRole = UserRole.USER,
    provider_user_id: str | None = None,
    email: str | None = None,
    ledger_partition_key: str | None = None,
) -> AppUser:
    now = utc_now()
    return AppUser(
        id=user_id,
        user_id=user_id,
        provider_user_id=provider_user_id or f"oid-{user_id}",
        email=email,
        status=status,
        role=role,
        ledger_partition_key=ledger_partition_key or user_id,
        registered_at=now,
        updated_at=now,
        onboarding_completed_at=now if status is UserStatus.ACTIVE else None,
    )


def build_app_user_service(users: list[AppUser]) -> AppUserService:
    """An :class:`AppUserService` over in-memory containers."""

    user_repo = InMemoryAppUserRepository(users)
    index_repo = InMemoryAppUserRepository([AppUserSummary.from_user(user) for user in users])
    return AppUserService(
        user_repo=user_repo,
        index_repo=index_repo,
        audit_repo=InMemoryAppUserRepository(),
    )


def lifecycle_overrides(user: AppUser, *, others: list[AppUser] | None = None) -> dict:
    """Dependency overrides that authenticate *and* authorise ``user``.

    Every ``/api`` route now sits behind both the Entra token check and the
    ``app_users`` lifecycle gate, so route tests must satisfy both.
    """

    roster = [user, *(others or [])]
    service = build_app_user_service(roster)
    return {
        get_current_user: lambda: AuthenticatedUser(
            user_id=user.user_id,
            claims={"oid": user.provider_user_id},
            provider_user_id=user.provider_user_id,
            email=user.email,
        ),
        get_app_user_service: lambda: service,
    }
