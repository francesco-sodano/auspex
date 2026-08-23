from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from auspex.pipeline.context import PipelineContext, PipelineRepos
from auspex.pipeline.manifest import new_manifest
from auspex.pipeline.steps import step_validate

AS_OF = date(2026, 8, 23)


class Rows:
    def __init__(self, *rows) -> None:
        self._rows = list(rows)

    def all(self) -> list:
        return list(self._rows)


def _context(*, recommendations: list) -> PipelineContext:
    empty = Rows()
    ctx = PipelineContext(
        universe=object(),
        config={"policy": {}, "fees": {}},
        as_of_date=AS_OF,
        user_id="owner",
        repos=PipelineRepos(
            document_sink=empty,
            price_sink=empty,
            fx_sink=Rows(SimpleNamespace(id="usdchf")),
            fundamental_sink=empty,
            blob_sink=empty,
            watermarks=empty,
            recommendation_repo=Rows(*recommendations),
        ),
    )
    ctx.__dict__["_snapshots"] = [
        SimpleNamespace(
            security_id="active",
            package_fingerprint="active-fingerprint",
            excluded_stale=False,
            composite="0.25",
        ),
        SimpleNamespace(
            security_id="stale",
            package_fingerprint="stale-fingerprint",
            excluded_stale=True,
            composite=None,
        ),
    ]
    ctx.__dict__["_score_results"] = {
        "active": SimpleNamespace(composite_result=object()),
        "stale": SimpleNamespace(composite_result=None),
    }
    return ctx


@pytest.mark.asyncio
async def test_validation_excludes_stale_non_policy_evaluable_snapshots() -> None:
    recommendation = SimpleNamespace(
        security_id="active",
        as_of_date=AS_OF,
        user_id="owner",
    )
    manifest = new_manifest(AS_OF)

    await step_validate(
        _context(recommendations=[recommendation]),
        manifest,
    )

    checkpoint = manifest.step_by_name("VALIDATE")
    assert checkpoint is not None
    assert checkpoint.status == "SUCCESS"
    assert checkpoint.degraded is False
    assert "recommendations reconciled" in (checkpoint.detail or "")


@pytest.mark.asyncio
async def test_validation_still_detects_a_missing_policy_recommendation() -> None:
    manifest = new_manifest(AS_OF)

    await step_validate(
        _context(recommendations=[]),
        manifest,
    )

    checkpoint = manifest.step_by_name("VALIDATE")
    assert checkpoint is not None
    assert checkpoint.status == "SUCCESS"
    assert checkpoint.degraded is True
    assert "missing=1, unexpected=0" in (checkpoint.detail or "")
