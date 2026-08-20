"""Unit tests for `POST /api/chat` (SSE) and `GET /api/chat/history` (arc42 §11, §5.10)."""

from __future__ import annotations

from datetime import UTC, datetime

from auspex.api.auth import AuthenticatedUser, get_current_user
from auspex.api.deps import get_app_user_service, get_universe
from auspex.api.repos import get_conversation_repo
from auspex.api.routes import conversation
from auspex.assistant.retrieval import RetrievalResult, RetrievedItem
from auspex.config.loader import load_universe
from auspex.models.app_user import UserStatus
from auspex.models.conversation import ConversationTurn, RetrievalPlan
from tests.unit.conftest import (
    FakeCosmosRepository,
    build_app_user_service,
    make_app_user,
    make_router_app,
)


def _turn(user_id: str, conversation_id: str, turn_index: int, question: str) -> ConversationTurn:
    return ConversationTurn(
        id=f"{conversation_id}:{turn_index}",
        user_id=user_id,
        conversation_id=conversation_id,
        turn_index=turn_index,
        question=question,
        created_at=datetime.now(UTC),
    )


def _make_client(repo=None, authed: bool = True, extra_overrides=None):
    overrides = {
        get_conversation_repo: lambda: repo or FakeCosmosRepository(),
        get_universe: load_universe,
        get_app_user_service: lambda: build_app_user_service(
            [make_app_user("owner-1")]
        ),
        **(extra_overrides or {}),
    }
    if authed:
        overrides[get_current_user] = lambda: AuthenticatedUser(user_id="owner-1", claims={})
    return make_router_app(conversation.router, overrides)


class TestChatMountPoint:
    def test_conversation_router_is_mounted_at_chat_not_conversation(self):
        assert conversation.router.prefix == "/chat"

    def test_post_requires_auth(self):
        client = _make_client(authed=False)
        response = client.post("/api/chat", json={"question": "why did NVDA move?"})
        assert response.status_code == 401

    def test_company_name_resolves_to_fixed_universe_ticker(self):
        tickers = conversation._resolve_question_tickers(
            "What do you think about Intel stock?",
            load_universe(),
        )

        assert tickers == ["INTC"]

    def test_common_company_word_does_not_false_match_without_stock_context(self):
        tickers = conversation._resolve_question_tickers(
            "How should my portfolio serve my retirement goals?",
            load_universe(),
        )

        assert "SERV" not in tickers

    def test_post_streams_a_grounded_answer_with_the_fixed_universe(self):
        class Planner:
            tickers: list[str] = []
            states = []

            async def plan(self, question, state, universe_tickers):
                self.tickers = universe_tickers
                self.states.append(state)
                return RetrievalPlan(securities=["NVDA"], data_classes=["score_snapshot"])

        class Fetcher:
            data_classes: list[str] = []

            async def fetch(self, plan, user_id):
                self.data_classes = plan.data_classes
                return RetrievalResult(
                    items=[
                        RetrievedItem(
                            data_class="score_snapshot",
                            content={"ticker": "NVDA", "percentile": 91},
                            document_id="score:nvda",
                        )
                    ]
                )

        class Answerer:
            async def stream_answer(self, question, retrieval, conversation_state):
                yield "NVDA is ranked 91 [cite:score:nvda]."

        planner = Planner()
        fetcher = Fetcher()
        repo = FakeCosmosRepository()
        client = _make_client(
            repo=repo,
            extra_overrides={
                conversation.get_planner: lambda: planner,
                conversation.get_fetcher: lambda: fetcher,
                conversation.get_answerer: Answerer,
            }
        )
        response = client.post(
            "/api/chat",
            json={"question": "why did NVDA move?", "conversation_id": "conversation-1"},
        )

        assert response.status_code == 200
        assert "NVDA is ranked 91" in response.text
        assert "NVDA" in planner.tickers
        assert {"score_snapshot", "leg_changes", "narrative_history"}.issubset(fetcher.data_classes)
        assert repo.upserted[-1].conversation_id == "conversation-1"
        assert repo.upserted[-1].state_after.resolved_securities == ["NVDA"]

        follow_up = client.post(
            "/api/chat",
            json={"question": "What about its risks?", "conversation_id": "conversation-1"},
        )

        assert follow_up.status_code == 200
        assert planner.states[-1].resolved_securities == ["NVDA"]
        assert repo.upserted[-1].turn_index == 1

    def test_post_rejects_an_answer_with_an_unresolved_citation(self):
        class Planner:
            async def plan(self, question, state, universe_tickers):
                return RetrievalPlan(data_classes=["score_snapshot"])

        class Fetcher:
            async def fetch(self, plan, user_id):
                return RetrievalResult(
                    items=[
                        RetrievedItem(
                            data_class="score_snapshot",
                            content={"ticker": "NVDA", "percentile": 91},
                            document_id="score:nvda",
                        )
                    ]
                )

        class Answerer:
            async def stream_answer(self, question, retrieval, conversation_state):
                yield "NVDA is ranked 99 [cite:invented]."

        client = _make_client(
            extra_overrides={
                conversation.get_planner: Planner,
                conversation.get_fetcher: Fetcher,
                conversation.get_answerer: Answerer,
            }
        )

        response = client.post("/api/chat", json={"question": "score?"})

        assert response.status_code == 200
        assert "could not produce an answer that passed Auspex grounding checks" in response.text
        assert "ranked 99" not in response.text

    def test_answer_is_not_persisted_after_account_access_changes(self):
        class Planner:
            async def plan(self, question, state, universe_tickers):
                return RetrievalPlan(data_classes=["score_snapshot"])

        class Fetcher:
            async def fetch(self, plan, user_id):
                return RetrievalResult(items=[])

        class Answerer:
            async def stream_answer(self, question, retrieval, conversation_state):
                yield "This answer finished after deletion started."

        repo = FakeCosmosRepository()
        suspended = make_app_user("owner-1", status=UserStatus.SUSPENDED)
        client = _make_client(
            repo=repo,
            extra_overrides={
                conversation.get_planner: Planner,
                conversation.get_fetcher: Fetcher,
                conversation.get_answerer: Answerer,
                get_app_user_service: lambda: build_app_user_service([suspended]),
            },
        )

        response = client.post("/api/chat", json={"question": "what changed?"})

        assert response.status_code == 200
        assert "Account access changed" in response.text
        assert repo.upserted == []

    def test_stock_opinion_forces_complete_company_briefing(self):
        class Planner:
            async def plan(self, question, state, universe_tickers):
                return RetrievalPlan(data_classes=["performance"])

        class Fetcher:
            plan = None

            async def fetch(self, plan, user_id):
                self.plan = plan
                return RetrievalResult(
                    items=[
                        RetrievedItem(
                            data_class="score_snapshot",
                            content={"ticker": "INTC", "percentile": 80},
                            document_id="score:INTC:today",
                        )
                    ]
                )

        class Answerer:
            async def stream_answer(self, question, retrieval, conversation_state):
                yield "Intel has an Auspex Score of 80 [cite:score:INTC:today]."

        fetcher = Fetcher()
        client = _make_client(
            extra_overrides={
                conversation.get_planner: Planner,
                conversation.get_fetcher: lambda: fetcher,
                conversation.get_answerer: Answerer,
            }
        )

        response = client.post(
            "/api/chat",
            json={"question": "What do you think about Intel stock?"},
        )

        assert response.status_code == 200
        assert fetcher.plan.securities == ["INTC"]
        assert set(fetcher.plan.data_classes) == {
            "score_snapshot",
            "leg_changes",
            "document_digest",
            "fundamentals",
            "portfolio_state",
            "recommendations",
        }


class TestChatHistory:
    def test_requires_auth(self):
        client = _make_client(authed=False)
        response = client.get("/api/chat/history")
        assert response.status_code == 401

    def test_scopes_history_to_the_authenticated_user(self):
        repo = FakeCosmosRepository([_turn("owner-1", "conv-1", 0, "hello")])
        client = _make_client(repo)

        response = client.get("/api/chat/history")

        assert response.status_code == 200
        assert len(response.json()) == 1
        recorded = repo.queries[0]
        assert recorded.partition_key == "owner-1"
        assert {"name": "@user_id", "value": "owner-1"} in recorded.parameters

    def test_filters_by_conversation_id_and_orders_by_turn_index(self):
        turns = [
            _turn("owner-1", "conv-1", 1, "second question"),
            _turn("owner-1", "conv-1", 0, "first question"),
            _turn("owner-1", "conv-2", 0, "different conversation"),
        ]
        repo = FakeCosmosRepository(turns)
        client = _make_client(repo)

        response = client.get("/api/chat/history", params={"conversation_id": "conv-1"})

        assert response.status_code == 200
        body = response.json()
        assert [row["turn_index"] for row in body] == [0, 1]
        assert all(row["conversation_id"] == "conv-1" for row in body)
