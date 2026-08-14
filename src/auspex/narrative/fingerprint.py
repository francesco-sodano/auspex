"""Package fingerprint (arc42 §5.9 narrative cache key: `package_fingerprint + model_version + prompt_version`)."""

from __future__ import annotations

import json

from auspex.models.common import sha256_hex


def compute_package_fingerprint(package: dict) -> str:
    """Stable fingerprint of the deterministic score/leg/action package.

    The prior narrative is deliberately never part of this input — narrative
    output must depend only on today's package, or replaying a past date
    would produce a different narrative on every re-run (arc42 §5.9).
    """

    canonical = json.dumps(package, sort_keys=True, default=str)
    return f"sha256:{sha256_hex(canonical)}"
