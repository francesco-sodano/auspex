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


def fx_watermark_key(pair: str) -> str:
    return pair.strip().lower()


class FxCollector:
    def __init__(self, provider: FxProvider, sink: FxSink, watermarks: WatermarkStore) -> None:
        self._provider = provider
        self._sink = sink
        self._watermarks = watermarks

    async def collect(
        self,
        default_since: date,
        *,
        pairs: tuple[str, ...] = ("USDCHF",),
    ) -> CollectorResult:
        result = CollectorResult(collector=COLLECTOR_NAME, security_id=None)
        errors: list[str] = []
        for pair in dict.fromkeys(value.strip().upper() for value in pairs):
            key = watermark_key(COLLECTOR_NAME, fx_watermark_key(pair))
            watermark = await self._watermarks.get_watermark(key)
            since = (
                date.fromisoformat(watermark) + timedelta(days=1)
                if watermark
                else default_since
            )
            try:
                get_daily_fx = getattr(self._provider, "get_daily_fx", None)
                if get_daily_fx is not None:
                    rates = await get_daily_fx(pair, since)
                elif pair == "USDCHF":
                    rates = await self._provider.get_usd_chf(since)
                else:
                    raise RuntimeError(
                        f"configured FX provider does not support {pair}"
                    )
            except Exception as exc:  # noqa: BLE001 - isolate one FX pair
                errors.append(f"{pair}: {exc}")
                continue

            result.items_seen += len(rates)
            latest_date = None
            for rate in rates:
                fx_rate = FxRate(
                    id=f"{rate.pair}:{rate.session_date.isoformat()}",
                    pair=rate.pair,
                    session_date=rate.session_date,
                    close_rate=str(rate.close_rate),
                )
                await self._sink.upsert_fx_rate(fx_rate)
                result.items_written += 1
                if latest_date is None or rate.session_date > latest_date:
                    latest_date = rate.session_date
            if latest_date is not None:
                await self._watermarks.set_watermark(
                    key,
                    latest_date.isoformat(),
                )
        if errors:
            result.degraded = True
            result.error = "; ".join(errors)
        return result
