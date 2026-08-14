"""Versioned config snapshot (`config_versions` container, arc42 §5.11).

Stores the complete weight set, thresholds, label mappings, cohort tree, and
taxonomy per version. Every ``scores`` row references ``config_version_id``;
without this container no historical score can be reproduced or re-run under
different parameters.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from auspex.models.common import AuspexModel


class ConfigVersion(AuspexModel):
    """`config_versions` container row, partitioned by `/config_type`."""

    id: str = Field(description="config_version_id, e.g. 2026-08-08-a")
    config_type: str = Field(default="scoring_bundle")
    created_at: datetime
    fingerprint: str = Field(description="sha256 of the serialised bundle below")
    weights: dict = Field(default_factory=dict)
    policy: dict = Field(default_factory=dict)
    label_mappings: dict = Field(default_factory=dict)
    cohorts: dict = Field(default_factory=dict)
    taxonomy: dict = Field(default_factory=dict)
    xbrl_concepts: dict = Field(default_factory=dict)
    fees: dict = Field(default_factory=dict)

    @property
    def partition_key(self) -> str:
        return self.config_type
