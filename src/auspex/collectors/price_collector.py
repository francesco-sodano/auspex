"""`PriceCollector` — daily EOD OHLCV from the price provider (arc42 §5.3)."""

from __future__ import annotations

from datetime import date, timedelta

from auspex.collectors.base import CollectorResult, PriceSink, WatermarkStore, watermark_key
from auspex.models.market import PriceBar
from auspex.providers.base import PriceProvider

COLLECTOR_NAME = "price"


class PriceCollector:
    def __init__(self, provider: PriceProvider, sink: PriceSink, watermarks: WatermarkStore) -> None:
        self._provider = provider
        self._sink = sink
        self._watermarks = watermarks

    async def collect(self, security_id: str, ticker: str, default_since: date) -> CollectorResult:
        key = watermark_key(COLLECTOR_NAME, security_id)
        watermark = await self._watermarks.get_watermark(key)
        since = date.fromisoformat(watermark) + timedelta(days=1) if watermark else default_since

        result = CollectorResult(collector=COLLECTOR_NAME, security_id=security_id)
        try:
            bars = await self._provider.get_daily_prices(ticker, since)
        except Exception as exc:  # noqa: BLE001 - degrade this security, do not abort the run
            result.degraded = True
            result.error = str(exc)
            return result

        result.items_seen = len(bars)
        latest_date = None
        for bar in bars:
            price_bar = PriceBar(
                id=f"{security_id}:{bar.session_date.isoformat()}",
                security_id=security_id,
                session_date=bar.session_date,
                open_raw=str(bar.open_raw),
                high_raw=str(bar.high_raw),
                low_raw=str(bar.low_raw),
                close_raw=str(bar.close_raw),
                volume=bar.volume,
                close_adjusted=str(bar.close_adjusted),
                adjustment_factor=str(bar.adjustment_factor),
                split_factor=str(bar.split_factor),
                dividend_amount=str(bar.dividend_amount),
            )
            await self._sink.upsert_price_bar(price_bar)
            result.items_written += 1
            if latest_date is None or bar.session_date > latest_date:
                latest_date = bar.session_date

        if latest_date is not None:
            await self._watermarks.set_watermark(key, latest_date.isoformat())
        return result
