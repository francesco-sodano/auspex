"""Two-channel extraction (arc42 §5.4): Channel A (scoring labels), Channel B
(prose digest + comparative diff), shared section targeting and cache keys.
"""

from __future__ import annotations

from auspex.extraction.cache import channel_a_cache_key, channel_b_cache_key
from auspex.extraction.channel_a import ChannelAExtractionSink, ChannelAExtractor
from auspex.extraction.channel_b import ChannelBDigestSink, ChannelBExtractor
from auspex.extraction.sections import WHOLE_DOCUMENT_FORMS, Section, target_sections

__all__ = [
    "channel_a_cache_key",
    "channel_b_cache_key",
    "ChannelAExtractionSink",
    "ChannelAExtractor",
    "ChannelBDigestSink",
    "ChannelBExtractor",
    "Section",
    "WHOLE_DOCUMENT_FORMS",
    "target_sections",
]
