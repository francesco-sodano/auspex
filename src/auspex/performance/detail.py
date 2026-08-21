"""Detail payload construction for versioned metrics (arc42 §5.8).

``PerformanceMetric.detail`` is a flat ``dict[str, str]`` so that stored rows
stay schema-free and forward compatible. New statistics are therefore published
as strings inside ``detail`` and stamped with ``DETAILED_METRICS_VERSION``:
consumers can tell which vintage of the methodology produced a row without the
container schema changing, and the pre-existing ``value``/``sample_size`` fields
keep their original meaning for the existing API contract.
"""

from __future__ import annotations

from decimal import Decimal

DETAILED_METRICS_VERSION = "2.0.0"

_DETAIL_PLACES = Decimal("0.0000000001")


def decimal_str(value: Decimal) -> str:
    """Fixed-precision string so stored detail is byte-stable across runs."""

    quantized = value.quantize(_DETAIL_PLACES)
    return f"{quantized.normalize():f}" if quantized != 0 else "0"


def detail_payload(**items: Decimal | int | str | bool | None) -> dict[str, str]:
    """Flatten keyword values into stringified detail, dropping absent entries."""

    payload: dict[str, str] = {}
    for key, value in items.items():
        if value is None:
            continue
        if isinstance(value, bool):
            payload[key] = "true" if value else "false"
        elif isinstance(value, Decimal):
            payload[key] = decimal_str(value)
        else:
            payload[key] = str(value)
    return payload
