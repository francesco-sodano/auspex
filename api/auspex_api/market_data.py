from decimal import Decimal
import re
from typing import TYPE_CHECKING

from .portfolio import ResolvedSecurity, _quantity

if TYPE_CHECKING:
    from azure.cosmos import ContainerProxy


_ISIN_PATTERN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_SECURITY_CODE_PATTERN = re.compile(r"^[A-Z0-9.-]{1,15}$")


def _read(container, document_id: str) -> dict | None:
    try:
        return container.read_item(item=document_id, partition_key=document_id)
    except Exception as exc:
        if getattr(exc, "status_code", None) != 404:
            raise
        return None


def _security(document: dict) -> ResolvedSecurity:
    return ResolvedSecurity(
        security_sk=int(document["security_sk"]),
        ticker=str(document["ticker"]).upper(),
        isin=document.get("isin"),
        company_name=document["company_name"],
        currency=str(document["currency"]).upper(),
        exchange=document.get("exchange"),
        gics_sector=document.get("gics_sector"),
        country=document.get("country"),
    )


class CosmosSecurityCatalog:
    def __init__(self, container: "ContainerProxy") -> None:
        self._container = container

    def resolve(self, code: str) -> ResolvedSecurity:
        normalized = str(code or "").strip().upper()
        if not _SECURITY_CODE_PATTERN.fullmatch(normalized):
            raise ValueError("invalid security_code")
        prefix = "isin" if _ISIN_PATTERN.fullmatch(normalized) else "ticker"
        document = _read(self._container, f"{prefix}:{normalized}")
        if document is None:
            raise ValueError("security was not found")
        return _security(document)

    def get(self, security_sk: int) -> ResolvedSecurity | None:
        document = _read(self._container, f"security:{int(security_sk)}")
        return _security(document) if document else None

    def search(self, prefix: str) -> list[ResolvedSecurity]:
        normalized = str(prefix or "").strip().upper()
        if not _SECURITY_CODE_PATTERN.fullmatch(normalized):
            raise ValueError("invalid security prefix")
        documents = self._container.query_items(
            query=(
                "SELECT TOP 8 * FROM c WHERE STARTSWITH(c.id, @prefix) "
                "ORDER BY c.id"
            ),
            parameters=[{"name": "@prefix", "value": f"ticker:{normalized}"}],
            enable_cross_partition_query=True,
        )
        return [_security(document) for document in documents]


class CosmosUniverseRepository:
    def __init__(self, container: "ContainerProxy") -> None:
        self._container = container

    def onboard(self, security: ResolvedSecurity) -> None:
        self._container.upsert_item({
            "id": security.ticker,
            "symbol": security.ticker,
            "security_sk": security.security_sk,
            "currency": security.currency,
            "source": "portfolio",
            "active": True,
        })


class CosmosMarketDataRepository:
    def __init__(self, container: "ContainerProxy") -> None:
        self._container = container

    def quote(self, ticker: str, security_sk: int | None = None) -> dict | None:
        if security_sk is not None:
            document = _read(self._container, f"quote:security:{int(security_sk)}")
            if document is not None:
                return document
        return _read(self._container, f"quote:{str(ticker).strip().upper()}")

    def fx_rate(
        self,
        from_currency: str,
        to_currency: str,
        as_of: str | None = None,
    ) -> dict | None:
        source = str(from_currency).upper()
        target = str(to_currency).upper()
        if source == target:
            return {"rate": "1.00000000", "as_of": as_of}
        direct = self._rate(source, target, as_of)
        if direct:
            return direct
        inverse = self._rate(target, source, as_of)
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

    def _rate(self, source: str, target: str, as_of: str | None) -> dict | None:
        pair = f"{source}{target}"
        if as_of is None:
            return _read(self._container, f"fx:{pair}")
        documents = self._container.query_items(
            query=(
                "SELECT TOP 1 * FROM c WHERE c.kind = 'fx' "
                "AND c.pair = @pair AND c.as_of <= @as_of ORDER BY c.as_of DESC"
            ),
            parameters=[
                {"name": "@pair", "value": pair},
                {"name": "@as_of", "value": as_of},
            ],
            enable_cross_partition_query=True,
        )
        return next(iter(documents), None)

    def _direct_or_inverse(self, source: str, target: str, as_of: str | None) -> dict | None:
        direct = self._rate(source, target, as_of)
        if direct:
            return direct
        inverse = self._rate(target, source, as_of)
        if not inverse:
            return None
        return {
            "rate": _quantity(Decimal("1") / Decimal(inverse["rate"])),
            "as_of": inverse.get("as_of"),
        }