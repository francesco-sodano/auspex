"""Security master record (`securities` container, arc42 §5.11)."""

from __future__ import annotations

from pydantic import Field

from auspex.models.common import AuspexModel
from auspex.models.enums import FilerProfile


class Security(AuspexModel):
    id: str = Field(description="security_id, stable uuid derived from ticker at load time")
    ticker: str
    cik: str = Field(description="10-digit zero-padded EDGAR CIK")
    name: str
    exchange: str = "US"
    cohort: str
    filer_profile: FilerProfile
    investable: bool = True

    @property
    def partition_key(self) -> str:
        return self.id
