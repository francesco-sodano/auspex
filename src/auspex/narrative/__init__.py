"""Daily narrative generator (arc42 §5.9). Cache key: package_fingerprint + model_version + prompt_version."""

from __future__ import annotations

from auspex.narrative.fingerprint import compute_package_fingerprint
from auspex.narrative.generator import NarrativeGenerator, NarrativeSink

__all__ = ["compute_package_fingerprint", "NarrativeGenerator", "NarrativeSink"]
