"""Live portfolio ledger integration (arc42 §5.7).

The event-sourced `portfolio_transactions` container is Auspex's
live portfolio system of record. Auspex binds to it through
:class:`PortfolioAdapter` for reads, replaying the
ledger with :mod:`auspex.portfolio.event_ledger`, using field/container
names from ``config/portfolio_mapping.yaml``. Validated append-only writes are
isolated in :class:`PortfolioLedgerService`; projections remain Auspex-owned.
"""

from __future__ import annotations

from auspex.portfolio.adapter import (
    OwnerResolutionError,
    PortfolioAdapter,
    ReadOnlyContainer,
    ReadOnlyDatabase,
)
from auspex.portfolio.event_ledger import (
    CashCurrencyUnresolvedError,
    LedgerTransaction,
    derive_cash_by_currency,
    derive_cash_chf,
    derive_holdings,
    effective_transactions,
)
from auspex.portfolio.ledger_service import PortfolioLedgerService, PortfolioLedgerValidationError
from auspex.portfolio.mapping import (
    IdentityMappingConfig,
    PortfolioMappingConfig,
    PortfolioMappingError,
    TransactionsMappingConfig,
    load_portfolio_mapping,
)
from auspex.portfolio.port import Holding, PortfolioPort, PortfolioSnapshot
from auspex.portfolio.projection import PortfolioProjectionResult, PositionProjection, project_portfolio
from auspex.portfolio.validation import BindingValidationResult, validate_portfolio_binding

__all__ = [
    "OwnerResolutionError",
    "PortfolioAdapter",
    "PortfolioLedgerService",
    "PortfolioLedgerValidationError",
    "ReadOnlyContainer",
    "ReadOnlyDatabase",
    "CashCurrencyUnresolvedError",
    "LedgerTransaction",
    "derive_cash_by_currency",
    "derive_cash_chf",
    "derive_holdings",
    "effective_transactions",
    "IdentityMappingConfig",
    "PortfolioMappingConfig",
    "PortfolioMappingError",
    "TransactionsMappingConfig",
    "load_portfolio_mapping",
    "Holding",
    "PortfolioPort",
    "PortfolioSnapshot",
    "PortfolioProjectionResult",
    "PositionProjection",
    "project_portfolio",
    "BindingValidationResult",
    "validate_portfolio_binding",
]
