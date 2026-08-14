"""Portfolio field mapping configuration (arc42 §5.7 "Mapping configuration").

The source ledger is event-sourced in `portfolio_transactions`, partitioned by
`/owner_user_sk`. Container names and identity resolution are configuration so
tenant-local and pre-existing ledgers share the same application code.

``owner_user_sk`` resolution precedence: an explicit static value, the stable
mapping computed from ``Settings.owner_provider_user_id``, then an optional
``app_users`` lookup for imported ledgers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from auspex.identity import compatible_user_id
from auspex.settings import get_settings

DEFAULT_REVISION_DOCUMENT_ID = "_ledger_revision"


class PortfolioMappingError(ValueError):
    """Raised when ``config/portfolio_mapping.yaml`` is missing a required mapping."""


@dataclass(frozen=True)
class TransactionsMappingConfig:
    container: str
    partition_key_field: str
    revision_document_id: str = DEFAULT_REVISION_DOCUMENT_ID


@dataclass(frozen=True)
class IdentityMappingConfig:
    """Optional dynamic owner_user_sk resolution via the `app_users` container
    (read-only lookup by identity key -> `user_sk`)."""

    container: str
    identity_key: str | None = None


@dataclass(frozen=True)
class PortfolioMappingConfig:
    transactions: TransactionsMappingConfig
    owner_user_sk: str | None
    identity_mapping: IdentityMappingConfig | None


def load_portfolio_mapping(config_dir: Path | None = None, mapping_path: Path | None = None) -> PortfolioMappingConfig:
    """Load ``config/portfolio_mapping.yaml``.

    ``mapping_path`` (or ``Settings.portfolio_mapping`` when set — matching
    infra's ``AUSPEX_PORTFOLIO_MAPPING`` env var) overrides the default
    ``config_dir / "portfolio_mapping.yaml"`` location.
    """

    settings = get_settings()
    if mapping_path is None and settings.portfolio_mapping:
        mapping_path = Path(settings.portfolio_mapping)
    if mapping_path is None:
        config_dir = config_dir or settings.config_dir
        mapping_path = config_dir / "portfolio_mapping.yaml"

    with mapping_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    transactions_raw = raw["transactions"]
    if not transactions_raw.get("container") or not transactions_raw.get("partition_key_field"):
        raise PortfolioMappingError(
            "config/portfolio_mapping.yaml: transactions.container and "
            "transactions.partition_key_field are required"
        )
    transactions = TransactionsMappingConfig(
        container=transactions_raw["container"],
        partition_key_field=transactions_raw["partition_key_field"],
        revision_document_id=transactions_raw.get("revision_document_id", DEFAULT_REVISION_DOCUMENT_ID),
    )

    owner_user_sk = raw.get("owner_user_sk") or None
    if not owner_user_sk and settings.owner_provider_user_id:
        owner_user_sk = compatible_user_id(settings.owner_provider_user_id)
    identity_mapping_raw = raw.get("identity_mapping")
    identity_mapping = None
    if identity_mapping_raw:
        identity_mapping = IdentityMappingConfig(
            container=identity_mapping_raw["container"],
            identity_key=identity_mapping_raw.get("identity_key") or None,
        )

    if not owner_user_sk and identity_mapping is None:
        raise PortfolioMappingError(
            "config/portfolio_mapping.yaml: either owner_user_sk or identity_mapping must be configured "
            "(or Settings.owner_provider_user_id / AUSPEX_OWNER_PROVIDER_USER_ID must be set)"
        )

    return PortfolioMappingConfig(
        transactions=transactions, owner_user_sk=owner_user_sk, identity_mapping=identity_mapping
    )
