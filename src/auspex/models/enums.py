"""Shared enumerations used across models, scoring, policy, and extraction.

Every enum here corresponds to a fixed vocabulary in ``doc/auspex-arc42.md``.
Numeric mappings for the Channel A enums live in ``config/label_mappings.yaml``
and ``config/weights.yaml`` — never hard-coded beside the enum itself — so that
the mapping is versioned independently of the vocabulary (arc42 §4, §5.4).
"""

from __future__ import annotations

from enum import StrEnum


class FilerProfile(StrEnum):
    DOMESTIC = "DOMESTIC"
    FPI = "FPI"


class Materiality(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    NONE = "NONE"


class Sentiment(StrEnum):
    POSITIVE = "POSITIVE"
    NEUTRAL = "NEUTRAL"
    NEGATIVE = "NEGATIVE"
    MIXED = "MIXED"


class GuidanceDirection(StrEnum):
    RAISED = "RAISED"
    MAINTAINED = "MAINTAINED"
    LOWERED = "LOWERED"
    WITHDRAWN = "WITHDRAWN"
    NONE = "NONE"


class Novelty(StrEnum):
    NEW_INFORMATION = "NEW_INFORMATION"
    RESTATEMENT = "RESTATEMENT"
    ROUTINE = "ROUTINE"


class ThemeStrength(StrEnum):
    STRONG = "STRONG"
    MODERATE = "MODERATE"
    WEAK = "WEAK"


class RiskCategory(StrEnum):
    MARGIN = "MARGIN"
    SUPPLY = "SUPPLY"
    REGULATORY = "REGULATORY"
    CUSTOMER_CONCENTRATION = "CUSTOMER_CONCENTRATION"
    COMPETITION = "COMPETITION"
    LITIGATION = "LITIGATION"
    LIQUIDITY = "LIQUIDITY"
    OTHER = "OTHER"


class RiskSeverity(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class NarrativeClaimType(StrEnum):
    TAM_EXPANSION = "TAM_EXPANSION"
    NEW_PRODUCT = "NEW_PRODUCT"
    PARTNERSHIP = "PARTNERSHIP"
    DESIGN_WIN = "DESIGN_WIN"
    CAPACITY_EXPANSION = "CAPACITY_EXPANSION"
    MANAGEMENT_CHANGE = "MANAGEMENT_CHANGE"


class ExtractionConfidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class RiskDirection(StrEnum):
    STRENGTHENED = "STRENGTHENED"
    SOFTENED = "SOFTENED"


class GuidanceLanguageShift(StrEnum):
    FIRMED = "FIRMED"
    UNCHANGED = "UNCHANGED"
    HEDGED = "HEDGED"
    WITHDRAWN = "WITHDRAWN"


class MdaToneShift(StrEnum):
    MORE_CONFIDENT = "MORE_CONFIDENT"
    UNCHANGED = "UNCHANGED"
    MORE_CAUTIOUS = "MORE_CAUTIOUS"


class CohortConfidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Direction(StrEnum):
    STRENGTHENING = "STRENGTHENING"
    WEAKENING = "WEAKENING"
    STABLE = "STABLE"


class Action(StrEnum):
    BUY = "BUY"
    ADD = "ADD"
    HOLD_NO_ACTION = "HOLD_NO_ACTION"
    HOLD_INSUFFICIENT_DATA = "HOLD_INSUFFICIENT_DATA"
    TRIM = "TRIM"
    SELL = "SELL"


class LegName(StrEnum):
    THESIS_LINKAGE = "thesis_linkage"
    ATTENTION_ACCELERATION = "attention_acceleration"
    NARRATIVE_PREMIUM = "narrative_premium"
    SMART_MONEY = "smart_money"
    FUNDAMENTAL_HEALTH = "fundamental_health"
    VALUATION_BRAKE = "valuation_brake"


class DocumentType(StrEnum):
    FORM_10K = "10-K"
    FORM_10Q = "10-Q"
    FORM_8K = "8-K"
    FORM_20F = "20-F"
    FORM_6K = "6-K"
    FORM_S1 = "S-1"
    FORM_4 = "4"
    NEWS = "NEWS"


class Form4TransactionCode(StrEnum):
    """Section 16 transaction codes. Only P and S feed smart_money (arc42 §5.5)."""

    P = "P"  # open-market purchase
    S = "S"  # open-market sale
    M = "M"  # option exercise — excluded
    A = "A"  # grant — excluded
    F = "F"  # tax withholding — excluded
    G = "G"  # gift — excluded
    C = "C"  # excluded
    D = "D"  # excluded


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"


class DispositionStatus(StrEnum):
    """Owner's response to a suggested action (arc42 §5.7 "Dispositions", §8.3).

    Auspex's own data, written to `recommendations` — never to the external
    portfolio ledger. The "Suggested by Auspex" flag on a position is
    resolved at read time by joining the projection against
    `recommendations`, not by writing a flag into the ledger.
    """

    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    DEFERRED = "DEFERRED"
