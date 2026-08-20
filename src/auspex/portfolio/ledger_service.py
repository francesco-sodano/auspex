"""Validated, owner-scoped writes to the event-sourced portfolio ledger."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from azure.cosmos.exceptions import CosmosResourceExistsError, CosmosResourceNotFoundError

from auspex.models.common import sha256_hex, utc_now
from auspex.portfolio.adapter import PortfolioAdapter
from auspex.portfolio.event_ledger import (
    LedgerTransaction,
    derive_cash_chf,
    derive_holdings,
    effective_transactions,
)
from auspex.portfolio.mapping import PortfolioMappingConfig

SECURITY_TYPES = frozenset({"OPENING_POSITION", "BUY", "SELL"})
CASH_IN_TYPES = frozenset({"OPENING_CASH", "DEPOSIT", "DIVIDEND", "INTEREST"})
CASH_OUT_TYPES = frozenset({"WITHDRAWAL", "FEE", "TAX"})
ALLOWED_TYPES = SECURITY_TYPES | CASH_IN_TYPES | CASH_OUT_TYPES
SUPPORTED_CURRENCIES = frozenset({"CHF", "USD"})
ALLOWED_COST_CATEGORIES = frozenset(
    {
        "BROKER_COMMISSION",
        "TRANSACTION_TAX",
        "WITHHOLDING_TAX",
        "VAT",
        "CUSTODY_FEE",
        "ACCOUNT_FEE",
        "OTHER_FEE",
    }
)
REQUEST_NAMESPACE = uuid.UUID("92f8d2dd-efb5-49ac-b36e-49cb53700699")


class WritableContainer(Protocol):
    def query_items(
        self,
        query: str,
        parameters: list[dict[str, Any]] | None = None,
        partition_key: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]: ...

    async def read_item(self, item: str, partition_key: str) -> dict[str, Any]: ...

    async def create_item(self, body: dict[str, Any]) -> dict[str, Any]: ...


class WritableDatabase(Protocol):
    def get_container_client(self, name: str) -> WritableContainer: ...


class PortfolioLedgerValidationError(ValueError):
    pass


def _decimal(
    value: str | int | Decimal | None,
    field: str,
    *,
    required: bool = False,
    non_negative: bool = False,
    max_scale: int = 8,
) -> Decimal | None:
    if value in (None, ""):
        if required:
            raise PortfolioLedgerValidationError(f"{field} is required")
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PortfolioLedgerValidationError(f"{field} must be a decimal number") from exc
    if not parsed.is_finite():
        raise PortfolioLedgerValidationError(f"{field} must be finite")
    if non_negative and parsed < 0:
        raise PortfolioLedgerValidationError(f"{field} must be non-negative")
    if parsed.as_tuple().exponent < -max_scale:
        raise PortfolioLedgerValidationError(f"{field} supports at most {max_scale} decimal places")
    return parsed


def _canonical_hash(payload: dict[str, Any]) -> str:
    return sha256_hex(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str))


def _normalize_cost_category(category: str, transaction_type: str) -> str:
    normalized = category.upper()
    if normalized == "STAMP_DUTY":
        return "TRANSACTION_TAX"
    if normalized == "TAX":
        return "WITHHOLDING_TAX" if transaction_type == "DIVIDEND" else "TRANSACTION_TAX"
    return normalized


class PortfolioLedgerService:
    """Validated, append-only writes against one user's event ledger.

    **Multi-user.** A service instance is bound to exactly one ledger
    partition, supplied as ``owner_user_sk`` by the caller that already knows
    who the authenticated principal is. There is no global "the owner" here
    any more: :meth:`_owner` derives the partition from the authenticated
    ``user_id`` alone, so two users can never observe or mutate each other's
    events regardless of what identifiers appear in a request body.

    ``owner_user_sk`` exists only so an imported deployment can pin a
    pre-existing ledger partition that is not the derived ``user_id``; when it
    is supplied, every call must still present the matching authenticated
    ``user_id``.
    """

    def __init__(
        self,
        database: WritableDatabase,
        mapping: PortfolioMappingConfig,
        adapter: PortfolioAdapter,
        valid_tickers: set[str],
        owner_user_sk: str | None = None,
        authenticated_user_id: str | None = None,
    ) -> None:
        self._database = database
        self._mapping = mapping
        self._adapter = adapter
        self._valid_tickers = valid_tickers
        self._owner_user_sk = owner_user_sk or None
        self._authenticated_user_id = authenticated_user_id or None

    async def _owner(self, authenticated_user_id: str) -> str:
        """Ledger partition for the authenticated caller.

        The partition is derived from the caller's own identity — never from
        anything in the request — so ordinary user operations cannot address
        another user's ledger. When the service was constructed for a specific
        principal, a mismatched ``authenticated_user_id`` is refused outright
        rather than quietly rebinding.
        """

        if not authenticated_user_id:
            raise PermissionError("an authenticated user is required for ledger access")
        if self._authenticated_user_id is not None and authenticated_user_id != self._authenticated_user_id:
            raise PermissionError("authenticated user does not match this ledger binding")
        if self._owner_user_sk is not None:
            return self._owner_user_sk
        explicit = getattr(self._adapter, "owner_user_sk", None)
        if explicit is not None:
            return explicit
        return authenticated_user_id

    async def purge_owner_ledger(self, authenticated_user_id: str) -> int:
        """Hard-delete every event in this user's ledger partition.

        Used only by account deletion (arc42 §8.3). Idempotent: replaying it
        over an already-empty partition deletes nothing and succeeds.
        """

        owner = await self._owner(authenticated_user_id)
        container = self._container()
        deleted = 0
        items = container.query_items(
            query="SELECT VALUE c.id FROM c",
            parameters=[],
            partition_key=owner,
        )
        document_ids = [str(item) async for item in items]
        for document_id in document_ids:
            delete_item = getattr(container, "delete_item", None)
            if delete_item is None:  # pragma: no cover - defensive
                raise PortfolioLedgerValidationError("ledger container does not support deletion")
            try:
                await delete_item(item=document_id, partition_key=owner)
            except CosmosResourceNotFoundError:
                continue
            deleted += 1
        return deleted

    async def count_owner_ledger(self, authenticated_user_id: str) -> int:
        """Documents remaining in this user's ledger partition."""

        owner = await self._owner(authenticated_user_id)
        items = self._container().query_items(
            query="SELECT VALUE COUNT(1) FROM c",
            parameters=[],
            partition_key=owner,
        )
        rows = [row async for row in items]
        return int(rows[0]) if rows else 0

    def _container(self) -> WritableContainer:
        return self._database.get_container_client(self._mapping.transactions.container)

    async def _documents(self, owner: str) -> list[dict[str, Any]]:
        cfg = self._mapping.transactions
        items = self._container().query_items(
            query=(
                f"SELECT * FROM c WHERE c.{cfg.partition_key_field} = @owner "
                "AND c.id != @revision_id"
            ),
            parameters=[
                {"name": "@owner", "value": owner},
                {"name": "@revision_id", "value": cfg.revision_document_id},
            ],
            partition_key=owner,
        )
        return [item async for item in items]

    @staticmethod
    def _child_cost_component(
        child: dict[str, Any],
        parent: dict[str, Any],
    ) -> dict[str, str | None]:
        amount = (
            child.get("source_amount")
            if child.get("source_amount") is not None
            else child.get("gross_amount")
            if child.get("gross_amount") is not None
            else abs(Decimal(str(child.get("cash_amount", "0"))))
        )
        component_currency = (
            child.get("source_currency")
            or child.get("currency")
            or parent.get("currency", "CHF")
        )
        return {
            "category": child.get("cost_category")
            or child.get("category")
            or "OTHER_FEE",
            "amount": str(amount),
            "currency": str(component_currency),
            "source_amount": (
                str(child["source_amount"])
                if child.get("source_amount") is not None
                else None
            ),
            "source_currency": child.get("source_currency"),
            "fx_rate_to_settlement": (
                str(child["fx_rate_to_settlement"])
                if child.get("fx_rate_to_settlement") is not None
                else None
            ),
        }

    def _cost_components_for_document(
        self,
        document: dict[str, Any],
        by_id: dict[str, dict[str, Any]],
        children_by_parent: dict[str, list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        current = document
        visited: set[str] = set()
        while True:
            transaction_id = str(current.get("transaction_id") or current.get("id"))
            if transaction_id in visited:
                return []
            visited.add(transaction_id)
            components = list(current.get("cost_components", []))
            components.extend(
                self._child_cost_component(child, current)
                for child in children_by_parent.get(transaction_id, [])
            )
            if components or current.get("cost_components_explicit"):
                return components
            corrected_id = current.get("corrects_transaction_id")
            if not corrected_id or str(corrected_id) not in by_id:
                return []
            current = by_id[str(corrected_id)]

    async def list_transactions(self, authenticated_user_id: str) -> list[dict[str, Any]]:
        owner = await self._owner(authenticated_user_id)
        documents = await self._documents(owner)
        children_by_parent: dict[str, list[dict[str, Any]]] = {}
        for document in documents:
            parent_id = document.get("linked_transaction_id")
            if parent_id:
                children_by_parent.setdefault(str(parent_id), []).append(document)
        by_id = {
            str(document.get("transaction_id") or document["id"]): document
            for document in documents
        }
        corrected_by = {
            document["corrects_transaction_id"]: document
            for document in documents
            if document.get("corrects_transaction_id")
        }
        rows = []
        for document in documents:
            transaction_id = document.get("transaction_id") or document.get("id")
            if document.get("transaction_type") == "VOID" or document.get("linked_transaction_id"):
                continue
            correction = corrected_by.get(transaction_id)
            status = (
                "VOIDED"
                if correction and correction.get("transaction_type") == "VOID"
                else "CORRECTED"
                if correction
                else "EFFECTIVE"
            )
            cost_components = self._cost_components_for_document(
                document,
                by_id,
                children_by_parent,
            )
            rows.append(
                {
                    **document,
                    "transaction_id": transaction_id,
                    "cost_components": cost_components,
                    "status": status,
                }
            )
        return sorted(
            (row for row in rows if row["status"] == "EFFECTIVE"),
            key=lambda row: (row.get("event_date", ""), row.get("created_at", ""), row["transaction_id"]),
            reverse=True,
        )

    def _validate_payload(
        self,
        payload: dict[str, Any],
        effective: list[LedgerTransaction],
    ) -> dict[str, Any]:
        transaction_type = str(payload["transaction_type"]).upper()
        if transaction_type not in ALLOWED_TYPES:
            raise PortfolioLedgerValidationError(f"unsupported transaction_type {transaction_type!r}")
        currency = str(payload.get("currency") or "CHF").upper()
        if currency not in SUPPORTED_CURRENCIES:
            raise PortfolioLedgerValidationError("currency must be CHF or USD")
        security_code = str(payload.get("security_code") or "").upper() or None
        quantity = _decimal(payload.get("quantity"), "quantity", non_negative=True)
        price = _decimal(payload.get("price"), "price", non_negative=True)
        amount = _decimal(payload.get("amount"), "amount", non_negative=True)
        flat_fees = (
            _decimal(
                payload.get("fees", "0"),
                "fees",
                required=True,
                non_negative=True,
                max_scale=2,
            )
            or Decimal(0)
        )
        fx_rate = _decimal(payload.get("fx_rate_to_base"), "fx_rate_to_base", non_negative=True)
        if fx_rate == 0:
            raise PortfolioLedgerValidationError("fx_rate_to_base must be positive")

        components_explicit = bool(
            payload.get(
                "_cost_components_explicit",
                payload.get("cost_components") is not None,
            )
        )
        raw_components = payload.get("cost_components")
        if raw_components is None:
            fixed_components = [
                (
                    "BROKER_COMMISSION",
                    _decimal(
                        payload.get("broker_commission", "0"),
                        "broker_commission",
                        required=True,
                        non_negative=True,
                        max_scale=2,
                    )
                    or Decimal(0),
                ),
                (
                    "TRANSACTION_TAX",
                    _decimal(
                        payload.get("stamp_duty", "0"),
                        "stamp_duty",
                        required=True,
                        non_negative=True,
                        max_scale=2,
                    )
                    or Decimal(0),
                ),
                (
                    "WITHHOLDING_TAX" if transaction_type == "DIVIDEND" else "TRANSACTION_TAX",
                    _decimal(
                        payload.get("taxes", "0"),
                        "taxes",
                        required=True,
                        non_negative=True,
                        max_scale=2,
                    )
                    or Decimal(0),
                ),
                ("OTHER_FEE", flat_fees),
            ]
            raw_components = [
                {"category": category, "amount": str(component_amount), "currency": currency}
                for category, component_amount in fixed_components
                if component_amount > 0
            ]
        elif flat_fees > 0 or any(
            _decimal(payload.get(field, "0"), field, non_negative=True) not in {None, Decimal(0)}
            for field in ("broker_commission", "stamp_duty", "taxes")
        ):
            raise PortfolioLedgerValidationError(
                "use cost_components instead of the flat fee fields"
            )

        if len(raw_components) > 20:
            raise PortfolioLedgerValidationError("cost_components supports at most 20 rows")
        cost_components: list[dict[str, str]] = []
        for index, component in enumerate(raw_components):
            category = _normalize_cost_category(
                str(component.get("category") or ""),
                transaction_type,
            )
            if category not in ALLOWED_COST_CATEGORIES:
                raise PortfolioLedgerValidationError(
                    f"cost_components[{index}].category is invalid"
                )
            component_currency = str(component.get("currency") or "").upper()
            if component_currency not in SUPPORTED_CURRENCIES:
                raise PortfolioLedgerValidationError(
                    f"cost_components[{index}].currency must be CHF or USD"
                )
            component_amount = _decimal(
                component.get("amount"),
                f"cost_components[{index}].amount",
                required=True,
                non_negative=True,
                max_scale=2,
            )
            if component_amount is None or component_amount <= 0:
                raise PortfolioLedgerValidationError(
                    f"cost_components[{index}].amount must be positive"
                )
            cost_components.append(
                {
                    "category": category,
                    "amount": str(component_amount),
                    "currency": component_currency,
                }
            )

        uses_usd = currency == "USD" or any(
            component["currency"] == "USD" for component in cost_components
        )
        if uses_usd and fx_rate is None:
            raise PortfolioLedgerValidationError(
                "fx_rate_to_base is required when the transaction or a cost is in USD"
            )

        def component_in_transaction_currency(component: dict[str, str]) -> Decimal:
            component_amount = Decimal(component["amount"])
            component_currency = component["currency"]
            if component_currency == currency:
                return component_amount
            if fx_rate is None:
                raise PortfolioLedgerValidationError(
                    "fx_rate_to_base is required for mixed-currency costs"
                )
            if component_currency == "USD":
                return component_amount * fx_rate
            return component_amount / fx_rate

        fees = sum(
            (component_in_transaction_currency(component) for component in cost_components),
            Decimal(0),
        )
        followed_auspex = bool(payload.get("followed_auspex", False))
        recommendation_id = str(payload.get("recommendation_id") or "") or None
        if followed_auspex and recommendation_id is None:
            raise PortfolioLedgerValidationError(
                "recommendation_id is required when followed_auspex is true"
            )

        if transaction_type in SECURITY_TYPES:
            if not security_code or security_code not in self._valid_tickers:
                raise PortfolioLedgerValidationError("security_code must be in the Auspex universe")
            if quantity is None or quantity <= 0:
                raise PortfolioLedgerValidationError("quantity must be positive")
            if price is None or price <= 0:
                raise PortfolioLedgerValidationError("price must be positive")
        if transaction_type == "DIVIDEND" and not security_code:
            raise PortfolioLedgerValidationError("security_code is required for a dividend")
        if transaction_type in CASH_IN_TYPES | CASH_OUT_TYPES and amount is None:
            raise PortfolioLedgerValidationError("amount is required")
        if transaction_type in {"OPENING_CASH", "FEE", "TAX"} and fees:
            raise PortfolioLedgerValidationError(f"{transaction_type} does not accept separate fees")

        if transaction_type == "SELL":
            held = sum(
                (
                    holding.quantity
                    for holding in derive_holdings(effective)
                    if holding.ticker == security_code
                ),
                Decimal(0),
            )
            if quantity is not None and quantity > held:
                raise PortfolioLedgerValidationError(
                    f"cannot sell {quantity} {security_code}; only {held} is held"
                )

        gross = quantity * price if quantity is not None and price is not None else amount or Decimal(0)
        gross_chf = (
            gross
            if currency == "CHF"
            else gross * (fx_rate or Decimal(0))
        )
        if transaction_type == "BUY":
            cash_amount = -gross_chf
        elif transaction_type in {"SELL", "DIVIDEND", "INTEREST"}:
            cash_amount = gross_chf
        elif transaction_type in CASH_OUT_TYPES:
            cash_amount = -gross_chf
        elif transaction_type == "OPENING_POSITION":
            cash_amount = Decimal(0)
        else:
            cash_amount = gross_chf

        costs_affect_cash = transaction_type != "OPENING_POSITION"
        component_cost_chf = (
            sum(
                (
                    Decimal(component["amount"])
                    if component["currency"] == "CHF"
                    else Decimal(component["amount"]) * (fx_rate or Decimal(0))
                    for component in cost_components
                ),
                Decimal(0),
            )
            if costs_affect_cash
            else Decimal(0)
        )
        cash_effect_chf = cash_amount - component_cost_chf
        available_chf = derive_cash_chf(
            effective,
            fx_rate_to_chf=lambda cash_currency: (
                Decimal(1)
                if cash_currency == "CHF"
                else fx_rate
                if cash_currency == "USD"
                else None
            ),
        )
        if available_chf + cash_effect_chf < 0:
            raise PortfolioLedgerValidationError(
                f"insufficient CHF cash: requested "
                f"{abs(min(cash_effect_chf, Decimal(0)))}, available {available_chf}"
            )

        return {
            "transaction_type": transaction_type,
            "event_date": date.fromisoformat(str(payload["event_date"])).isoformat(),
            "currency": currency,
            "security_code": security_code,
            "quantity": str(quantity) if quantity is not None else None,
            "price": str(price) if price is not None else None,
            "gross_amount": str(gross),
            "cash_amount": str(cash_amount),
            "cash_currency": "CHF",
            "fees": str(fees),
            "cost_components": cost_components,
            "cost_components_affect_cash": costs_affect_cash,
            "cost_components_explicit": components_explicit,
            "fx_rate_to_base": str(fx_rate) if fx_rate is not None else None,
            "followed_auspex": followed_auspex,
            "recommendation_id": recommendation_id if followed_auspex else None,
            "notes": str(payload.get("notes") or "") or None,
            "affects_cash": transaction_type != "OPENING_POSITION",
        }

    async def create_transaction(
        self,
        authenticated_user_id: str,
        payload: dict[str, Any],
        *,
        corrects_transaction_id: str | None = None,
        exclude_transaction_id: str | None = None,
    ) -> dict[str, Any]:
        owner = await self._owner(authenticated_user_id)
        documents = await self._documents(owner)
        transactions = [LedgerTransaction.from_document(document) for document in documents]
        effective = [
            transaction
            for transaction in effective_transactions(transactions)
            if transaction.transaction_id != exclude_transaction_id
        ]
        normalized = self._validate_payload(payload, effective)
        client_request_id = str(payload["client_request_id"])
        transaction_id = str(uuid.uuid5(REQUEST_NAMESPACE, f"{owner}\0{client_request_id}"))
        request_hash = _canonical_hash({**normalized, "corrects_transaction_id": corrects_transaction_id})
        document = {
            "id": transaction_id,
            "transaction_id": transaction_id,
            "owner_user_sk": owner,
            "client_request_id": client_request_id,
            **normalized,
            "corrects_transaction_id": corrects_transaction_id,
            "linked_transaction_id": None,
            "created_at": utc_now().isoformat(),
            "payload_hash": request_hash,
            "request_hash": request_hash,
        }
        container = self._container()
        try:
            await container.create_item(document)
        except CosmosResourceExistsError:
            existing = await container.read_item(item=transaction_id, partition_key=owner)
            if existing.get("request_hash") != request_hash:
                raise PortfolioLedgerValidationError(
                    "client_request_id was already used with a different payload"
                ) from None
            document = existing
        return document

    async def correct_transaction(
        self,
        authenticated_user_id: str,
        transaction_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        owner = await self._owner(authenticated_user_id)
        documents = await self._documents(owner)
        by_id = {
            str(document.get("transaction_id") or document["id"]): document
            for document in documents
        }
        target = by_id.get(transaction_id)
        if target is None:
            raise KeyError(transaction_id)
        if target.get("owner_user_sk") != owner:
            raise KeyError(transaction_id)
        if target.get("linked_transaction_id"):
            raise PortfolioLedgerValidationError("linked cost rows cannot be edited independently")
        if any(document.get("corrects_transaction_id") == transaction_id for document in documents):
            raise PortfolioLedgerValidationError("transaction has already been corrected or voided")
        components_explicit = payload.get("cost_components") is not None
        if payload.get("cost_components") is None:
            children_by_parent: dict[str, list[dict[str, Any]]] = {}
            for child in documents:
                parent_id = child.get("linked_transaction_id")
                if parent_id:
                    children_by_parent.setdefault(str(parent_id), []).append(child)
            inherited_components = [
                {
                    "category": component["category"],
                    "amount": component["amount"],
                    "currency": component["currency"],
                }
                for component in self._cost_components_for_document(
                    target,
                    by_id,
                    children_by_parent,
                )
            ]
            if not inherited_components and Decimal(str(target.get("fees", "0"))) > 0:
                inherited_components.append(
                    {
                        "category": "OTHER_FEE",
                        "amount": str(target["fees"]),
                        "currency": target.get("currency", "CHF"),
                    }
                )
            payload = {
                **payload,
                "cost_components": inherited_components,
                "fees": "0",
                "broker_commission": "0",
                "stamp_duty": "0",
                "taxes": "0",
            }
        payload = {**payload, "_cost_components_explicit": components_explicit}
        if payload.get("fx_rate_to_base") in {None, ""}:
            payload = {**payload, "fx_rate_to_base": target.get("fx_rate_to_base")}
        return await self.create_transaction(
            authenticated_user_id,
            payload,
            corrects_transaction_id=transaction_id,
            exclude_transaction_id=transaction_id,
        )

    async def void_transaction(
        self,
        authenticated_user_id: str,
        transaction_id: str,
        client_request_id: str,
    ) -> dict[str, Any]:
        owner = await self._owner(authenticated_user_id)
        documents = await self._documents(owner)
        by_id = {
            str(document.get("transaction_id") or document["id"]): document
            for document in documents
        }
        target = by_id.get(transaction_id)
        if target is None:
            raise KeyError(transaction_id)
        if target.get("owner_user_sk") != owner:
            raise KeyError(transaction_id)
        if target.get("linked_transaction_id"):
            raise PortfolioLedgerValidationError("linked cost rows cannot be voided independently")
        if any(document.get("corrects_transaction_id") == transaction_id for document in documents):
            raise PortfolioLedgerValidationError("transaction has already been corrected or voided")
        void_id = str(uuid.uuid5(REQUEST_NAMESPACE, f"{owner}\0{client_request_id}"))
        document = {
            "id": void_id,
            "transaction_id": void_id,
            "owner_user_sk": owner,
            "client_request_id": client_request_id,
            "transaction_type": "VOID",
            "event_date": date.today().isoformat(),
            "currency": target.get("currency", "CHF"),
            "security_code": target.get("security_code"),
            "quantity": None,
            "price": None,
            "gross_amount": "0",
            "cash_amount": "0",
            "cash_currency": "CHF",
            "fees": "0",
            "fx_rate_to_base": None,
            "notes": f"Voids {transaction_id}",
            "affects_cash": False,
            "corrects_transaction_id": transaction_id,
            "linked_transaction_id": None,
            "created_at": utc_now().isoformat(),
        }
        request_hash = _canonical_hash(document)
        document["payload_hash"] = request_hash
        document["request_hash"] = request_hash
        try:
            await self._container().create_item(document)
        except CosmosResourceExistsError:
            existing = await self._container().read_item(item=void_id, partition_key=owner)
            if (
                existing.get("transaction_type") != "VOID"
                or existing.get("request_hash") != request_hash
            ):
                raise PortfolioLedgerValidationError(
                    "client_request_id was already used with a different payload"
                ) from None
            return existing
        return document
