"""Unit tests for portfolio field mapping configuration (arc42 §5.7 "Mapping
configuration") — event-ledger shape (`portfolio_transactions` / `owner_user_sk`)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from auspex.identity import compatible_user_id
from auspex.portfolio.mapping import (
    DEFAULT_REVISION_DOCUMENT_ID,
    PortfolioMappingError,
    load_portfolio_mapping,
)

VALID_STATIC_MAPPING = {
    "transactions": {
        "container": "portfolio_transactions",
        "partition_key_field": "owner_user_sk",
        "revision_document_id": "_ledger_revision",
    },
    "owner_user_sk": "owner-1-sk",
}

VALID_IDENTITY_MAPPING = {
    "transactions": {
        "container": "portfolio_transactions",
        "partition_key_field": "owner_user_sk",
    },
    "identity_mapping": {
        "container": "app_users",
        "identity_key": "aad-principal-abc",
    },
}

VALID_SINGLE_OWNER_MAPPING = {
    "transactions": {
        "container": "portfolio_transactions",
        "partition_key_field": "owner_user_sk",
    },
    "identity_mapping": {
        "container": "app_users",
        "identity_key": None,
    },
}


def write_mapping(tmp_path: Path, mapping: dict) -> Path:
    path = tmp_path / "portfolio_mapping.yaml"
    path.write_text(yaml.safe_dump(mapping), encoding="utf-8")
    return path


class TestLoadPortfolioMapping:
    def test_loads_valid_static_mapping(self, tmp_path):
        write_mapping(tmp_path, VALID_STATIC_MAPPING)
        config = load_portfolio_mapping(config_dir=tmp_path)
        assert config.transactions.container == "portfolio_transactions"
        assert config.transactions.partition_key_field == "owner_user_sk"
        assert config.transactions.revision_document_id == "_ledger_revision"
        assert config.owner_user_sk == "owner-1-sk"
        assert config.identity_mapping is None

    def test_loads_valid_identity_mapping(self, tmp_path):
        write_mapping(tmp_path, VALID_IDENTITY_MAPPING)
        config = load_portfolio_mapping(config_dir=tmp_path)
        assert config.owner_user_sk is None
        assert config.identity_mapping is not None
        assert config.identity_mapping.container == "app_users"
        assert config.identity_mapping.identity_key == "aad-principal-abc"

    def test_default_revision_document_id_used_when_absent(self, tmp_path):
        mapping = {**VALID_STATIC_MAPPING, "transactions": {**VALID_STATIC_MAPPING["transactions"]}}
        del mapping["transactions"]["revision_document_id"]
        write_mapping(tmp_path, mapping)
        config = load_portfolio_mapping(config_dir=tmp_path)
        assert config.transactions.revision_document_id == DEFAULT_REVISION_DOCUMENT_ID

    def test_loads_single_active_owner_mapping(self, tmp_path):
        write_mapping(tmp_path, VALID_SINGLE_OWNER_MAPPING)
        config = load_portfolio_mapping(config_dir=tmp_path)
        assert config.identity_mapping is not None
        assert config.identity_mapping.identity_key is None

    def test_missing_container_raises(self, tmp_path):
        mapping = {
            "transactions": {"partition_key_field": "owner_user_sk"},
            "owner_user_sk": "owner-1-sk",
        }
        write_mapping(tmp_path, mapping)
        with pytest.raises(PortfolioMappingError, match="container"):
            load_portfolio_mapping(config_dir=tmp_path)

    def test_missing_partition_key_field_raises(self, tmp_path):
        mapping = {
            "transactions": {"container": "portfolio_transactions"},
            "owner_user_sk": "owner-1-sk",
        }
        write_mapping(tmp_path, mapping)
        with pytest.raises(PortfolioMappingError, match="partition_key_field"):
            load_portfolio_mapping(config_dir=tmp_path)

    def test_missing_owner_resolution_raises(self, tmp_path):
        mapping = {"transactions": VALID_STATIC_MAPPING["transactions"]}
        write_mapping(tmp_path, mapping)
        with pytest.raises(PortfolioMappingError, match="owner_user_sk|identity_mapping"):
            load_portfolio_mapping(config_dir=tmp_path)

    def test_shipped_default_mapping_file_parses(self):
        config = load_portfolio_mapping()
        assert config.transactions.container == "portfolio_transactions"
        assert config.transactions.partition_key_field == "owner_user_sk"
        assert config.owner_user_sk is None
        assert config.identity_mapping is not None


class TestOwnerProviderUserIdFallback:
    """arc42 §5.7: when neither owner_user_sk nor identity_mapping is
    configured at all, fall back to the legacy identity-compatibility
    mapping computed from Settings.owner_provider_user_id — never
    overriding an explicit choice in the YAML file itself."""

    def test_falls_back_to_compatible_mapping_when_nothing_else_configured(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUSPEX_OWNER_PROVIDER_USER_ID", "provider-user-1")
        from auspex.settings import get_settings

        get_settings.cache_clear()
        try:
            mapping = {"transactions": VALID_STATIC_MAPPING["transactions"]}
            write_mapping(tmp_path, mapping)
            config = load_portfolio_mapping(config_dir=tmp_path)
            assert config.owner_user_sk == compatible_user_id("provider-user-1")
            assert config.identity_mapping is None
        finally:
            get_settings.cache_clear()

    def test_explicit_static_owner_user_sk_takes_priority_over_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUSPEX_OWNER_PROVIDER_USER_ID", "provider-user-1")
        from auspex.settings import get_settings

        get_settings.cache_clear()
        try:
            write_mapping(tmp_path, VALID_STATIC_MAPPING)
            config = load_portfolio_mapping(config_dir=tmp_path)
            assert config.owner_user_sk == "owner-1-sk"  # explicit value, not the compat mapping
        finally:
            get_settings.cache_clear()

    def test_configured_owner_object_id_takes_priority_over_identity_lookup(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUSPEX_OWNER_PROVIDER_USER_ID", "provider-user-1")
        from auspex.settings import get_settings

        get_settings.cache_clear()
        try:
            write_mapping(tmp_path, VALID_IDENTITY_MAPPING)
            config = load_portfolio_mapping(config_dir=tmp_path)
            assert config.owner_user_sk == compatible_user_id("provider-user-1")
            assert config.identity_mapping is not None
        finally:
            get_settings.cache_clear()

    def test_still_raises_when_neither_configured_and_no_settings_fallback(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AUSPEX_OWNER_PROVIDER_USER_ID", raising=False)
        from auspex.settings import get_settings

        get_settings.cache_clear()
        try:
            mapping = {"transactions": VALID_STATIC_MAPPING["transactions"]}
            write_mapping(tmp_path, mapping)
            with pytest.raises(PortfolioMappingError):
                load_portfolio_mapping(config_dir=tmp_path)
        finally:
            get_settings.cache_clear()
