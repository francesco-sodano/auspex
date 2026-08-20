from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from auspex.cli.main import _migrate_multi_user_command
from auspex.models.app_user import UserRole, UserStatus


@pytest.mark.asyncio
async def test_migrate_multi_user_activates_the_configured_owner(monkeypatch):
    approved = SimpleNamespace(
        user_id="owner-user-id",
        status=UserStatus.APPROVED_NEEDS_ONBOARDING,
        ledger_partition_key="legacy-ledger-partition",
    )
    active = SimpleNamespace(
        user_id="owner-user-id",
        status=UserStatus.ACTIVE,
        role=UserRole.ADMIN,
        ledger_partition_key="legacy-ledger-partition",
    )
    service = SimpleNamespace(
        register=AsyncMock(return_value=approved),
        complete_onboarding=AsyncMock(return_value=active),
        set_role=AsyncMock(),
    )
    cosmos = SimpleNamespace(aclose=AsyncMock())

    monkeypatch.setattr(
        "auspex.settings.get_settings",
        lambda: SimpleNamespace(
            owner_provider_user_id="provider-owner-id",
            owner_ledger_partition_key="legacy-ledger-partition",
            initial_admin_email="owner@example.com",
        ),
    )
    monkeypatch.setattr("auspex.api.deps.get_app_user_service", lambda: service)
    monkeypatch.setattr("auspex.persistence.cosmos_client.get_cosmos_context", lambda: cosmos)

    assert await _migrate_multi_user_command() == 0
    service.register.assert_awaited_once_with(
        provider_user_id="provider-owner-id",
        email="owner@example.com",
        email_verified=False,
    )
    service.complete_onboarding.assert_awaited_once_with("owner-user-id")
    service.set_role.assert_not_awaited()
    cosmos.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_migrate_multi_user_refuses_missing_owner_configuration(monkeypatch):
    monkeypatch.setattr(
        "auspex.settings.get_settings",
        lambda: SimpleNamespace(
            owner_provider_user_id="",
            owner_ledger_partition_key="legacy-ledger-partition",
            initial_admin_email="owner@example.com",
        ),
    )

    assert await _migrate_multi_user_command() == 1


@pytest.mark.asyncio
async def test_migrate_multi_user_refuses_an_implicit_ledger_partition(monkeypatch):
    monkeypatch.setattr(
        "auspex.settings.get_settings",
        lambda: SimpleNamespace(
            owner_provider_user_id="provider-owner-id",
            owner_ledger_partition_key="",
            initial_admin_email="owner@example.com",
        ),
    )

    assert await _migrate_multi_user_command() == 1


@pytest.mark.asyncio
async def test_migrate_multi_user_promotes_an_existing_active_owner(monkeypatch):
    existing = SimpleNamespace(
        user_id="owner-user-id",
        status=UserStatus.ACTIVE,
        role=UserRole.USER,
        ledger_partition_key="legacy-ledger-partition",
    )
    promoted = SimpleNamespace(
        user_id="owner-user-id",
        status=UserStatus.ACTIVE,
        role=UserRole.ADMIN,
        ledger_partition_key="legacy-ledger-partition",
    )
    service = SimpleNamespace(
        register=AsyncMock(return_value=existing),
        complete_onboarding=AsyncMock(),
        set_role=AsyncMock(return_value=promoted),
    )
    cosmos = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(
        "auspex.settings.get_settings",
        lambda: SimpleNamespace(
            owner_provider_user_id="provider-owner-id",
            owner_ledger_partition_key="legacy-ledger-partition",
            initial_admin_email="owner@example.com",
        ),
    )
    monkeypatch.setattr("auspex.api.deps.get_app_user_service", lambda: service)
    monkeypatch.setattr("auspex.persistence.cosmos_client.get_cosmos_context", lambda: cosmos)

    assert await _migrate_multi_user_command() == 0
    service.complete_onboarding.assert_not_awaited()
    service.set_role.assert_awaited_once_with(
        "owner-user-id",
        UserRole.ADMIN,
        actor_user_id="owner-user-id",
    )


@pytest.mark.asyncio
async def test_migrate_multi_user_refuses_an_existing_wrong_ledger_binding(
    monkeypatch,
):
    existing = SimpleNamespace(
        user_id="owner-user-id",
        status=UserStatus.ACTIVE,
        role=UserRole.ADMIN,
        ledger_partition_key="wrong-ledger",
    )
    service = SimpleNamespace(
        register=AsyncMock(return_value=existing),
        complete_onboarding=AsyncMock(),
        set_role=AsyncMock(),
    )
    cosmos = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(
        "auspex.settings.get_settings",
        lambda: SimpleNamespace(
            owner_provider_user_id="provider-owner-id",
            owner_ledger_partition_key="legacy-ledger-partition",
            initial_admin_email="owner@example.com",
        ),
    )
    monkeypatch.setattr("auspex.api.deps.get_app_user_service", lambda: service)
    monkeypatch.setattr("auspex.persistence.cosmos_client.get_cosmos_context", lambda: cosmos)

    assert await _migrate_multi_user_command() == 1
    service.complete_onboarding.assert_not_awaited()
    service.set_role.assert_not_awaited()
