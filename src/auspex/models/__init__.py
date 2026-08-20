"""Typed Pydantic models for every Auspex domain object.

See ``doc/auspex-arc42.md`` for the container-to-model mapping.
"""

from __future__ import annotations

from auspex.models.app_user import (
    ADMIN_BINDING_ID,
    ALLOWED_TRANSITIONS,
    USER_INDEX_SCOPE,
    AdminAuthorityBinding,
    AppUser,
    AppUserSummary,
    UserRole,
    UserStatus,
)
from auspex.models.audit import AuditEventType, UserAuditEvent
from auspex.models.common import AuspexModel, as_decimal, content_hash, new_id, sha256_hex, utc_now
from auspex.models.config_version import ConfigVersion
from auspex.models.conversation import Citation, ConversationState, ConversationTurn, RetrievalPlan
from auspex.models.deletion import (
    DeletionJob,
    DeletionJobStatus,
    DeletionTarget,
    DeletionTargetStatus,
)
from auspex.models.document import Document, InsiderTransaction
from auspex.models.enums import (
    Action,
    CohortConfidence,
    Direction,
    DispositionStatus,
    DocumentType,
    ExtractionConfidence,
    FilerProfile,
    Form4TransactionCode,
    GuidanceDirection,
    GuidanceLanguageShift,
    LegName,
    Materiality,
    MdaToneShift,
    NarrativeClaimType,
    Novelty,
    RiskCategory,
    RiskDirection,
    RiskSeverity,
    RunStatus,
    Sentiment,
    ThemeStrength,
)
from auspex.models.extraction import (
    ChannelAExtraction,
    ChannelBDigest,
    ComparativeDiff,
    KeyQuote,
    NarrativeClaim,
    RiskClaim,
    RiskFactorAdded,
    RiskFactorRemoved,
    RiskFactorReworded,
    ThemeClaim,
)
from auspex.models.fundamentals import FundamentalSnapshot, XbrlFact
from auspex.models.market import FxRate, PriceBar
from auspex.models.onboarding import (
    InitialPortfolio,
    InitialPositionInput,
    OnboardingAcknowledgements,
    OnboardingPreferences,
    OnboardingState,
    OnboardingStep,
)
from auspex.models.performance import PerformanceMetric
from auspex.models.policy import (
    CostOutcomeOverlay,
    GateResult,
    Recommendation,
    RecommendationDisposition,
)
from auspex.models.portfolio import PortfolioProjection, PositionProjectionRow
from auspex.models.run import PIPELINE_STEPS, RunManifest, StepCheckpoint
from auspex.models.scoring import LegChange, LegResult, ScoreSnapshot
from auspex.models.security import Security
from auspex.models.user_settings import (
    HORIZON_UPPER_BOUND_MONTHS,
    LEGACY_INVESTMENT_HORIZONS,
    InvestmentHorizon,
    InvestmentObjective,
    RiskProfile,
    UserSettings,
)

__all__ = [
    "AuspexModel",
    "as_decimal",
    "content_hash",
    "new_id",
    "sha256_hex",
    "utc_now",
    "ADMIN_BINDING_ID",
    "ALLOWED_TRANSITIONS",
    "USER_INDEX_SCOPE",
    "AdminAuthorityBinding",
    "AppUser",
    "AppUserSummary",
    "UserRole",
    "UserStatus",
    "AuditEventType",
    "UserAuditEvent",
    "ConfigVersion",
    "Citation",
    "ConversationState",
    "ConversationTurn",
    "RetrievalPlan",
    "Document",
    "InsiderTransaction",
    "Action",
    "CohortConfidence",
    "Direction",
    "DispositionStatus",
    "DocumentType",
    "ExtractionConfidence",
    "FilerProfile",
    "Form4TransactionCode",
    "GuidanceDirection",
    "GuidanceLanguageShift",
    "LegName",
    "Materiality",
    "MdaToneShift",
    "NarrativeClaimType",
    "Novelty",
    "RiskCategory",
    "RiskDirection",
    "RiskSeverity",
    "RunStatus",
    "Sentiment",
    "ThemeStrength",
    "ChannelAExtraction",
    "ChannelBDigest",
    "ComparativeDiff",
    "KeyQuote",
    "NarrativeClaim",
    "RiskClaim",
    "RiskFactorAdded",
    "RiskFactorRemoved",
    "RiskFactorReworded",
    "ThemeClaim",
    "FundamentalSnapshot",
    "XbrlFact",
    "DeletionJob",
    "DeletionJobStatus",
    "DeletionTarget",
    "DeletionTargetStatus",
    "FxRate",
    "PriceBar",
    "InitialPortfolio",
    "InitialPositionInput",
    "OnboardingAcknowledgements",
    "OnboardingPreferences",
    "OnboardingState",
    "OnboardingStep",
    "PerformanceMetric",
    "CostOutcomeOverlay",
    "GateResult",
    "Recommendation",
    "RecommendationDisposition",
    "PortfolioProjection",
    "PositionProjectionRow",
    "PIPELINE_STEPS",
    "RunManifest",
    "StepCheckpoint",
    "LegChange",
    "LegResult",
    "ScoreSnapshot",
    "Security",
    "HORIZON_UPPER_BOUND_MONTHS",
    "LEGACY_INVESTMENT_HORIZONS",
    "InvestmentHorizon",
    "InvestmentObjective",
    "RiskProfile",
    "UserSettings",
]
