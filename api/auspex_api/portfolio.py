from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Callable, Protocol, TYPE_CHECKING
import uuid

from .owner_scoped import OwnerScope
from .services import AuthorizationError, IdentityService

if TYPE_CHECKING:
    from azure.cosmos import ContainerProxy


_TRANSACTION_NAMESPACE = uuid.UUID("88873225-1eb9-4a08-826f-d3b17302a14b")
_TRANSACTION_TYPES = {
    "OPENING_POSITION",
    "OPENING_CASH",
    "DEPOSIT",
    "WITHDRAWAL",
    "DIVIDEND",
    "INTEREST",
    "FEE",
    "BUY",
    "SELL",
}
_CURRENCIES = {"USD", "CHF", "EUR", "GBP"}
_SECURITY_TYPES = {"OPENING_POSITION", "BUY", "SELL"}
_SECURITY_REFERENCE_TYPES = _SECURITY_TYPES | {"DIVIDEND"}
_CASH_IN_TYPES = {"OPENING_CASH", "DEPOSIT", "DIVIDEND", "INTEREST"}
_CASH_OUT_TYPES = {"WITHDRAWAL", "FEE"}
_FX_RATE_TYPES = {"OPENING_CASH", "OPENING_POSITION", "DEPOSIT", "WITHDRAWAL"}
_LINKED_COST_TYPES = {"OPENING_POSITION", "BUY", "SELL", "DIVIDEND", "FEE"}
_COST_CATEGORIES = {
    "BROKER_COMMISSION",
    "TRANSACTION_TAX",
    "WITHHOLDING_TAX",
    "VAT",
    "CUSTODY_FEE",
    "ACCOUNT_FEE",
    "OTHER_FEE",
}
_ACCOUNT_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SECURITY_PATTERN = re.compile(r"^[A-Z0-9.-]{1,15}$")
_MAX_MONEY = Decimal("999999999999.99")
_MAX_QUANTITY = Decimal("1000000000")
_LEDGER_REVISION_ID = "_ledger_revision"


def _decimal(
    value,
    field: str,
    *,
    allow_zero: bool = False,
    max_value: Decimal | None = None,
    max_scale: int | None = None,
) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a decimal number") from exc
    if not parsed.is_finite() or parsed < 0 or (not allow_zero and parsed == 0):
        raise ValueError(f"{field} must be {'non-negative' if allow_zero else 'positive'}")
    if max_value is not None and parsed > max_value:
        raise ValueError(f"{field} exceeds the maximum supported value")
    if max_scale is not None and max(0, -parsed.as_tuple().exponent) > max_scale:
        raise ValueError(f"{field} supports at most {max_scale} decimal places")
    return parsed


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), ".2f")


def _quantity(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text in {"-0", ""} else text


def _add_amount(amounts: dict[str, Decimal], currency: str, amount: Decimal) -> None:
    amounts[currency] = amounts.get(currency, Decimal("0")) + amount


def _money_map(amounts: dict[str, Decimal]) -> dict[str, str]:
    return {
        currency: _money(amount)
        for currency, amount in sorted(amounts.items())
        if amount != 0
    }


def _transaction_id(owner_user_sk: str, client_request_id: str) -> str:
    return str(uuid.uuid5(
        _TRANSACTION_NAMESPACE,
        f"{owner_user_sk}\0{client_request_id}",
    ))


def _converted_amount(amount: Decimal, source_currency: str, settlement_currency: str, rate) -> tuple[Decimal, Decimal | None]:
    if source_currency == settlement_currency:
        if rate is not None:
            raise ValueError("FX rate is only valid when currencies differ")
        return amount, None
    parsed_rate = _decimal(
        rate, "fx_rate_to_settlement", max_value=_MAX_QUANTITY, max_scale=8,
    )
    converted = (amount * parsed_rate).quantize(Decimal("0.01"))
    if converted <= 0 or converted > _MAX_MONEY:
        raise ValueError("converted amount is outside the supported range")
    return converted, parsed_rate


def _request_hash(payload: dict, corrects_transaction_id: str | None = None) -> str:
    if not isinstance(payload, dict):
        raise ValueError("transaction payload must be an object")
    if any(field in payload for field in (
        "owner_user_sk", "security_sk", "corrects_transaction_id",
    )):
        raise ValueError("owner and security keys are server-controlled")
    def canonical_decimal(field: str, *, allow_zero: bool = False) -> str | None:
        value = payload.get(field)
        if value is None:
            return None
        is_quantity = field in {"quantity", "fx_rate_to_base", "fx_rate_to_settlement"}
        parsed = _decimal(
            value,
            field,
            allow_zero=allow_zero,
            max_value=_MAX_QUANTITY if is_quantity else _MAX_MONEY,
            max_scale=8 if is_quantity else 2,
        )
        return _quantity(parsed)

    transaction_type = str(payload.get("transaction_type") or "").upper()
    raw_components = payload.get("cost_components") or []
    if not isinstance(raw_components, list):
        raise ValueError("cost_components must be an array")
    if len(raw_components) > 20:
        raise ValueError("cost_components supports at most 20 entries")
    components = []
    for component in raw_components:
        if not isinstance(component, dict):
            raise ValueError("each cost component must be an object")
        components.append({
            "category": str(component.get("category") or "").upper(),
            "amount": _quantity(_decimal(
                component.get("amount"), "cost component amount",
                max_value=_MAX_MONEY, max_scale=2,
            )),
            "currency": str(component.get("currency") or "").upper(),
            "fx_rate_to_settlement": (
                _quantity(_decimal(
                    component.get("fx_rate_to_settlement"),
                    "fx_rate_to_settlement",
                    max_value=_MAX_QUANTITY,
                    max_scale=8,
                ))
                if component.get("fx_rate_to_settlement") is not None else None
            ),
        })
    canonical = {
        "account_id": payload.get("account_id") or "primary",
        "transaction_type": transaction_type,
        "event_date": payload.get("event_date"),
        "currency": None if transaction_type in _SECURITY_REFERENCE_TYPES else str(payload.get("currency") or "").upper(),
        "security_code": (
            str(payload["security_code"]).strip().upper()
            if payload.get("security_code") is not None
            else None
        ),
        "quantity": canonical_decimal("quantity"),
        "price": canonical_decimal("price"),
        "amount": canonical_decimal("amount"),
        "fees": canonical_decimal("fees", allow_zero=True) or "0",
        "fx_rate_to_base": canonical_decimal("fx_rate_to_base"),
        "settlement_currency": str(payload.get("settlement_currency") or "").upper() or None,
        "source_currency": str(payload.get("source_currency") or "").upper() or None,
        "fx_rate_to_settlement": canonical_decimal("fx_rate_to_settlement"),
        "cost_category": str(payload.get("cost_category") or "").upper() or None,
        "cost_components": components,
        "corrects_transaction_id": corrects_transaction_id,
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class ResolvedSecurity:
    security_sk: int
    ticker: str
    isin: str | None
    company_name: str
    currency: str
    exchange: str | None
    gics_sector: str | None = None
    country: str | None = None


class SecurityCatalog(Protocol):
    def resolve(self, code: str) -> ResolvedSecurity: ...

    def get(self, security_sk: int) -> ResolvedSecurity | None: ...

    def search(self, prefix: str) -> list[ResolvedSecurity]: ...


class UniverseRepository(Protocol):
    def onboard(self, security: ResolvedSecurity) -> None: ...


class MarketDataRepository(Protocol):
    def quote(self, ticker: str, security_sk: int | None = None) -> dict | None: ...

    def fx_rate(self, from_currency: str, to_currency: str, as_of: str | None = None) -> dict | None: ...


class InMemorySecurityCatalog:
    def __init__(self, securities: list[ResolvedSecurity] | None = None) -> None:
        self._securities = {security.security_sk: security for security in securities or []}
        self.resolve_calls: list[str] = []

    def resolve(self, code: str) -> ResolvedSecurity:
        normalized = str(code).strip().upper()
        self.resolve_calls.append(normalized)
        matches = [
            security
            for security in self._securities.values()
            if normalized in {security.ticker.upper(), (security.isin or "").upper()}
        ]
        if not matches:
            raise ValueError("security was not found")
        if len(matches) != 1:
            raise ValueError("security is ambiguous")
        return matches[0]

    def get(self, security_sk: int) -> ResolvedSecurity | None:
        return self._securities.get(security_sk)

    def search(self, prefix: str) -> list[ResolvedSecurity]:
        normalized = str(prefix or "").strip().upper()
        if not _SECURITY_PATTERN.fullmatch(normalized):
            raise ValueError("invalid security prefix")
        return sorted(
            [
                security
                for security in self._securities.values()
                if security.ticker.upper().startswith(normalized)
            ],
            key=lambda security: security.ticker,
        )[:8]


class InMemoryUniverseRepository:
    def __init__(self) -> None:
        self._symbols: set[str] = set()

    def onboard(self, security: ResolvedSecurity) -> None:
        self._symbols.add(security.ticker.upper())

    def symbols(self) -> list[str]:
        return sorted(self._symbols)


class InMemoryMarketDataRepository:
    def __init__(self, quotes: dict | None = None, fx_rates: dict | None = None) -> None:
        self._quotes = {str(key).upper(): value for key, value in (quotes or {}).items()}
        self._fx_rates = {
            (str(pair[0]).upper(), str(pair[1]).upper()): value
            for pair, value in (fx_rates or {}).items()
        }

    def quote(self, ticker: str, security_sk: int | None = None) -> dict | None:
        return self._quotes.get(ticker.upper())

    def fx_rate(self, from_currency: str, to_currency: str, as_of: str | None = None) -> dict | None:
        source = from_currency.upper()
        target = to_currency.upper()
        if source == target:
            return {"rate": "1.00000000", "as_of": as_of}
        direct = self._fx_rates.get((source, target))
        if direct:
            return direct
        inverse = self._fx_rates.get((target, source))
        if inverse:
            return {
                "rate": _quantity(Decimal("1") / Decimal(inverse["rate"])),
                "as_of": inverse.get("as_of"),
            }
        source_usd = self._direct_or_inverse(source, "USD", as_of) if source != "USD" else {"rate": "1", "as_of": as_of}
        usd_target = self._direct_or_inverse("USD", target, as_of) if target != "USD" else {"rate": "1", "as_of": as_of}
        if not source_usd or not usd_target:
            return None
        dates = [value for value in (source_usd.get("as_of"), usd_target.get("as_of")) if value]
        return {
            "rate": _quantity(Decimal(source_usd["rate"]) * Decimal(usd_target["rate"])),
            "as_of": min(dates) if dates else as_of,
        }

    def _direct_or_inverse(self, source: str, target: str, as_of: str | None) -> dict | None:
        direct = self._fx_rates.get((source, target))
        if direct:
            return direct
        inverse = self._fx_rates.get((target, source))
        if not inverse:
            return None
        return {
            "rate": _quantity(Decimal("1") / Decimal(inverse["rate"])),
            "as_of": inverse.get("as_of") or as_of,
        }


@dataclass(frozen=True)
class PortfolioTransaction:
    transaction_id: str
    owner_user_sk: str
    client_request_id: str
    account_id: str
    transaction_type: str
    event_date: str
    currency: str
    security_code: str | None
    quantity: str | None
    price: str | None
    fees: str
    cash_amount: str
    payload_hash: str
    created_at: str
    request_hash: str | None = None
    security_sk: int | None = None
    security_isin: str | None = None
    security_name: str | None = None
    security_currency: str | None = None
    security_exchange: str | None = None
    base_currency: str | None = None
    fx_rate_to_base: str | None = None
    corrects_transaction_id: str | None = None
    security_sector: str | None = None
    security_country: str | None = None
    gross_amount: str | None = None
    source_currency: str | None = None
    source_amount: str | None = None
    fx_rate_to_settlement: str | None = None
    linked_transaction_id: str | None = None
    cost_category: str | None = None
    affects_cash: bool = True

    @classmethod
    def from_payload(
        cls,
        owner_user_sk: str,
        payload: dict,
        now: datetime | None = None,
        resolved_security: ResolvedSecurity | None = None,
        request_hash: str | None = None,
        base_currency: str | None = None,
        corrects_transaction_id: str | None = None,
    ) -> "PortfolioTransaction":
        if not isinstance(payload, dict):
            raise ValueError("transaction payload must be an object")
        if "corrects_transaction_id" in payload:
            raise ValueError("corrects_transaction_id is server-controlled")
        if any(field in payload for field in ("linked_transaction_id", "affects_cash")):
            raise ValueError("linked cost fields are server-controlled")
        client_request_id = payload.get("client_request_id")
        if not isinstance(client_request_id, str) or not 1 <= len(client_request_id) <= 128:
            raise ValueError("client_request_id is required and must be at most 128 characters")

        transaction_type = str(payload.get("transaction_type") or "").upper()
        if transaction_type not in _TRANSACTION_TYPES:
            raise ValueError("invalid transaction_type")
        event_date = payload.get("event_date")
        try:
            parsed_date = date.fromisoformat(event_date)
        except (TypeError, ValueError) as exc:
            raise ValueError("event_date must be an ISO date") from exc
        if parsed_date.isoformat() != event_date:
            raise ValueError("event_date must be an ISO date")
        current_date = (now or datetime.now(timezone.utc)).date()
        if parsed_date > current_date:
            raise ValueError("event_date cannot be in the future")

        account_id = payload.get("account_id") or "primary"
        if not isinstance(account_id, str) or not _ACCOUNT_PATTERN.fullmatch(account_id):
            raise ValueError("invalid account_id")
        currency = str(payload.get("currency") or "").upper()
        if currency not in _CURRENCIES:
            raise ValueError("invalid currency")

        security_code = payload.get("security_code")
        if security_code is not None:
            security_code = str(security_code).upper()
            if not _SECURITY_PATTERN.fullmatch(security_code):
                raise ValueError("invalid security_code")
        if transaction_type in _SECURITY_REFERENCE_TYPES and security_code is None:
            raise ValueError("security_code is required")
        if transaction_type in _SECURITY_REFERENCE_TYPES and resolved_security is not None:
            security_code = resolved_security.ticker.upper()
            security_currency = resolved_security.currency.upper()
            if security_currency not in _CURRENCIES:
                raise ValueError("security currency is not supported")
            currency = str(payload.get("settlement_currency") or security_currency).upper()
            if currency not in _CURRENCIES:
                raise ValueError("invalid settlement_currency")
        else:
            security_currency = currency if transaction_type in _SECURITY_REFERENCE_TYPES else None

        normalized_base_currency = str(base_currency or currency).upper()
        if normalized_base_currency not in _CURRENCIES:
            raise ValueError("invalid base currency")
        fx_rate_value = None
        if payload.get("fx_rate_to_base") is not None:
            if transaction_type not in _FX_RATE_TYPES:
                raise ValueError("fx_rate_to_base is not valid for this transaction type")
            if currency == normalized_base_currency:
                raise ValueError("fx_rate_to_base is only valid for a foreign currency")
            fx_rate_value = _decimal(
                payload["fx_rate_to_base"], "fx_rate_to_base",
                max_value=_MAX_QUANTITY, max_scale=8,
            )

        fees_value = _decimal(
            payload.get("fees", "0"), "fees",
            allow_zero=True, max_value=_MAX_MONEY, max_scale=2,
        )
        if transaction_type in {"OPENING_CASH", "OPENING_POSITION", "FEE"} and fees_value != 0:
            raise ValueError(f"{transaction_type} does not accept separate fees")
        quantity_value = None
        price_value = None
        amount_value = None
        if transaction_type in _SECURITY_TYPES:
            quantity_value = _decimal(
                payload.get("quantity"), "quantity",
                max_value=_MAX_QUANTITY, max_scale=8,
            )
            price_value = _decimal(
                payload.get("price"), "price",
                max_value=_MAX_MONEY, max_scale=2,
            )
            if quantity_value * price_value > _MAX_MONEY:
                raise ValueError("transaction notional exceeds the maximum supported value")
        elif transaction_type in _CASH_IN_TYPES | _CASH_OUT_TYPES:
            amount_value = _decimal(
                payload.get("amount"), "amount",
                max_value=_MAX_MONEY, max_scale=2,
            )

        source_amount_value = None
        gross_amount_value = None
        fx_rate_to_settlement_value = None
        if transaction_type in _SECURITY_TYPES:
            source_amount_value = quantity_value * price_value
            gross_amount_value, fx_rate_to_settlement_value = _converted_amount(
                source_amount_value,
                security_currency,
                currency,
                payload.get("fx_rate_to_settlement"),
            )
        elif transaction_type == "DIVIDEND":
            source_currency = str(payload.get("source_currency") or security_currency).upper()
            if source_currency not in _CURRENCIES:
                raise ValueError("invalid dividend source currency")
            source_amount_value = amount_value
            gross_amount_value, fx_rate_to_settlement_value = _converted_amount(
                source_amount_value,
                source_currency,
                currency,
                payload.get("fx_rate_to_settlement"),
            )
            security_currency = source_currency
        elif transaction_type == "FEE":
            source_amount_value = amount_value
            gross_amount_value = amount_value
            security_currency = currency

        if transaction_type == "BUY":
            cash_value = -(gross_amount_value + fees_value)
        elif transaction_type == "SELL":
            cash_value = gross_amount_value - fees_value
            if cash_value <= 0:
                raise ValueError("SELL proceeds must exceed fees")
        elif transaction_type == "OPENING_POSITION":
            cash_value = Decimal("0")
        elif transaction_type == "DIVIDEND":
            cash_value = gross_amount_value - fees_value
            if cash_value <= 0:
                raise ValueError("cash inflow must exceed fees")
        elif transaction_type in _CASH_IN_TYPES:
            cash_value = amount_value - fees_value
            if cash_value <= 0:
                raise ValueError("cash inflow must exceed fees")
        else:
            cash_value = -(amount_value + fees_value)
        if abs(cash_value) > _MAX_MONEY:
            raise ValueError("cash movement exceeds the maximum supported value")

        canonical = {
            "account_id": account_id,
            "transaction_type": transaction_type,
            "event_date": event_date,
            "currency": currency,
            "security_code": security_code,
            "quantity": _quantity(quantity_value) if quantity_value is not None else None,
            "price": _money(price_value) if price_value is not None else None,
            "fees": _money(fees_value),
            "cash_amount": _money(cash_value),
            "base_currency": normalized_base_currency,
            "fx_rate_to_base": _quantity(fx_rate_value) if fx_rate_value is not None else None,
            "corrects_transaction_id": corrects_transaction_id,
            "gross_amount": _money(gross_amount_value) if gross_amount_value is not None else None,
            "source_currency": security_currency if source_amount_value is not None else None,
            "source_amount": _money(source_amount_value) if source_amount_value is not None else None,
            "fx_rate_to_settlement": _quantity(fx_rate_to_settlement_value) if fx_rate_to_settlement_value is not None else None,
            "linked_transaction_id": None,
            "cost_category": (
                str(payload.get("cost_category") or "OTHER_FEE").upper()
                if transaction_type == "FEE" else None
            ),
            "affects_cash": True,
        }
        if canonical["cost_category"] not in _COST_CATEGORIES and transaction_type == "FEE":
            raise ValueError("invalid cost_category")
        payload_hash = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        transaction_id = _transaction_id(owner_user_sk, client_request_id)
        timestamp = (now or datetime.now(timezone.utc)).isoformat()
        return cls(
            transaction_id=transaction_id,
            owner_user_sk=owner_user_sk,
            client_request_id=client_request_id,
            payload_hash=payload_hash,
            created_at=timestamp,
            request_hash=request_hash,
            security_sk=resolved_security.security_sk if resolved_security else None,
            security_isin=resolved_security.isin if resolved_security else None,
            security_name=resolved_security.company_name if resolved_security else None,
            security_currency=resolved_security.currency if resolved_security else None,
            security_exchange=resolved_security.exchange if resolved_security else None,
            security_sector=resolved_security.gics_sector if resolved_security else None,
            security_country=resolved_security.country if resolved_security else None,
            **canonical,
        )

    @classmethod
    def from_document(cls, document: dict) -> "PortfolioTransaction":
        values = {
            field: document.get(field)
            for field in cls.__dataclass_fields__
        }
        values["affects_cash"] = document.get("affects_cash", True)
        return cls(**values)

    @classmethod
    def linked_costs_from_payload(
        cls,
        parent: "PortfolioTransaction",
        payload: dict,
    ) -> list["PortfolioTransaction"]:
        raw_components = payload.get("cost_components") or []
        if raw_components and parent.transaction_type not in _LINKED_COST_TYPES:
            raise ValueError("cost components are not valid for this transaction type")
        if raw_components and Decimal(parent.fees) != 0:
            raise ValueError("use either cost_components or legacy fees, not both")
        linked_costs = []
        for index, component in enumerate(raw_components):
            category = str(component.get("category") or "").upper()
            if category not in _COST_CATEGORIES:
                raise ValueError("invalid cost component category")
            source_currency = str(component.get("currency") or "").upper()
            if source_currency not in _CURRENCIES:
                raise ValueError("invalid cost component currency")
            source_amount = _decimal(
                component.get("amount"), "cost component amount",
                max_value=_MAX_MONEY, max_scale=2,
            )
            settlement_amount, rate = _converted_amount(
                source_amount,
                source_currency,
                parent.currency,
                component.get("fx_rate_to_settlement"),
            )
            component_request_id = "component-" + hashlib.sha256(
                f"{parent.client_request_id}\0{index}".encode()
            ).hexdigest()[:40]
            child = cls.from_payload(
                parent.owner_user_sk,
                {
                    "client_request_id": component_request_id,
                    "transaction_type": "FEE",
                    "event_date": parent.event_date,
                    "account_id": parent.account_id,
                    "currency": parent.currency,
                    "amount": _money(settlement_amount),
                    "cost_category": category,
                },
                now=datetime.fromisoformat(parent.created_at),
                base_currency=parent.base_currency,
            )
            affects_cash = parent.transaction_type != "OPENING_POSITION"
            canonical = {
                "linked_transaction_id": parent.transaction_id,
                "cost_category": category,
                "source_currency": source_currency,
                "source_amount": _money(source_amount),
                "gross_amount": _money(settlement_amount),
                "fx_rate_to_settlement": _quantity(rate) if rate is not None else None,
                "affects_cash": affects_cash,
                "cash_amount": _money(-settlement_amount) if affects_cash else "0.00",
            }
            payload_hash = hashlib.sha256(json.dumps(
                {**child.public_payload(), **canonical},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()).hexdigest()
            linked_costs.append(replace(child, payload_hash=payload_hash, **canonical))
        return linked_costs

    def to_document(self) -> dict:
        document = {
            field: getattr(self, field)
            for field in self.__dataclass_fields__
        }
        document.update({"id": self.transaction_id, "schema_version": 5})
        return document

    def public_payload(self) -> dict:
        return {
            "transaction_id": self.transaction_id,
            "client_request_id": self.client_request_id,
            "account_id": self.account_id,
            "transaction_type": self.transaction_type,
            "event_date": self.event_date,
            "currency": self.currency,
            "security_code": self.security_code,
            "security_sk": self.security_sk,
            "security_isin": self.security_isin,
            "security_name": self.security_name,
            "security_currency": self.security_currency,
            "security_exchange": self.security_exchange,
            "quantity": self.quantity,
            "price": self.price,
            "fees": self.fees,
            "cash_amount": self.cash_amount,
            "base_currency": self.base_currency,
            "fx_rate_to_base": self.fx_rate_to_base,
            "corrects_transaction_id": self.corrects_transaction_id,
            "security_sector": self.security_sector,
            "security_country": self.security_country,
            "gross_amount": self.gross_amount,
            "source_currency": self.source_currency,
            "source_amount": self.source_amount,
            "fx_rate_to_settlement": self.fx_rate_to_settlement,
            "linked_transaction_id": self.linked_transaction_id,
            "cost_category": self.cost_category,
            "affects_cash": self.affects_cash,
            "created_at": self.created_at,
        }


class PortfolioTransactionRepository(Protocol):
    def create(
        self,
        scope: OwnerScope,
        transaction: PortfolioTransaction,
        validate: Callable[[list[PortfolioTransaction], PortfolioTransaction], None] | None = None,
    ) -> tuple[PortfolioTransaction, bool]: ...

    def list(self, scope: OwnerScope) -> list[PortfolioTransaction]: ...

    def create_bundle(
        self,
        scope: OwnerScope,
        transactions: list[PortfolioTransaction],
        validate: Callable[[list[PortfolioTransaction], list[PortfolioTransaction]], None],
    ) -> tuple[PortfolioTransaction, bool]: ...

    def read(self, scope: OwnerScope, transaction_id: str) -> PortfolioTransaction | None: ...

    def replace(self, scope: OwnerScope, transaction: PortfolioTransaction) -> PortfolioTransaction: ...


class CosmosPortfolioTransactionRepository:
    def __init__(self, container: "ContainerProxy") -> None:
        self._container = container

    def create(self, scope, transaction, validate=None):
        if transaction.owner_user_sk != scope.owner_user_sk:
            raise ValueError("transaction owner does not match authenticated owner")
        if validate is not None:
            return self._create_validated(scope, transaction, validate)
        return self._create_direct(scope, transaction)

    def _create_direct(self, scope, transaction):
        try:
            document = self._container.create_item(transaction.to_document())
            return PortfolioTransaction.from_document(document), True
        except Exception as exc:
            if getattr(exc, "status_code", None) != 409:
                raise
            document = self._container.read_item(
                item=transaction.transaction_id,
                partition_key=scope.owner_user_sk,
            )
            existing = PortfolioTransaction.from_document(document)
            if existing.payload_hash != transaction.payload_hash:
                raise ValueError("client_request_id was already used for different data") from exc
            return existing, False

    def _create_validated(self, scope, transaction, validate):
        return self.create_bundle(
            scope,
            [transaction],
            lambda existing, candidates: validate(existing, candidates[0]),
        )

    def create_bundle(self, scope, transactions, validate):
        if not transactions:
            raise ValueError("transaction bundle cannot be empty")
        if any(row.owner_user_sk != scope.owner_user_sk for row in transactions):
            raise ValueError("transaction owner does not match authenticated owner")
        parent = transactions[0]
        for _ in range(3):
            existing = self.read(scope, parent.transaction_id)
            if existing is not None:
                if existing.payload_hash != parent.payload_hash:
                    raise ValueError("client_request_id was already used for different data")
                return existing, False

            revision = self._read_revision(scope)
            validate(self.list(scope), transactions)
            next_revision = {
                "id": _LEDGER_REVISION_ID,
                "owner_user_sk": scope.owner_user_sk,
                "kind": "ledger_revision",
                "version": int(revision.get("version", 0)) + 1 if revision else 1,
                "updated_at": parent.created_at,
            }
            operations = [
                ("create", (transaction.to_document(),))
                for transaction in transactions
            ]
            if revision is None:
                operations.append(("create", (next_revision,)))
            else:
                operations.append((
                    "replace",
                    (_LEDGER_REVISION_ID, next_revision),
                    {"if_match_etag": revision["_etag"]},
                ))
            try:
                self._container.execute_item_batch(
                    batch_operations=operations,
                    partition_key=scope.owner_user_sk,
                )
                return parent, True
            except Exception as exc:
                if getattr(exc, "status_code", None) not in {409, 412}:
                    raise
        raise ValueError("portfolio changed concurrently; retry the transaction")

    def _read_revision(self, scope):
        try:
            return self._container.read_item(
                item=_LEDGER_REVISION_ID,
                partition_key=scope.owner_user_sk,
            )
        except Exception as exc:
            if getattr(exc, "status_code", None) != 404:
                raise
            return None

    def list(self, scope):
        documents = self._container.query_items(
            query=(
                "SELECT * FROM c WHERE c.owner_user_sk = @owner_user_sk "
                "AND c.id != @revision_id"
            ),
            parameters=[
                {"name": "@owner_user_sk", "value": scope.owner_user_sk},
                {"name": "@revision_id", "value": _LEDGER_REVISION_ID},
            ],
            partition_key=scope.owner_user_sk,
        )
        transactions = [
            PortfolioTransaction.from_document(document)
            for document in documents
        ]
        return sorted(
            transactions,
            key=lambda transaction: (transaction.event_date, transaction.created_at),
            reverse=True,
        )

    def read(self, scope, transaction_id):
        try:
            document = self._container.read_item(
                item=transaction_id,
                partition_key=scope.owner_user_sk,
            )
            return PortfolioTransaction.from_document(document)
        except Exception as exc:
            if getattr(exc, "status_code", None) != 404:
                raise
            return None

    def replace(self, scope, transaction):
        if transaction.owner_user_sk != scope.owner_user_sk:
            raise ValueError("transaction owner does not match authenticated owner")
        document = self._container.replace_item(
            item=transaction.transaction_id,
            body=transaction.to_document(),
        )
        return PortfolioTransaction.from_document(document)


class InMemoryPortfolioTransactionRepository:
    def __init__(self) -> None:
        self._documents: dict[tuple[str, str], dict] = {}

    def create(self, scope, transaction, validate=None):
        if validate is not None:
            return self.create_bundle(
                scope,
                [transaction],
                lambda existing, candidates: validate(existing, candidates[0]),
            )
        if transaction.owner_user_sk != scope.owner_user_sk:
            raise ValueError("transaction owner does not match authenticated owner")
        key = (scope.owner_user_sk, transaction.transaction_id)
        existing = self._documents.get(key)
        if existing:
            existing_transaction = PortfolioTransaction.from_document(existing)
            if existing_transaction.payload_hash != transaction.payload_hash:
                raise ValueError("client_request_id was already used for different data")
            return existing_transaction, False
        self._documents[key] = transaction.to_document()
        return transaction, True

    def create_bundle(self, scope, transactions, validate):
        if not transactions:
            raise ValueError("transaction bundle cannot be empty")
        if any(row.owner_user_sk != scope.owner_user_sk for row in transactions):
            raise ValueError("transaction owner does not match authenticated owner")
        parent = transactions[0]
        existing = self.read(scope, parent.transaction_id)
        if existing is not None:
            if existing.payload_hash != parent.payload_hash:
                raise ValueError("client_request_id was already used for different data")
            return existing, False
        validate(self.list(scope), transactions)
        for transaction in transactions:
            self._documents[(scope.owner_user_sk, transaction.transaction_id)] = transaction.to_document()
        return parent, True

    def list(self, scope):
        transactions = [
            PortfolioTransaction.from_document(document)
            for (owner_user_sk, _), document in self._documents.items()
            if owner_user_sk == scope.owner_user_sk
        ]
        return sorted(
            transactions,
            key=lambda transaction: (transaction.event_date, transaction.created_at),
            reverse=True,
        )

    def read(self, scope, transaction_id):
        document = self._documents.get((scope.owner_user_sk, transaction_id))
        return PortfolioTransaction.from_document(document) if document else None

    def replace(self, scope, transaction):
        if transaction.owner_user_sk != scope.owner_user_sk:
            raise ValueError("transaction owner does not match authenticated owner")
        self._documents[(scope.owner_user_sk, transaction.transaction_id)] = transaction.to_document()
        return transaction


class PortfolioService:
    def __init__(
        self,
        identity: IdentityService,
        transactions: PortfolioTransactionRepository,
        security_catalog: SecurityCatalog | None = None,
        universe: UniverseRepository | None = None,
        market_data: MarketDataRepository | None = None,
        clock=None,
    ):
        self._identity = identity
        self._transactions = transactions
        self._security_catalog = security_catalog
        self._universe = universe
        self._market_data = market_data
        self._clock = clock

    def _scope(self, principal_header) -> OwnerScope:
        user = self._identity.product_user(principal_header)
        if not user.onboarded:
            raise AuthorizationError("Portfolio onboarding is required")
        return OwnerScope(user.user_sk)

    def create_transaction(self, principal_header, payload):
        user = self._identity.product_user(principal_header)
        if not user.onboarded:
            raise AuthorizationError("Portfolio onboarding is required")
        scope = OwnerScope(user.user_sk)
        request_hash = _request_hash(payload)
        client_request_id = payload.get("client_request_id") if isinstance(payload, dict) else None
        if isinstance(client_request_id, str):
            existing = self._transactions.read(
                scope,
                _transaction_id(scope.owner_user_sk, client_request_id),
            )
            if existing and not existing.request_hash and existing.transaction_type in _SECURITY_TYPES:
                if self._security_catalog is None:
                    raise RuntimeError("security catalog is unavailable")
                resolved_security = self._security_catalog.resolve(existing.security_code)
                migrated = PortfolioTransaction.from_payload(
                    scope.owner_user_sk,
                    payload,
                    now=datetime.fromisoformat(existing.created_at),
                    resolved_security=resolved_security,
                    request_hash=request_hash,
                    base_currency=user.base_currency,
                )
                migrated = self._transactions.replace(scope, migrated)
                if self._universe is not None:
                    self._universe.onboard(resolved_security)
                return migrated, False
            if existing and existing.request_hash:
                if existing.request_hash != request_hash:
                    raise ValueError("client_request_id was already used for different data")
                if existing.security_sk is not None and self._universe is not None:
                    self._universe.onboard(ResolvedSecurity(
                        security_sk=existing.security_sk,
                        ticker=existing.security_code,
                        isin=existing.security_isin,
                        company_name=existing.security_name or existing.security_code,
                        currency=existing.security_currency or existing.currency,
                        exchange=existing.security_exchange,
                        gics_sector=existing.security_sector,
                        country=existing.security_country,
                    ))
                return existing, False
        transaction_type = str(payload.get("transaction_type") or "").upper()
        resolved_security = None
        if transaction_type in _SECURITY_REFERENCE_TYPES:
            if self._security_catalog is None:
                raise RuntimeError("security catalog is unavailable")
            resolved_security = self._security_catalog.resolve(payload.get("security_code"))
        transaction = PortfolioTransaction.from_payload(
            scope.owner_user_sk,
            payload,
            now=self._clock() if self._clock else None,
            resolved_security=resolved_security,
            request_hash=request_hash,
            base_currency=user.base_currency,
        )
        bundle = [
            transaction,
            *PortfolioTransaction.linked_costs_from_payload(transaction, payload),
        ]
        stored, created = self._transactions.create_bundle(
            scope,
            bundle,
            self._validate_state_bundle,
        )
        if created and resolved_security is not None and self._universe is not None:
            self._universe.onboard(resolved_security)
        return stored, created

    def correct_transaction(self, principal_header, transaction_id: str, payload: dict):
        user = self._identity.product_user(principal_header)
        if not user.onboarded:
            raise AuthorizationError("Portfolio onboarding is required")
        scope = OwnerScope(user.user_sk)
        target = self._transactions.read(scope, transaction_id)
        if target is None:
            raise ValueError("transaction was not found")
        if target.linked_transaction_id is not None:
            raise ValueError("linked cost rows are corrected with their parent transaction")
        if target.corrects_transaction_id is not None:
            raise ValueError("a correction event cannot be corrected")

        request_hash = _request_hash(payload, transaction_id)
        client_request_id = payload.get("client_request_id") if isinstance(payload, dict) else None
        if isinstance(client_request_id, str):
            existing = self._transactions.read(
                scope,
                _transaction_id(scope.owner_user_sk, client_request_id),
            )
            if existing is not None:
                if existing.corrects_transaction_id != transaction_id:
                    raise ValueError("client_request_id was already used for different data")
                if existing.request_hash != request_hash:
                    raise ValueError("client_request_id was already used for different data")
                return existing, False

        audit_rows = self._transactions.list(scope)
        if any(row.corrects_transaction_id == transaction_id for row in audit_rows):
            raise ValueError("transaction was already corrected")

        transaction_type = str(payload.get("transaction_type") or "").upper()
        resolved_security = None
        if transaction_type in _SECURITY_REFERENCE_TYPES:
            if self._security_catalog is None:
                raise RuntimeError("security catalog is unavailable")
            resolved_security = self._security_catalog.resolve(payload.get("security_code"))
        correction = PortfolioTransaction.from_payload(
            scope.owner_user_sk,
            payload,
            now=self._clock() if self._clock else None,
            resolved_security=resolved_security,
            request_hash=request_hash,
            base_currency=user.base_currency,
            corrects_transaction_id=transaction_id,
        )
        bundle = [
            correction,
            *PortfolioTransaction.linked_costs_from_payload(correction, payload),
        ]
        stored, created = self._transactions.create_bundle(
            scope,
            bundle,
            self._validate_state_bundle,
        )
        if created and resolved_security is not None and self._universe is not None:
            self._universe.onboard(resolved_security)
        return stored, created

    def list_transactions(self, principal_header):
        return self._transactions.list(self._scope(principal_header))

    def annual_trade_count(self, principal_header, year: int) -> int:
        if year < 1900 or year > 9999:
            raise ValueError("invalid trade year")
        transactions = self._effective_transactions(
            self._transactions.list(self._scope(principal_header))
        )
        return sum(
            transaction.transaction_type in {"BUY", "SELL"}
            and date.fromisoformat(transaction.event_date).year == year
            for transaction in transactions
        )

    def lookup_security(self, principal_header, code: str) -> ResolvedSecurity:
        self._scope(principal_header)
        if self._security_catalog is None:
            raise RuntimeError("security catalog is unavailable")
        return self._security_catalog.resolve(code)

    def search_securities(self, principal_header, prefix: str) -> list[ResolvedSecurity]:
        self._scope(principal_header)
        if self._security_catalog is None:
            raise RuntimeError("security catalog is unavailable")
        return self._security_catalog.search(prefix)

    @staticmethod
    def _effective_transactions(
        transactions: list[PortfolioTransaction],
    ) -> list[PortfolioTransaction]:
        by_id = {transaction.transaction_id: transaction for transaction in transactions}
        corrected: set[str] = set()
        for transaction in sorted(
            transactions,
            key=lambda row: (row.created_at, row.transaction_id),
        ):
            target_id = transaction.corrects_transaction_id
            if target_id is None:
                continue
            target = by_id.get(target_id)
            if target is None:
                raise ValueError("corrected transaction was not found")
            if target.corrects_transaction_id is not None:
                raise ValueError("a correction event cannot be corrected")
            if target_id in corrected:
                raise ValueError("transaction was already corrected")
            corrected.add(target_id)
        return [
            transaction
            for transaction in transactions
            if transaction.transaction_id not in corrected
            and transaction.linked_transaction_id not in corrected
        ]

    @staticmethod
    def _validate_state_bundle(
        existing: list[PortfolioTransaction],
        candidates: list[PortfolioTransaction],
    ) -> None:
        staged = list(existing)
        for candidate in candidates:
            PortfolioService._validate_state(staged, candidate)
            staged.append(candidate)

    @staticmethod
    def _validate_state(
        existing: list[PortfolioTransaction],
        candidate: PortfolioTransaction,
    ) -> None:
        all_transactions = [*existing, candidate]
        created_at_by_id = {
            transaction.transaction_id: transaction.created_at
            for transaction in all_transactions
        }
        def ledger_order(transaction: PortfolioTransaction):
            effective_created_at = created_at_by_id.get(
                transaction.corrects_transaction_id or "",
                transaction.created_at,
            )
            opening_rank = int(transaction.transaction_type not in {
                "OPENING_CASH", "OPENING_POSITION",
            })
            return (
                transaction.event_date,
                opening_rank,
                effective_created_at,
                transaction.created_at,
                transaction.transaction_id,
            )
        ordered = sorted(
            PortfolioService._effective_transactions(all_transactions),
            key=ledger_order,
        )
        cash: dict[tuple[str, str], Decimal] = {}
        positions: dict[tuple[str, int | str], Decimal] = {}
        cash_activity: set[tuple[str, str]] = set()
        security_activity: set[tuple[str, int | str]] = set()
        opening_cash: set[tuple[str, str]] = set()
        opening_positions: set[tuple[str, int | str]] = set()

        for transaction in ordered:
            cash_key = (transaction.account_id, transaction.currency)
            security_key = (
                transaction.account_id,
                transaction.security_sk or transaction.security_code or "",
            )
            transaction_type = transaction.transaction_type

            if transaction_type == "OPENING_CASH":
                if cash_key in opening_cash:
                    raise ValueError("opening cash already exists for this account and currency")
                if cash_key in cash_activity:
                    raise ValueError("opening cash must precede other cash activity")
                opening_cash.add(cash_key)
            elif Decimal(transaction.cash_amount) != 0:
                cash_activity.add(cash_key)

            if transaction_type == "OPENING_POSITION":
                if security_key in opening_positions:
                    raise ValueError("opening position already exists for this account and security")
                if security_key in security_activity:
                    raise ValueError("opening position must precede other security activity")
                opening_positions.add(security_key)
            elif transaction_type in _SECURITY_REFERENCE_TYPES:
                security_activity.add(security_key)

            held = positions.get(security_key, Decimal("0"))
            if transaction_type == "DIVIDEND" and held <= 0:
                raise ValueError("dividend requires a currently held security")
            if transaction_type == "SELL":
                quantity = Decimal(transaction.quantity)
                if quantity > held:
                    raise ValueError("sell quantity exceeds held quantity")
                positions[security_key] = held - quantity
            elif transaction_type in {"OPENING_POSITION", "BUY"}:
                positions[security_key] = held + Decimal(transaction.quantity)

            balance = cash.get(cash_key, Decimal("0")) + Decimal(transaction.cash_amount)
            if balance < 0:
                raise ValueError(
                    f"insufficient cash in {transaction.currency} for {transaction_type}"
                )
            cash[cash_key] = balance

    def quick_summary(self, principal_header):
        transactions = self._effective_transactions(
            self.list_transactions(principal_header)
        )
        cash: dict[str, Decimal] = {}
        positions: dict[str, Decimal] = {}
        contributed_capital: dict[str, Decimal] = {}
        fees: dict[str, Decimal] = {}
        dividends: dict[str, Decimal] = {}
        interest: dict[str, Decimal] = {}
        for transaction in transactions:
            cash_amount = Decimal(transaction.cash_amount)
            fees_amount = Decimal(transaction.fees)
            _add_amount(cash, transaction.currency, cash_amount)
            _add_amount(fees, transaction.currency, fees_amount)
            if transaction.transaction_type == "FEE":
                cost_amount = (
                    Decimal(transaction.gross_amount)
                    if transaction.gross_amount is not None
                    else -cash_amount - fees_amount
                )
                _add_amount(fees, transaction.currency, cost_amount)
            if transaction.transaction_type in {"OPENING_POSITION", "BUY"}:
                positions[transaction.security_code] = positions.get(transaction.security_code, Decimal("0")) + Decimal(transaction.quantity)
            elif transaction.transaction_type == "SELL":
                positions[transaction.security_code] = positions.get(transaction.security_code, Decimal("0")) - Decimal(transaction.quantity)
            if transaction.transaction_type in {"OPENING_CASH", "DEPOSIT"}:
                _add_amount(
                    contributed_capital,
                    transaction.currency,
                    cash_amount + fees_amount,
                )
            elif transaction.transaction_type == "WITHDRAWAL":
                withdrawal_amount = -cash_amount - fees_amount
                _add_amount(contributed_capital, transaction.currency, -withdrawal_amount)
            elif transaction.transaction_type == "OPENING_POSITION":
                _add_amount(
                    contributed_capital,
                    transaction.currency,
                    Decimal(transaction.gross_amount)
                    if transaction.gross_amount is not None
                    else Decimal(transaction.quantity) * Decimal(transaction.price),
                )
            elif (
                transaction.transaction_type == "FEE"
                and transaction.linked_transaction_id is not None
                and not transaction.affects_cash
            ):
                _add_amount(
                    contributed_capital,
                    transaction.currency,
                    Decimal(transaction.gross_amount),
                )
            elif transaction.transaction_type == "DIVIDEND":
                _add_amount(
                    dividends,
                    transaction.currency,
                    Decimal(transaction.gross_amount)
                    if transaction.gross_amount is not None
                    else cash_amount + fees_amount,
                )
            elif transaction.transaction_type == "INTEREST":
                _add_amount(interest, transaction.currency, cash_amount + fees_amount)
        valuation = self.portfolio_summary(principal_header)
        valuation_ready = valuation["status"] in {"ready", "stale"}
        valuation_currency = valuation["base_currency"]
        total_value = valuation["total_value_base"]
        total_earnings = valuation["total_earnings_base"]
        updated_dates = [transaction.created_at[:10] for transaction in transactions]
        if valuation.get("valuation_as_of"):
            updated_dates.append(valuation["valuation_as_of"])
        return {
            "cash_by_currency": _money_map(cash),
            "positions": [
                {"security_code": security_code, "quantity": _quantity(quantity)}
                for security_code, quantity in sorted(positions.items())
                if quantity != 0
            ],
            "transaction_count": len(transactions),
            "updated_on": max(updated_dates, default=None),
            "net_contributed_capital_by_currency": _money_map(contributed_capital),
            "total_fees_by_currency": _money_map(fees),
            "dividends_by_currency": _money_map(dividends),
            "interest_by_currency": _money_map(interest),
            "total_value": {
                "status": valuation["status"] if valuation_ready else "pending_market_valuation",
                "value_by_currency": {valuation_currency: total_value} if valuation_ready else None,
                "reason": None if valuation_ready else "Total value requires market prices and FX valuation.",
            },
            "earnings": {
                "status": valuation["status"] if valuation_ready else "pending_market_valuation",
                "value_by_currency": {valuation_currency: total_earnings} if valuation_ready else None,
                "reason": None if valuation_ready else "Current earnings requires market prices and FX valuation.",
            },
        }

    def portfolio_summary(self, principal_header):
        user = self._identity.product_user(principal_header)
        if not user.onboarded:
            raise AuthorizationError("Portfolio onboarding is required")
        transactions = self._effective_transactions(
            self._transactions.list(OwnerScope(user.user_sk))
        )
        base_currency = user.base_currency
        if not transactions:
            return {
                "status": "empty",
                "base_currency": base_currency,
                "valuation_as_of": None,
                "total_cash_base": "0.00",
                "total_stocks_base": "0.00",
                "total_value_base": "0.00",
                "net_contributed_capital_base": "0.00",
                "total_earnings_base": "0.00",
                "cash_weight": None,
                "holdings": [],
                "exposures": {"sector": [], "country": [], "currency": []},
                "coverage": {"missing_prices": [], "missing_fx": [], "oldest_price_date": None},
            }
        if self._market_data is None:
            raise RuntimeError("market data is unavailable")

        cash_by_currency: dict[str, Decimal] = {}
        capital_base = Decimal("0")
        positions: dict[int | str, dict] = {}
        missing_fx: set[str] = set()
        missing_capital_fx: set[str] = set()
        valuation_dates: list[str] = []
        for transaction in transactions:
            cash_amount = Decimal(transaction.cash_amount)
            fees_amount = Decimal(transaction.fees)
            _add_amount(cash_by_currency, transaction.currency, cash_amount)
            capital_amount = None
            if transaction.transaction_type in {"OPENING_CASH", "DEPOSIT"}:
                capital_amount = cash_amount + fees_amount
            elif transaction.transaction_type == "WITHDRAWAL":
                capital_amount = cash_amount + fees_amount
            elif transaction.transaction_type == "OPENING_POSITION":
                capital_amount = (
                    Decimal(transaction.gross_amount)
                    if transaction.gross_amount is not None
                    else Decimal(transaction.quantity) * Decimal(transaction.price)
                )
            elif (
                transaction.transaction_type == "FEE"
                and transaction.linked_transaction_id is not None
                and not transaction.affects_cash
            ):
                capital_amount = Decimal(transaction.gross_amount)
            if capital_amount is not None:
                conversion = None
                if (
                    transaction.fx_rate_to_base is not None
                    and transaction.base_currency == base_currency
                ):
                    conversion = (
                        capital_amount * Decimal(transaction.fx_rate_to_base),
                        transaction.event_date,
                    )
                else:
                    conversion = self._convert(
                        capital_amount,
                        transaction.currency,
                        base_currency,
                        transaction.event_date,
                    )
                if conversion is None:
                    pair = f"{transaction.currency}/{base_currency}"
                    missing_fx.add(pair)
                    missing_capital_fx.add(pair)
                else:
                    capital_base += conversion[0]
                    if conversion[1]:
                        valuation_dates.append(conversion[1])
            if transaction.transaction_type in _SECURITY_TYPES:
                key = transaction.security_sk or transaction.security_code
                position = positions.setdefault(key, {
                    "security_sk": transaction.security_sk,
                    "ticker": transaction.security_code,
                    "isin": transaction.security_isin,
                    "company_name": transaction.security_name or transaction.security_code,
                    "currency": transaction.security_currency or transaction.currency,
                    "exchange": transaction.security_exchange,
                    "sector": transaction.security_sector,
                    "country": transaction.security_country,
                    "quantity": Decimal("0"),
                })
                direction = Decimal("-1") if transaction.transaction_type == "SELL" else Decimal("1")
                position["quantity"] += direction * Decimal(transaction.quantity)

        cash_base = Decimal("0")
        cash_exposure_base: dict[str, Decimal] = {}
        missing_cash_fx: set[str] = set()
        for currency, amount in cash_by_currency.items():
            conversion = self._convert(amount, currency, base_currency)
            if conversion is None:
                pair = f"{currency}/{base_currency}"
                missing_fx.add(pair)
                missing_cash_fx.add(pair)
            else:
                cash_base += conversion[0]
                _add_amount(cash_exposure_base, currency, conversion[0])
                if conversion[1]:
                    valuation_dates.append(conversion[1])

        missing_prices: set[str] = set()
        stocks_base = Decimal("0")
        sector_exposure_base: dict[str, Decimal] = {}
        country_exposure_base: dict[str, Decimal] = {}
        currency_exposure_base = dict(cash_exposure_base)
        holdings = []
        for position in positions.values():
            if position["quantity"] == 0:
                continue
            quote = self._market_data.quote(position["ticker"], position["security_sk"])
            market_value_base = None
            latest_price = None
            price_as_of = None
            if not quote:
                missing_prices.add(position["ticker"])
            else:
                latest_price = Decimal(quote["price"])
                price_as_of = quote.get("as_of")
                quote_currency = str(quote.get("currency") or position["currency"]).upper()
                conversion = self._convert(
                    position["quantity"] * latest_price,
                    quote_currency,
                    base_currency,
                    price_as_of,
                )
                if conversion is None:
                    missing_fx.add(f"{quote_currency}/{base_currency}")
                else:
                    market_value_base = conversion[0]
                    stocks_base += conversion[0]
                    _add_amount(
                        sector_exposure_base,
                        position["sector"] or "Unknown",
                        conversion[0],
                    )
                    _add_amount(
                        country_exposure_base,
                        position["country"] or "Unknown",
                        conversion[0],
                    )
                    _add_amount(currency_exposure_base, quote_currency, conversion[0])
                    if price_as_of:
                        valuation_dates.append(price_as_of)
                    if conversion[1]:
                        valuation_dates.append(conversion[1])
            holdings.append({
                "security_sk": position["security_sk"],
                "ticker": position["ticker"],
                "isin": position["isin"],
                "company_name": position["company_name"],
                "currency": position["currency"],
                "price_currency": quote_currency if quote else None,
                "exchange": position["exchange"],
                "sector": position["sector"],
                "country": position["country"],
                "quantity": _quantity(position["quantity"]),
                "latest_price": _money(latest_price) if latest_price is not None else None,
                "price_as_of": price_as_of,
                "market_value_base": _money(market_value_base) if market_value_base is not None else None,
                "weight": None,
            })

        complete = not missing_prices and not missing_fx
        total_value = cash_base + stocks_base if complete else None
        if total_value and total_value != 0:
            for holding in holdings:
                if holding["market_value_base"] is not None:
                    holding["weight"] = _quantity(
                        Decimal(holding["market_value_base"]) / total_value
                    )
        def exposure_rows(values: dict[str, Decimal]) -> list[dict]:
            if total_value is None or total_value == 0:
                return []
            return [
                {
                    "name": name,
                    "market_value_base": _money(value),
                    "weight": _quantity(value / total_value),
                }
                for name, value in sorted(values.items())
                if value != 0
            ]
        oldest_price_date = min(valuation_dates) if valuation_dates else None
        now = self._clock() if self._clock else datetime.now(timezone.utc)
        stale = bool(
            complete
            and oldest_price_date
            and (now.date() - date.fromisoformat(oldest_price_date)).days > 5
        )
        return {
            "status": "stale" if stale else "ready" if complete else "pending_ingestion",
            "base_currency": base_currency,
            "valuation_as_of": min(valuation_dates) if valuation_dates else None,
            "total_cash_base": _money(cash_base) if not missing_cash_fx else None,
            "total_stocks_base": _money(stocks_base) if complete else None,
            "total_value_base": _money(total_value) if total_value is not None else None,
            "net_contributed_capital_base": _money(capital_base) if not missing_capital_fx else None,
            "total_earnings_base": _money(total_value - capital_base) if total_value is not None and not missing_fx else None,
            "cash_weight": _quantity(cash_base / total_value) if total_value else None,
            "holdings": sorted(holdings, key=lambda holding: holding["ticker"]),
            "exposures": {
                "sector": exposure_rows(sector_exposure_base),
                "country": exposure_rows(country_exposure_base),
                "currency": exposure_rows(currency_exposure_base),
            },
            "coverage": {
                "missing_prices": sorted(missing_prices),
                "missing_fx": sorted(missing_fx),
                "oldest_price_date": oldest_price_date,
            },
        }

    def _convert(
        self,
        amount: Decimal,
        from_currency: str,
        to_currency: str,
        as_of: str | None = None,
    ) -> tuple[Decimal, str | None] | None:
        rate = self._market_data.fx_rate(from_currency, to_currency, as_of)
        if not rate:
            return None
        return amount * Decimal(rate["rate"]), rate.get("as_of")
