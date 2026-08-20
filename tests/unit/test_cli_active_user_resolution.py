from types import SimpleNamespace

import pytest

from auspex.cli.main import _resolve_active_users
from auspex.models.app_user import UserStatus


@pytest.mark.asyncio
@pytest.mark.parametrize("authoritative_status", [None, UserStatus.SUSPENDED])
async def test_stale_active_roster_entries_are_not_fanned_out(
    monkeypatch,
    authoritative_status,
):
    class FakeService:
        def __init__(self, **kwargs):
            pass

        async def list_users(self, *, status):
            assert status is UserStatus.ACTIVE
            return [SimpleNamespace(user_id="stale-user")]

        async def get_user(self, user_id):
            if authoritative_status is None:
                return None
            return SimpleNamespace(
                user_id=user_id,
                status=authoritative_status,
                ledger_partition_key="stale-ledger",
            )

    monkeypatch.setattr("auspex.users.service.AppUserService", FakeService)

    assert await _resolve_active_users(object()) == []


@pytest.mark.asyncio
async def test_authoritative_active_user_keeps_its_ledger_binding(monkeypatch):
    class FakeService:
        def __init__(self, **kwargs):
            pass

        async def list_users(self, *, status):
            return [SimpleNamespace(user_id="active-user")]

        async def get_user(self, user_id):
            return SimpleNamespace(
                user_id=user_id,
                status=UserStatus.ACTIVE,
                ledger_partition_key="preserved-ledger",
            )

    monkeypatch.setattr("auspex.users.service.AppUserService", FakeService)

    assert await _resolve_active_users(object()) == [
        ("active-user", "preserved-ledger")
    ]
