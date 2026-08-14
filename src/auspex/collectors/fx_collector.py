"""`FxCollector` — USD/CHF daily close (arc42 §5.3).

Ledger only, never scoring (arc42 §8.2: FX never enters the scoring engine).
"""

from __future__ import annotations

from datetime import date, timedelta

from auspex.collectors.base import CollectorResult, FxSink, WatermarkStore, watermark_key
from auspex.models.market import FxRate
from auspex.providers.base import FxProvider

COLLECTOR_NAME = "fx"
FX_WATERMARK_KEY = "usdchf"


class FxCollector:
    def __init__(self, provider: FxProvider, sink: FxSink, watermarks: WatermarkStore) -> None:
        self._provider = provider
        self._sink = sink
        self._watermarks = watermarks

    async def collect(self, default_since: date) -> CollectorResult:
        key = watermark_key(COLLECTOR_NAME, FX_WATERMARK_KEY)
        watermark = await self._watermarks.get_watermark(key)
        since = date.fromisoformat(watermark) + timedelta(days=1) if watermark else default_since

        result = CollectorResult(collector=COLLECTOR_NAME, security_id=None)
        try:
            rates = await self._provider.get_usd_chf(since)
        except Exception as exc:  # noqa: BLE001 - a missing FX rate defers revaluation only
            result.degraded = True
            result.error = str(exc)
            return result

        result.items_seen = len(rates)
        latest_date = None
        for rate in rates:
            fx_rate = FxRate(
                id=f"USDCHF:{rate.session_date.isoformat()}",
                pair=rate.pair,
                session_date=rate.session_date,
                close_rate=str(rate.close_rate),
            )
            await self._sink.upsert_fx_rate(fx_rate)
            result.items_written += 1
            if latest_date is None or rate.session_date > latest_date:
                latest_date = rate.session_date

        if latest_date is not None:
            await self._watermarks.set_watermark(key, latest_date.isoformat())
        return result
