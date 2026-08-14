"""Six ingestion collectors (arc42 §5.3): price, FX, filings, insiders, news, fundamentals."""

from __future__ import annotations

from auspex.collectors.base import (
    BlobSink,
    CollectorResult,
    DocumentSink,
    FundamentalSink,
    FxSink,
    PriceSink,
    WatermarkStore,
    watermark_key,
)
from auspex.collectors.filing_collector import FilingCollector
from auspex.collectors.fundamental_collector import FundamentalCollector, build_snapshots_by_accession
from auspex.collectors.fx_collector import FxCollector
from auspex.collectors.insider_collector import InsiderCollector, parse_form4_xml
from auspex.collectors.news_collector import NewsCollector
from auspex.collectors.price_collector import PriceCollector

__all__ = [
    "BlobSink",
    "CollectorResult",
    "DocumentSink",
    "FundamentalSink",
    "FxSink",
    "PriceSink",
    "WatermarkStore",
    "watermark_key",
    "FilingCollector",
    "FundamentalCollector",
    "build_snapshots_by_accession",
    "FxCollector",
    "InsiderCollector",
    "parse_form4_xml",
    "NewsCollector",
    "PriceCollector",
]
