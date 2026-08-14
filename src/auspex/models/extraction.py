"""Channel A (scoring) and Channel B (evidence) extraction schemas.

arc42 §5.4. Channel A output is maximally constrained enum labels + short
verbatim excerpts; Channel B is a 150-250 word prose digest plus a
comparative diff. Both share the cache key
``content_hash + model_version + prompt_version + schema_version [+ taxonomy_version]``.
"""

from __future__ import annotations

from pydantic import Field, field_validator

from auspex.models.common import AuspexModel
from auspex.models.enums import (
    ExtractionConfidence,
    GuidanceDirection,
    GuidanceLanguageShift,
    Materiality,
    MdaToneShift,
    NarrativeClaimType,
    Novelty,
    RiskCategory,
    RiskDirection,
    RiskSeverity,
    Sentiment,
    ThemeStrength,
)

MAX_EXCERPT_CHARS = 300
MAX_QUOTE_CHARS = 400


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


class ThemeClaim(AuspexModel):
    theme_id: str
    strength: ThemeStrength
    evidence_excerpt: str = Field(max_length=MAX_EXCERPT_CHARS)
    location_hint: str | None = None

    @field_validator("evidence_excerpt", mode="before")
    @classmethod
    def _clip(cls, v: str) -> str:
        return _truncate(v, MAX_EXCERPT_CHARS)


class RiskClaim(AuspexModel):
    category: RiskCategory
    severity: RiskSeverity
    evidence_excerpt: str = Field(max_length=MAX_EXCERPT_CHARS)

    @field_validator("evidence_excerpt", mode="before")
    @classmethod
    def _clip(cls, v: str) -> str:
        return _truncate(v, MAX_EXCERPT_CHARS)


class NarrativeClaim(AuspexModel):
    claim_type: NarrativeClaimType
    strength: ThemeStrength
    evidence_excerpt: str = Field(max_length=MAX_EXCERPT_CHARS)

    @field_validator("evidence_excerpt", mode="before")
    @classmethod
    def _clip(cls, v: str) -> str:
        return _truncate(v, MAX_EXCERPT_CHARS)


class ChannelAExtraction(AuspexModel):
    """`extractions` container row."""

    id: str = Field(description="extraction_id")
    security_id: str
    document_id: str
    content_hash: str
    model_version: str
    prompt_version: str = "extract-a-v1"
    schema_version: str = "4.0"
    taxonomy_version: str

    materiality: Materiality
    sentiment: Sentiment
    guidance_direction: GuidanceDirection
    novelty: Novelty

    theme_claims: list[ThemeClaim] = Field(default_factory=list)
    risk_claims: list[RiskClaim] = Field(default_factory=list)
    narrative_claims: list[NarrativeClaim] = Field(default_factory=list)

    extraction_confidence: ExtractionConfidence

    @property
    def partition_key(self) -> str:
        return self.security_id

    @property
    def cache_key(self) -> str:
        return "|".join(
            [
                self.content_hash,
                self.model_version,
                self.prompt_version,
                self.schema_version,
                self.taxonomy_version,
            ]
        )


class KeyQuote(AuspexModel):
    text: str = Field(max_length=MAX_QUOTE_CHARS)
    section: str
    why_it_matters: str

    @field_validator("text", mode="before")
    @classmethod
    def _clip(cls, v: str) -> str:
        return _truncate(v, MAX_QUOTE_CHARS)


class RiskFactorAdded(AuspexModel):
    summary: str
    verbatim: str = Field(max_length=MAX_QUOTE_CHARS)
    category: RiskCategory
    severity: RiskSeverity

    @field_validator("verbatim", mode="before")
    @classmethod
    def _clip(cls, v: str) -> str:
        return _truncate(v, MAX_QUOTE_CHARS)


class RiskFactorRemoved(AuspexModel):
    summary: str
    prior_verbatim: str = Field(max_length=MAX_QUOTE_CHARS)

    @field_validator("prior_verbatim", mode="before")
    @classmethod
    def _clip(cls, v: str) -> str:
        return _truncate(v, MAX_QUOTE_CHARS)


class RiskFactorReworded(AuspexModel):
    summary: str
    before: str
    after: str
    direction: RiskDirection


class ComparativeDiff(AuspexModel):
    prior_document_id: str | None = None
    risk_factors_added: list[RiskFactorAdded] = Field(default_factory=list)
    risk_factors_removed: list[RiskFactorRemoved] = Field(default_factory=list)
    risk_factors_reworded: list[RiskFactorReworded] = Field(default_factory=list)
    guidance_language_shift: GuidanceLanguageShift = GuidanceLanguageShift.UNCHANGED
    mda_tone_shift: MdaToneShift = MdaToneShift.UNCHANGED

    @property
    def has_high_severity_addition(self) -> bool:
        return any(r.severity == RiskSeverity.HIGH for r in self.risk_factors_added)


class ChannelBDigest(AuspexModel):
    """`digests` container row."""

    id: str = Field(description="digest_id")
    security_id: str
    document_id: str
    content_hash: str
    model_version: str
    prompt_version: str = "digest-b-v1"

    headline: str
    digest: str
    key_quotes: list[KeyQuote] = Field(default_factory=list)
    management_claims: list[str] = Field(default_factory=list)
    unanswered_questions: list[str] = Field(default_factory=list)
    comparative: ComparativeDiff | None = None

    @property
    def partition_key(self) -> str:
        return self.security_id

    @property
    def cache_key(self) -> str:
        return "|".join([self.content_hash, self.model_version, self.prompt_version])
