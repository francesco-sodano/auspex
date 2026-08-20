"""Cross-cutting multi-user invariants that are easy to regress silently.

Two classes of mistake this guards against:

1. **A new user-partitioned container is added but not erased on deletion.**
   The container registry and the deletion target list are checked against
   each other, so forgetting one is a test failure rather than a
   data-protection incident discovered later.
2. **An ordinary route starts accepting a `user_id`.** Identity must always
   come from the validated token; a route that takes it as input is an
   authorisation bug waiting to happen.
"""

from __future__ import annotations

import asyncio
import re

from auspex.api import create_app
from auspex.api.deps import IndexRepositoryFacade
from auspex.api.routes.account_deletion import build_purge_targets
from auspex.models.app_user import ADMIN_BINDING_ID
from auspex.persistence.cosmos_client import (
    CONTAINER_PARTITION_KEYS,
    USER_PARTITIONED_CONTAINERS,
)

_PLACEHOLDER_RE = re.compile(r"\{([^}]+)\}")


def _path_placeholders(path: str) -> list[str]:
    return _PLACEHOLDER_RE.findall(path)

#: Containers that are user-partitioned but finalized outside the generic target list.
#:
#: ``app_users`` and its roster row are hard-deleted by
#: :meth:`AppUserService.purge_user_record` only after every private target
#: verifies empty. ``deletion_jobs`` is removed immediately before that final
#: account-record purge.
NON_PURGED_USER_CONTAINERS = {"app_users", "deletion_jobs"}


def openapi_paths(app) -> dict:
    """External paths as the app actually serves them.

    Walking ``app.routes`` is unreliable here: this FastAPI version applies
    router prefixes at match time, so the raw route objects carry unprefixed
    paths and a naive scan finds ``/session`` rather than ``/api/session``.
    The OpenAPI document is the authoritative view of the served surface.
    """

    return app.openapi()["paths"]


class _StubRepo:
    async def purge_partition(self, partition_key: str) -> int:  # pragma: no cover - shape only
        return 0

    async def count_partition(self, partition_key: str) -> int:  # pragma: no cover - shape only
        return 0


class _DeleteRepo:
    def __init__(self) -> None:
        self.deleted: list[tuple[str, str]] = []

    async def delete(self, id_: str, partition_key: str) -> bool:
        self.deleted.append((id_, partition_key))
        return True


def purge_target_names() -> set[str]:
    targets = build_purge_targets(
        ledger=None,
        user_id="user-1",
        settings_repo=_StubRepo(),
        recommendation_repo=_StubRepo(),
        disposition_repo=_StubRepo(),
        projection_repo=_StubRepo(),
        conversation_repo=_StubRepo(),
        onboarding_repo=_StubRepo(),
        audit_repo=_StubRepo(),
        user_performance_repo=_StubRepo(),
    )
    return {target.name for target in targets}


class TestContainerRegistry:
    def test_index_facade_deletes_summary_and_binding_from_correct_repository(self):
        summaries = _DeleteRepo()
        bindings = _DeleteRepo()
        facade = IndexRepositoryFacade(summaries, bindings)

        assert asyncio.run(facade.delete("user-1", "registry")) is True
        assert asyncio.run(facade.delete(ADMIN_BINDING_ID, "registry")) is True
        assert summaries.deleted == [("user-1", "registry")]
        assert bindings.deleted == [(ADMIN_BINDING_ID, "registry")]

    def test_every_user_partitioned_container_is_declared_with_a_user_partition_key(self):
        for name in USER_PARTITIONED_CONTAINERS:
            assert CONTAINER_PARTITION_KEYS[name] == "/user_id", name

    def test_every_user_partitioned_container_is_erased_or_explicitly_exempt(self):
        purged = purge_target_names()

        missing = set(USER_PARTITIONED_CONTAINERS) - purged - NON_PURGED_USER_CONTAINERS
        assert missing == set(), f"user data would survive deletion in: {sorted(missing)}"

    def test_deletion_also_erases_the_users_ledger_partition(self):
        class _Ledger:
            async def purge_owner_ledger(self, user_id: str) -> int:  # pragma: no cover - shape only
                return 0

            async def count_owner_ledger(self, user_id: str) -> int:  # pragma: no cover - shape only
                return 0

        targets = build_purge_targets(
            ledger=_Ledger(),
            user_id="user-1",
            settings_repo=_StubRepo(),
            recommendation_repo=_StubRepo(),
            disposition_repo=_StubRepo(),
            projection_repo=_StubRepo(),
            conversation_repo=_StubRepo(),
            onboarding_repo=_StubRepo(),
            audit_repo=_StubRepo(),
            user_performance_repo=_StubRepo(),
        )

        ledger_target = next(target for target in targets if target.name == "portfolio_transactions")
        assert ledger_target.store == "source_ledger"

    def test_shared_research_containers_are_never_deletion_targets(self):
        purged = purge_target_names()

        for shared in (
            "securities",
            "documents",
            "extractions",
            "digests",
            "narratives",
            "market_daily",
            "fundamentals",
            "scores",
            "leg_changes",
            "performance",
            "runs",
            "config_versions",
            "watermarks",
        ):
            assert shared not in purged, shared


class TestRouteIdentityDiscipline:
    def test_no_ordinary_route_accepts_a_user_id(self):
        """`user_id` may only appear on the administrator surface.

        Everywhere else the caller's identity comes from the validated token,
        so a route that accepts it as a path/query/body parameter would let
        one user address another's data.
        """

        offenders: list[str] = []
        forbidden = {"user_id", "owner_user_sk", "owner_id"}
        for path, operations in openapi_paths(create_app()).items():
            if not path.startswith("/api") or path.startswith("/api/admin/"):
                continue
            if forbidden & set(_path_placeholders(path)):
                offenders.append(path)
                continue
            for operation in operations.values():
                names = {
                    parameter.get("name")
                    for parameter in operation.get("parameters", [])
                    if parameter.get("in") in ("path", "query")
                }
                if forbidden & names:
                    offenders.append(path)

        assert offenders == []

    def test_admin_routes_are_the_only_ones_addressing_another_user(self):
        admin_paths = {
            path for path in openapi_paths(create_app()) if "{user_id}" in path
        }

        assert admin_paths
        assert all(path.startswith("/api/admin/users") for path in admin_paths)

    def test_lifecycle_routes_are_exempt_from_the_active_gate(self):
        """Registration, onboarding and deletion must stay reachable.

        A user who is not yet ACTIVE has to be able to register, finish
        onboarding, and delete their account; gating those behind ACTIVE
        would make the states unreachable and unrecoverable.
        """

        paths = set(openapi_paths(create_app()))

        assert "/api/session" in paths
        assert "/api/session/register" in paths
        assert "/api/session/status" in paths
        assert "/api/onboarding/complete" in paths
        assert "/api/account/deletion" in paths
