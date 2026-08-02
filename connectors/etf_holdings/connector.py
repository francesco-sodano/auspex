"""Alpha Vantage ETF profile/holdings connector for theme ground-truth seeds."""
import hashlib
import json
import math
import os
import time
from datetime import date
from pathlib import Path
from typing import Optional

from shared.base_connector import BaseConnector
from shared.models import Batch, Watermark
from shared.retry import http_get

from alpha_vantage.mapping import utc_now_iso

_AV_URL = "https://www.alphavantage.co/query"
_DEFAULT_REQUESTS_PER_MINUTE = 5


def _load_theme_catalog() -> dict:
    path = Path(__file__).resolve().parents[1] / "shared" / "themes_seed.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    _validate_theme_catalog(catalog)
    return catalog


def _validate_theme_catalog(catalog: dict) -> None:
    themes = catalog.get("themes") or []
    theme_ids = [str(theme.get("theme_id") or "").strip() for theme in themes]
    if not themes or any(not theme_id for theme_id in theme_ids):
        raise ValueError("Theme catalog requires nonempty theme IDs")
    if len(theme_ids) != len(set(theme_ids)):
        raise ValueError("Theme catalog contains duplicate theme IDs")
    for theme in themes:
        components = theme.get("components") or []
        symbols = [str(component.get("etf_symbol") or "").strip().upper() for component in components]
        weights = [float(component.get("blend_weight") or 0) for component in components]
        if not theme.get("name") or not theme.get("benchmark_symbol") or not components:
            raise ValueError(f"Theme {theme['theme_id']} is missing required metadata")
        if any(not symbol for symbol in symbols) or len(symbols) != len(set(symbols)):
            raise ValueError(f"Theme {theme['theme_id']} has invalid component ETFs")
        if any(not math.isfinite(weight) or weight <= 0 for weight in weights):
            raise ValueError(f"Theme {theme['theme_id']} has invalid blend weights")
        if not math.isclose(sum(weights), 1.0, abs_tol=1e-9):
            raise ValueError(f"Theme {theme['theme_id']} blend weights must sum to 1")


def _component_map(catalog: dict) -> dict[str, list[dict]]:
    components: dict[str, list[dict]] = {}
    for theme in catalog["themes"]:
        for component in theme["components"]:
            symbol = str(component["etf_symbol"]).upper()
            components.setdefault(symbol, []).append({
                "theme_id": theme["theme_id"],
                "theme_name": theme["name"],
                "benchmark_symbol": theme["benchmark_symbol"],
                "blend_weight": component["blend_weight"],
            })
    return components


class EtfHoldingsConnector(BaseConnector):
    source_id = "etf_holdings"
    schema_version = 1

    def __init__(self, cp, bw, etf_symbols: list = None, source_config: Optional[dict] = None) -> None:
        super().__init__(cp, bw, source_config=source_config)
        self._api_key = os.environ["ALPHAVANTAGE_API_KEY"]
        self._theme_catalog = _load_theme_catalog()
        self._component_map = _component_map(self._theme_catalog)
        self._etf_symbols = etf_symbols if etf_symbols is not None else (source_config or {}).get("etf_symbols") or []
        env_rpm = os.environ.get("AV_RPM")
        rpm = int(env_rpm) if env_rpm else self._requests_per_minute(_DEFAULT_REQUESTS_PER_MINUTE)
        self._min_interval_s = 60 / rpm

    def fetch(self, since: Optional[Watermark]) -> Batch:
        today = date.today().isoformat()
        fetched_at = utc_now_iso()
        records = []
        for symbol in sorted({str(symbol).upper() for symbol in self._etf_symbols if symbol}):
            started_at = time.monotonic()
            payload = http_get(_AV_URL, params={"function": "ETF_PROFILE", "symbol": symbol, "apikey": self._api_key}).json()
            if "Error Message" in payload or "Note" in payload or "Information" in payload:
                message = payload.get("Error Message") or payload.get("Note") or payload.get("Information")
                raise RuntimeError(f"Alpha Vantage ETF_PROFILE returned: {message}")
            elapsed = time.monotonic() - started_at
            if elapsed < self._min_interval_s:
                time.sleep(self._min_interval_s - elapsed)
            records.append({
                "function": "ETF_PROFILE",
                "context": {
                    "symbol": symbol,
                    "theme_components": self._component_map.get(symbol, []),
                    "listing_scope": self._theme_catalog["listing_scope"],
                    "theme_catalog_version": self._theme_catalog["schema_version"],
                },
                "fetched_at": fetched_at,
                "payload": payload,
            })

        new_wm = Watermark(source_id=self.source_id, last_event_ts=today, last_cursor=today)
        identity = json.dumps(
            {"symbols": sorted(self._etf_symbols), "catalog": self._theme_catalog},
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        return Batch(
            records=records,
            new_wm=new_wm,
            window=f"{today}-etfs-{len(records)}-{digest}",
            partition_date=today,
        )
