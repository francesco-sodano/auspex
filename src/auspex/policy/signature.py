"""Versioned recommendation decision signature (arc42 §5.6, §5.7).

A *decision signature* is a stable fingerprint of everything that makes one
recommendation materially different from another. It exists so a user's
disposition ("I rejected this", "ask me again later") can suppress the *same*
decision on subsequent nights without also suppressing a genuinely new one.

The signature covers exactly the inputs a user would consider material:

``action``
    BUY / ADD / TRIM / SELL / HOLD_*.
``quantity`` and ``notional``
    what they would actually have to trade, bucketed so that sub-material
    drift (a few francs of price noise) does not resurface an identical ask.
``target weight``
    the allocation the action is steering toward, bucketed likewise.
``readiness``
    whether the action is immediately executable.
``gates``
    the pass/fail shape of the gate cascade — a decision that now passes a
    gate it previously failed is a different decision.
``material evidence``
    the evidence fingerprint the action rests on (score percentile band,
    cohort confidence, direction).

Bucketing matters: an unbucketed notional would change every single night on
price noise alone, which would defeat suppression entirely. Quantities are
exact (they are whole units the user must trade); money and weights are
rounded to a coarse band.

The signature is versioned (:data:`SIGNATURE_VERSION`) and rendered as
``"<version>:<sha256>"``. Bumping the version deliberately invalidates every
stored suppression, which is the correct behaviour when the *meaning* of a
signature changes.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from auspex.models.common import sha256_hex
from auspex.models.enums import Action

SIGNATURE_VERSION = "v1"

#: Actions the user can actually act on. Everything else is a hold.
ACTIONABLE = frozenset({Action.BUY, Action.ADD, Action.TRIM, Action.SELL})

#: Money is bucketed to CHF 50 and weights to 0.5 percentage points so that
#: ordinary day-to-day price drift does not re-raise an identical decision.
NOTIONAL_BUCKET_CHF = Decimal("50")
WEIGHT_BUCKET_PCT = Decimal("0.5")

#: Share counts below this are compared exactly — at that size a single share
#: is a material fraction of the trade. Above it, quantities are banded to two
#: significant figures, because an exact count derived from a drifting price
#: would change nightly and defeat suppression entirely (a rejected "buy 100"
#: must not resurface tomorrow as "buy 101").
QUANTITY_EXACT_BELOW = Decimal("10")
QUANTITY_SIGNIFICANT_FIGURES = 2


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _bucket(value: object, bucket: Decimal) -> str:
    parsed = _decimal_or_none(value)
    if parsed is None:
        return "-"
    if bucket <= 0:  # pragma: no cover - defensive
        return str(parsed)
    steps = (parsed / bucket).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return str(steps * bucket)


def quantity_band(value: object) -> str:
    """Stable band for a share count.

    Small counts compare exactly. Larger ones are rounded to
    :data:`QUANTITY_SIGNIFICANT_FIGURES` significant figures, because the
    suggested quantity is ``floor(notional / price)`` and therefore crosses an
    integer boundary on ordinary price noise — hashing it exactly would let a
    rejected decision resurface the next night unchanged in substance.
    """

    parsed = _decimal_or_none(value)
    if parsed is None:
        return "-"
    magnitude = abs(parsed)
    if magnitude < QUANTITY_EXACT_BELOW:
        return str(parsed.normalize())
    digits = len(magnitude.to_integral_value(rounding=ROUND_HALF_UP).as_tuple().digits)
    step = Decimal(10) ** max(0, digits - QUANTITY_SIGNIFICANT_FIGURES)
    banded = (parsed / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step
    return str(banded.normalize())


def gate_fingerprint(gate_trace: Iterable[object]) -> str:
    """Deterministic ``gate=pass|fail`` shape of the cascade.

    Sorted by gate name so trace ordering changes (a refactor of the gate
    evaluation order) do not by themselves invalidate suppressions.
    """

    entries: list[str] = []
    for result in gate_trace or []:
        name = getattr(result, "gate", None)
        passed = getattr(result, "passed", None)
        if name is None:
            continue
        entries.append(f"{name}={'1' if passed else '0'}")
    return ",".join(sorted(entries))


def evidence_fingerprint(
    *,
    percentile: int | None,
    cohort_confidence: object | None,
    direction: object | None,
    coverage: object | None = None,
) -> str:
    """Fingerprint of the material evidence behind the action.

    The percentile is banded into deciles and coverage into tenths: a
    one-point score move is not a new decision, a ten-point move is.
    """

    band = "-" if percentile is None else str(int(percentile) // 10)
    coverage_decimal = _decimal_or_none(coverage)
    coverage_band = "-" if coverage_decimal is None else str(int(coverage_decimal * 10))
    confidence = getattr(cohort_confidence, "value", cohort_confidence)
    direction_value = getattr(direction, "value", direction)
    return "|".join(
        [
            f"pct={band}",
            f"cov={coverage_band}",
            f"conf={confidence if confidence is not None else '-'}",
            f"dir={direction_value if direction_value is not None else '-'}",
        ]
    )


def is_ready(action: Action | str) -> bool:
    """Readiness — whether the action is something to execute now."""

    value = action.value if isinstance(action, Action) else str(action)
    return value in {member.value for member in ACTIONABLE}


def compute_decision_signature(
    *,
    action: Action | str,
    security_id: str,
    suggested_quantity: object = None,
    suggested_trade_chf: object = None,
    target_weight_pct: object = None,
    gate_trace: Sequence[object] | None = None,
    evidence: str | None = None,
) -> str:
    """Return ``"<version>:<sha256 hex>"`` for one decision."""

    action_value = action.value if isinstance(action, Action) else str(action)
    payload = "\n".join(
        [
            f"version={SIGNATURE_VERSION}",
            f"security={security_id}",
            f"action={action_value}",
            f"ready={'1' if is_ready(action_value) else '0'}",
            f"quantity={quantity_band(suggested_quantity)}",
            f"notional={_bucket(suggested_trade_chf, NOTIONAL_BUCKET_CHF)}",
            f"target={_bucket(target_weight_pct, WEIGHT_BUCKET_PCT)}",
            f"gates={gate_fingerprint(gate_trace or [])}",
            f"evidence={evidence or '-'}",
        ]
    )
    return f"{SIGNATURE_VERSION}:{sha256_hex(payload)}"
