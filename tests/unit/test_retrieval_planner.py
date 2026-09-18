from __future__ import annotations

import json
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock

import pytest

from auspex.assistant.planner import RetrievalPlanner
from auspex.models.conversation import ConversationState


def _response(**changes) -> str:
    return json.dumps(
        {
            "securities": [],
            "date_range": {"start": None, "end": None},
            "data_classes": ["score_snapshot", "leg_changes", "narrative_history"],
            "structured_filters": {"item": None},
            "needs_verbatim": False,
            **changes,
        }
    )


def _planner(*responses: str) -> tuple[RetrievalPlanner, AsyncMock]:
    client = AsyncMock()
    client.complete_json.side_effect = responses
    return RetrievalPlanner(
        openai_client=client,
        deployment="gpt-4.1-mini",
        system_prompt="Return a retrieval plan.",
    ), client


async def test_recovers_from_the_live_boolean_top_movers_filter(caplog):
    planner, client = _planner(
        _response(structured_filters={"top_movers": True}),
        _response(),
    )

    plan = await planner.plan(
        "What changed in the top movers today, and why?",
        ConversationState(),
        ["NVDA", "AMD"],
    )

    assert plan.structured_filters == {}
    assert plan.securities == []
    assert plan.data_classes == ["score_snapshot", "leg_changes", "narrative_history"]
    assert client.complete_json.await_count == 2
    assert "invalid retrieval plan" in caplog.text.lower()
    schema = client.complete_json.call_args.kwargs["json_schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["securities"]["items"]["enum"] == ["NVDA", "AMD"]
    assert schema["$defs"]["PlannerFilters"]["properties"] == {
        "item": {"anyOf": [{"type": "string"}, {"type": "null"}], "title": "Item"}
    }


async def test_invalid_model_output_fails_explicitly_after_one_retry():
    planner, client = _planner(
        _response(structured_filters={"top_movers": True}),
        _response(structured_filters={"top_movers": True}),
    )

    with pytest.raises(ValueError, match="valid retrieval plan"):
        await planner.plan("top movers", ConversationState(), ["NVDA"])

    assert client.complete_json.await_count == 2


async def test_unknown_security_is_not_allowed_to_broaden_retrieval():
    planner, client = _planner(
        _response(securities=["INVENTED"]),
        _response(securities=["INVENTED"]),
    )

    with pytest.raises(ValueError, match="valid retrieval plan"):
        await planner.plan("INVENTED", ConversationState(), ["NVDA"])

    assert client.complete_json.await_count == 2


def test_verbatim_item_and_dates_keep_the_persisted_plan_contract():
    planner, _ = _planner()
    plan = planner.parse_response(
        _response(
            securities=["NVDA"],
            date_range={"start": "2026-08-01", "end": "2026-08-31"},
            data_classes=["document_section"],
            structured_filters={"item": "item1a"},
            needs_verbatim=True,
        )
    )

    assert plan.structured_filters == {"item": "item1a"}
    assert plan.date_range_start == date(2026, 8, 1)
    assert plan.date_range_end == date(2026, 8, 31)
    assert plan.needs_verbatim is True


@pytest.mark.parametrize(
    "changes",
    [
        {"needs_verbatim": "false"},
        {"data_classes": ["invented"]},
        {"structured_filters": {"item": {"unsafe": True}}},
        {"date_range": {"start": "2026-09-18", "end": "2026-08-01"}},
    ],
)
def test_invalid_plans_are_not_silently_coerced(changes):
    planner, _ = _planner()
    with pytest.raises(ValueError):
        planner.parse_response(_response(**changes))


def test_planner_receives_current_utc_date_and_conversation_scope(monkeypatch):
    monkeypatch.setattr(
        "auspex.assistant.planner.utc_now",
        lambda: datetime(2026, 9, 18, 8, tzinfo=UTC),
    )
    planner, _ = _planner()
    content = json.loads(
        planner.build_user_content(
            "And its risks today?",
            ConversationState(resolved_securities=["NVDA"]),
            ["NVDA", "AMD"],
        )
    )
    assert content["current_date"] == "2026-09-18"
    assert content["conversation_state"]["resolved_securities"] == ["NVDA"]
