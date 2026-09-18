"""Conversational assistant endpoint — two-pass retrieval, SSE streaming (arc42 §5.10, §6.2).

Mounted at `/api/chat` (arc42 §11): `POST /chat` streams a grounded answer
over SSE, `GET /chat/history` lists the caller's own prior turns from the
`conversations` container (partitioned `/user_id`, arc42 §5.11).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
from uuid import uuid4

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from auspex.api.auth import AuthenticatedUser, get_current_user
from auspex.api.chat_grounding import get_chat_fetcher
from auspex.api.deps import get_app_user_service, get_universe
from auspex.api.rate_limit import SlidingWindowRateLimiter, get_rate_limiter
from auspex.api.repos import get_conversation_repo
from auspex.assistant.answer import AnswerGenerator
from auspex.assistant.grounding import (
    check_citations_present,
    check_citations_resolve,
    check_truncation_disclosed,
)
from auspex.assistant.planner import RetrievalPlanner
from auspex.assistant.retrieval import RetrievalFetcher
from auspex.config.loader import Universe
from auspex.models.app_user import UserStatus
from auspex.models.common import utc_now
from auspex.models.conversation import Citation, ConversationState, ConversationTurn
from auspex.persistence.repositories import CosmosRepository
from auspex.pipeline.prompts import load_prompt
from auspex.providers.openai_provider import AzureOpenAIClient
from auspex.settings import get_settings
from auspex.users.service import AppUserService

router = APIRouter(prefix="/chat", tags=["chat"])
logger = logging.getLogger(__name__)
CHAT_TIMEOUT_SECONDS = 180
CHAT_HEARTBEAT_SECONDS = 10
AMBIGUOUS_LOWERCASE_TICKERS = frozenset(
    {"ai", "app", "arm", "be", "form", "meta", "now", "on", "onto", "path", "snow"}
)


def _resolve_question_tickers(question: str, universe: Universe) -> list[str]:
    normalized = re.sub(r"[^a-z0-9]+", " ", question.lower())
    tokens = set(normalized.split())
    stock_context = bool(
        tokens.intersection(
            {"stock", "stocks", "share", "shares", "ticker", "company", "outlook"}
        )
    )
    matches: list[str] = []
    suffixes = {
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "ltd",
        "limited",
        "plc",
        "holdings",
        "de",
        "nv",
    }
    for security in universe.securities:
        ticker = security.ticker.lower()
        company_tokens = [
            token
            for token in re.sub(r"[^a-z0-9]+", " ", security.name.lower()).split()
            if token not in suffixes
        ]
        company_phrase = " ".join(company_tokens)
        explicit_ticker = bool(
            re.search(rf"\b{re.escape(security.ticker)}\b", question)
            or re.search(rf"(?:\$|\bticker\s+){re.escape(ticker)}\b", question, flags=re.IGNORECASE)
        ) or (stock_context and ticker in tokens and ticker not in AMBIGUOUS_LOWERCASE_TICKERS)
        company_match = bool(
            company_phrase
            and company_phrase in normalized
            and (len(company_tokens) > 1 or stock_context)
        )
        if explicit_ticker or company_match:
            matches.append(security.ticker)
    return list(dict.fromkeys(matches))


class ConversationRequest(BaseModel):
    question: str
    conversation_id: str | None = None
    state: ConversationState = Field(default_factory=ConversationState)


def _sse_event(data: str) -> str:
    return f"data: {json.dumps({'chunk': data})}\n\n"


@dataclass
class ChatProgress:
    phase: str = "history"


@dataclass(frozen=True)
class PreparedAnswer:
    conversation_id: str
    chunks: list[str]


class AccountAccessChangedError(PermissionError):
    pass


async def _prepare_answer(
    request: ConversationRequest,
    user: AuthenticatedUser,
    planner: RetrievalPlanner,
    fetcher: RetrievalFetcher,
    answerer: AnswerGenerator,
    universe: Universe,
    conversation_repo: CosmosRepository[ConversationTurn],
    users: AppUserService,
    progress: ChatProgress,
) -> PreparedAnswer:
    conversation_id = request.conversation_id or str(uuid4())
    prior_turns = await conversation_repo.query(
        query=(
            "SELECT TOP 1 * FROM c WHERE c.user_id=@user_id "
            "AND c.conversation_id=@conversation_id ORDER BY c.turn_index DESC"
        ),
        parameters=[
            {"name": "@user_id", "value": user.user_id},
            {"name": "@conversation_id", "value": conversation_id},
        ],
        partition_key=user.user_id,
    )
    prior_turn = prior_turns[0] if prior_turns else None
    state = prior_turn.state_after if prior_turn and prior_turn.state_after else request.state
    progress.phase = "planning"
    plan = await planner.plan(
        request.question,
        state,
        universe_tickers=[security.ticker for security in universe.securities],
    )
    question = request.question.lower()
    resolved_tickers = _resolve_question_tickers(request.question, universe)
    if resolved_tickers:
        plan = plan.model_copy(update={"securities": resolved_tickers})
    data_classes = list(plan.data_classes)
    stock_opinion = bool(resolved_tickers) and any(
        phrase in question
        for phrase in (
            "what do you think",
            "stock",
            "company",
            "happening",
            "latest",
            "outlook",
            "opinion",
        )
    )
    if stock_opinion:
        data_classes = [
            "score_snapshot",
            "leg_changes",
            "document_digest",
            "fundamentals",
            "portfolio_state",
            "recommendations",
        ]
    if any(term in question for term in ("move", "mover", "changed", "overnight", "score")):
        data_classes.extend(["score_snapshot", "leg_changes", "narrative_history"])
    if any(term in question for term in ("portfolio", "suggest", "buy", "sell", "trim", "add")):
        data_classes.extend(["portfolio_state", "recommendations"])
    if not data_classes:
        data_classes.extend(["score_snapshot", "portfolio_state", "recommendations"])
    plan = plan.model_copy(update={"data_classes": list(dict.fromkeys(data_classes))})
    progress.phase = "retrieval"
    retrieval = await fetcher.fetch(plan, user.user_id)
    progress.phase = "generation"
    chunks = [
        chunk
        async for chunk in answerer.stream_answer(
            request.question,
            retrieval,
            state.model_dump(mode="json"),
        )
    ]
    answer = "".join(chunks)
    if not answer.strip():
        raise ValueError("The model returned an empty answer.")
    violations = [
        *check_citations_present(answer, retrieval.items),
        *check_citations_resolve(answer, retrieval.items),
        *check_truncation_disclosed(answer, retrieval.truncated),
    ]
    if violations:
        logger.warning(
            "Chat grounding rejected an answer: %s",
            ", ".join(violation.kind for violation in violations),
        )
        answer = (
            "I could not produce an answer that passed Auspex grounding checks. "
            "Please ask a narrower question so I can answer only from retrieved facts."
        )
        chunks = [answer]

    resolved_securities = list(
        dict.fromkeys([*state.resolved_securities, *plan.securities])
    )
    state_after = ConversationState(
        resolved_securities=resolved_securities,
        active_date_range_start=plan.date_range_start or state.active_date_range_start,
        active_date_range_end=plan.date_range_end or state.active_date_range_end,
        securities_under_discussion=list(
            dict.fromkeys([*state.securities_under_discussion, *plan.securities])
        ),
    )
    citations = [
        Citation(
            document_id=item.document_id,
            source_url=item.source_url,
            retrieved_at=item.retrieved_at or utc_now(),
        )
        for item in retrieval.items
        if item.document_id is not None
    ]
    turn_index = prior_turn.turn_index + 1 if prior_turn is not None else 0
    progress.phase = "storage"
    latest_user = await users.get_user(user.user_id)
    if latest_user is None or latest_user.status is not UserStatus.ACTIVE:
        raise AccountAccessChangedError("Account access changed while the answer was prepared.")
    await conversation_repo.upsert(
        ConversationTurn(
            id=f"{conversation_id}:{turn_index}",
            user_id=user.user_id,
            conversation_id=conversation_id,
            turn_index=turn_index,
            question=request.question,
            plan=plan,
            truncated=retrieval.truncated,
            truncated_scope=retrieval.truncated_scope,
            answer=answer,
            citations=citations,
            state_after=state_after,
            created_at=utc_now(),
        )
    )
    return PreparedAnswer(conversation_id=conversation_id, chunks=chunks)


async def _stream_answer(
    request: ConversationRequest,
    user: AuthenticatedUser,
    planner: RetrievalPlanner,
    fetcher: RetrievalFetcher,
    answerer: AnswerGenerator,
    universe: Universe,
    conversation_repo: CosmosRepository[ConversationTurn],
    users: AppUserService,
) -> AsyncIterator[str]:
    request_id = str(uuid4())
    progress = ChatProgress()
    worker = asyncio.create_task(
        _prepare_answer(request, user, planner, fetcher, answerer, universe, conversation_repo, users, progress)
    )
    try:
        yield (
            "event: status\n"
            'data: {"message":"Reading current scores, evidence, and portfolio suggestions..."}\n\n'
        )
        async with asyncio.timeout(CHAT_TIMEOUT_SECONDS):
            while not worker.done():
                finished, _ = await asyncio.wait({worker}, timeout=CHAT_HEARTBEAT_SECONDS)
                if not finished:
                    yield ": keep-alive\n\n"
            prepared = await worker

        for chunk in prepared.chunks:
            yield _sse_event(chunk)
        yield f"event: conversation\ndata: {json.dumps({'conversation_id': prepared.conversation_id})}\n\n"
    except Exception as exc:  # noqa: BLE001 - the SSE response has started; report failures in-band
        logger.exception("Chat request %s failed during %s", request_id, progress.phase)
        code = "chat_failed"
        if isinstance(exc, AccountAccessChangedError):
            code = "account_access_changed"
            message = "Account access changed while the answer was prepared. Please sign in again."
        elif isinstance(exc, TimeoutError):
            code = "chat_timeout"
            message = "Auspex took too long to prepare the answer. Please try again or ask a narrower question."
        elif progress.phase == "storage":
            message = "Auspex could not save this conversation. Please try again."
        else:
            message = "Auspex could not complete the answer. Please try again in a moment."
        yield f"event: error\ndata: {json.dumps({'code': code, 'message': message, 'request_id': request_id})}\n\n"
    finally:
        if not worker.done():
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
    yield "event: done\ndata: {}\n\n"


@lru_cache
def get_chat_openai_client() -> AzureOpenAIClient:
    settings = get_settings()
    return AzureOpenAIClient(
        endpoint=settings.aoai_endpoint,
        api_version=settings.aoai_api_version,
        tokens_per_minute=settings.aoai_tokens_per_minute,
        tokens_per_minute_by_deployment={
            settings.aoai_deployment_answer: settings.aoai_narrative_tokens_per_minute,
        },
    )


@lru_cache
def get_planner() -> RetrievalPlanner:
    settings = get_settings()
    return RetrievalPlanner(
        openai_client=get_chat_openai_client(),
        deployment=settings.aoai_deployment_planner,
        system_prompt=load_prompt(RetrievalPlanner.prompt_version),
    )


def get_fetcher() -> RetrievalFetcher:
    return get_chat_fetcher()


@lru_cache
def get_answerer() -> AnswerGenerator:
    settings = get_settings()
    return AnswerGenerator(
        openai_client=get_chat_openai_client(),
        deployment=settings.aoai_deployment_answer,
        system_prompt=load_prompt(AnswerGenerator.prompt_version),
    )


@router.post("")
async def converse(
    request: ConversationRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    planner: RetrievalPlanner = Depends(get_planner),
    fetcher: RetrievalFetcher = Depends(get_fetcher),
    answerer: AnswerGenerator = Depends(get_answerer),
    universe: Universe = Depends(get_universe),
    conversation_repo: CosmosRepository = Depends(get_conversation_repo),
    users: AppUserService = Depends(get_app_user_service),
    limiter: SlidingWindowRateLimiter = Depends(get_rate_limiter),
) -> StreamingResponse:
    settings = get_settings()
    await limiter.check(
        scope="chat",
        key=user.user_id,
        limit=settings.chat_rate_limit,
        window_seconds=settings.rate_limit_window_seconds,
    )
    return StreamingResponse(
        _stream_answer(
            request,
            user,
            planner,
            fetcher,
            answerer,
            universe,
            conversation_repo,
            users,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.get("/history", response_model=list[ConversationTurn])
async def get_chat_history(
    conversation_id: str | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
    repo: CosmosRepository = Depends(get_conversation_repo),
) -> list[ConversationTurn]:
    """List the caller's own turns, optionally scoped to one conversation."""

    cutoff = (utc_now() - timedelta(days=15)).isoformat()
    if conversation_id is not None:
        query = (
            "SELECT * FROM c WHERE c.user_id = @user_id AND c.conversation_id = @conversation_id "
            "AND c.created_at >= @cutoff "
            "ORDER BY c.turn_index ASC"
        )
        parameters = [
            {"name": "@user_id", "value": user.user_id},
            {"name": "@conversation_id", "value": conversation_id},
            {"name": "@cutoff", "value": cutoff},
        ]
    else:
        query = (
            "SELECT * FROM c WHERE c.user_id = @user_id "
            "AND c.created_at >= @cutoff ORDER BY c.created_at DESC"
        )
        parameters = [
            {"name": "@user_id", "value": user.user_id},
            {"name": "@cutoff", "value": cutoff},
        ]

    return await repo.query(query=query, parameters=parameters, partition_key=user.user_id)
