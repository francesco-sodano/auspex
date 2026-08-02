from functools import lru_cache
from datetime import date, datetime, timezone
import json
import os

import azure.functions as func
from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential

from agent.discussion import AzureOpenAIDiscussionNarrator, GroundedDiscussionService
from agent.narrator import AzureOpenAIGroundedNarrator
from agent.service import GroundedRecommendationAgent
from auspex_api.app_users import CosmosAppUserRepository
from auspex_api.decision_log import CosmosDecisionLogRepository
from auspex_api.discussion import CosmosDiscussionRepository, NotificationPreferenceService
from auspex_api.http import execute, registration_payload
from auspex_api.metric_metadata import metric_metadata_payload
from auspex_api.market_data import (
    CosmosMarketDataRepository,
    CosmosSecurityCatalog,
    CosmosUniverseRepository,
)
from auspex_api.portfolio import CosmosPortfolioTransactionRepository, PortfolioService
from auspex_api.recommendations import (
    CosmosOpportunitySignalRepository,
    RecommendationService,
)
from auspex_api.recommendation_events import (
    CosmosRecommendationEventRepository,
    RecommendationExperienceService,
)
from auspex_api.services import IdentityService
from search.clients import AzureOpenAIChat, AzureSearchRestClient
from search.retrieval import EvidenceSearchService


app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


def _json_response(payload: object, status_code: int) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload),
        status_code=status_code,
        mimetype="application/json",
        headers={"Cache-Control": "no-store"},
    )


@lru_cache(maxsize=1)
def _identity_service() -> IdentityService:
    endpoint = os.environ.get("COSMOS_ENDPOINT", "").strip()
    if not endpoint:
        raise RuntimeError("COSMOS_ENDPOINT is required")
    database_name = os.environ.get("COSMOS_DATABASE_NAME", "auspex")
    container_name = os.environ.get("APP_USERS_CONTAINER", "app_users")
    cosmos = CosmosClient(endpoint, credential=DefaultAzureCredential())
    container = cosmos.get_database_client(database_name).get_container_client(container_name)
    return IdentityService(CosmosAppUserRepository(container))


@lru_cache(maxsize=1)
def _portfolio_service() -> PortfolioService:
    endpoint = os.environ.get("COSMOS_ENDPOINT", "").strip()
    if not endpoint:
        raise RuntimeError("COSMOS_ENDPOINT is required")
    database_name = os.environ.get("COSMOS_DATABASE_NAME", "auspex")
    transaction_container_name = os.environ.get(
        "PORTFOLIO_TRANSACTIONS_CONTAINER",
        "portfolio_transactions",
    )
    cosmos = CosmosClient(endpoint, credential=DefaultAzureCredential())
    database = cosmos.get_database_client(database_name)
    return PortfolioService(
        _identity_service(),
        CosmosPortfolioTransactionRepository(
            database.get_container_client(transaction_container_name)
        ),
        security_catalog=CosmosSecurityCatalog(database.get_container_client(
            os.environ.get("SECURITY_CATALOG_CONTAINER", "security_catalog")
        )),
        universe=CosmosUniverseRepository(database.get_container_client(
            os.environ.get("INGESTION_UNIVERSE_CONTAINER", "ingestion_universe")
        )),
        market_data=CosmosMarketDataRepository(database.get_container_client(
            os.environ.get("MARKET_DATA_CONTAINER", "market_data")
        )),
    )


@lru_cache(maxsize=1)
def _recommendation_service() -> RecommendationService:
    endpoint = os.environ.get("COSMOS_ENDPOINT", "").strip()
    if not endpoint:
        raise RuntimeError("COSMOS_ENDPOINT is required")
    database_name = os.environ.get("COSMOS_DATABASE_NAME", "auspex")
    cosmos = CosmosClient(endpoint, credential=DefaultAzureCredential())
    database = cosmos.get_database_client(database_name)
    return RecommendationService(
        _identity_service(),
        _portfolio_service(),
        CosmosOpportunitySignalRepository(database.get_container_client(
            os.environ.get("MARKET_DATA_CONTAINER", "market_data")
        )),
    )


@lru_cache(maxsize=1)
def _evidence_service() -> EvidenceSearchService:
    search_endpoint = os.environ.get("AI_SEARCH_ENDPOINT", "").strip()
    index_name = os.environ.get("AI_SEARCH_EVIDENCE_INDEX", "idx-news-filings").strip()
    if not search_endpoint:
        raise RuntimeError("AI_SEARCH_ENDPOINT is required")
    return EvidenceSearchService(AzureSearchRestClient(search_endpoint, index_name))


@lru_cache(maxsize=1)
def _grounded_recommendation_agent() -> GroundedRecommendationAgent:
    endpoint = os.environ.get("COSMOS_ENDPOINT", "").strip()
    openai_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
    deployment = os.environ.get("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4o").strip()
    model_version = os.environ.get(
        "AZURE_OPENAI_CHAT_MODEL_VERSION", "gpt-4o:2024-11-20"
    ).strip()
    if not endpoint:
        raise RuntimeError("COSMOS_ENDPOINT is required")
    if not openai_endpoint:
        raise RuntimeError("AZURE_OPENAI_ENDPOINT is required")
    cosmos = CosmosClient(endpoint, credential=DefaultAzureCredential())
    database = cosmos.get_database_client(os.environ.get("COSMOS_DATABASE_NAME", "auspex"))
    return GroundedRecommendationAgent(
        _identity_service(),
        _recommendation_service(),
        _evidence_service(),
        AzureOpenAIGroundedNarrator(
            AzureOpenAIChat(openai_endpoint, deployment),
            model_version=model_version,
        ),
        CosmosDecisionLogRepository(database.get_container_client(
            os.environ.get("DECISION_LOG_CONTAINER", "decision_log")
        )),
    )


@lru_cache(maxsize=1)
def _recommendation_experience_service() -> RecommendationExperienceService:
    endpoint = os.environ.get("COSMOS_ENDPOINT", "").strip()
    if not endpoint:
        raise RuntimeError("COSMOS_ENDPOINT is required")
    cosmos = CosmosClient(endpoint, credential=DefaultAzureCredential())
    database = cosmos.get_database_client(os.environ.get("COSMOS_DATABASE_NAME", "auspex"))
    container = database.get_container_client(
        os.environ.get("DECISION_LOG_CONTAINER", "decision_log")
    )
    return RecommendationExperienceService(
        _identity_service(),
        _recommendation_service(),
        CosmosDecisionLogRepository(container),
        CosmosRecommendationEventRepository(container),
    )


@lru_cache(maxsize=1)
def _discussion_repository() -> CosmosDiscussionRepository:
    endpoint = os.environ.get("COSMOS_ENDPOINT", "").strip()
    if not endpoint:
        raise RuntimeError("COSMOS_ENDPOINT is required")
    cosmos = CosmosClient(endpoint, credential=DefaultAzureCredential())
    database = cosmos.get_database_client(os.environ.get("COSMOS_DATABASE_NAME", "auspex"))
    return CosmosDiscussionRepository(database.get_container_client(
        os.environ.get("DECISION_LOG_CONTAINER", "decision_log")
    ))


@lru_cache(maxsize=1)
def _discussion_service() -> GroundedDiscussionService:
    openai_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
    deployment = os.environ.get("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4o").strip()
    model_version = os.environ.get(
        "AZURE_OPENAI_CHAT_MODEL_VERSION", "gpt-4o:2024-11-20"
    ).strip()
    if not openai_endpoint:
        raise RuntimeError("AZURE_OPENAI_ENDPOINT is required")
    return GroundedDiscussionService(
        _identity_service(),
        _portfolio_service(),
        _recommendation_service(),
        _evidence_service(),
        AzureOpenAIDiscussionNarrator(
            AzureOpenAIChat(openai_endpoint, deployment),
            model_version=model_version,
        ),
        _discussion_repository(),
    )


@lru_cache(maxsize=1)
def _notification_service() -> NotificationPreferenceService:
    return NotificationPreferenceService(
        _identity_service(),
        _portfolio_service(),
        _recommendation_service(),
        _discussion_repository(),
    )


def _principal(req: func.HttpRequest) -> str | None:
    return req.headers.get("x-ms-client-principal")


def _request_json(req: func.HttpRequest) -> dict:
    try:
        body = req.get_json()
    except ValueError as exc:
        raise ValueError("request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise ValueError("request body must be an object")
    return body


@app.route(route="me", methods=["GET"])
def me(req: func.HttpRequest) -> func.HttpResponse:
    result = execute(lambda: _identity_service().me(_principal(req)).public_profile())
    return _json_response(result.payload, result.status_code)


@app.route(route="registration", methods=["GET", "POST"])
def registration(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "POST":
        def submit():
            user, created = _identity_service().register(_principal(req), _request_json(req))
            return {**registration_payload(user), "created": created}
        result = execute(submit, success_status=201)
    else:
        result = execute(lambda: registration_payload(
            _identity_service().me(_principal(req))
        ))
    return _json_response(result.payload, result.status_code)


@app.route(route="onboarding", methods=["POST"])
def onboarding(req: func.HttpRequest) -> func.HttpResponse:
    result = execute(lambda: _identity_service().onboard(
        _principal(req),
        _request_json(req),
    ).public_profile())
    return _json_response(result.payload, result.status_code)


@app.route(route="transactions", methods=["GET", "POST"])
def transactions(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "POST":
        def create():
            transaction, created = _portfolio_service().create_transaction(
                _principal(req),
                _request_json(req),
            )
            return {"transaction": transaction.public_payload(), "created": created}
        result = execute(create, success_status=201)
    else:
        result = execute(lambda: [
            transaction.public_payload()
            for transaction in _portfolio_service().list_transactions(_principal(req))
        ])
    return _json_response(result.payload, result.status_code)


@app.route(route="transactions/{transaction_id}/correct", methods=["POST"])
def correct_transaction(req: func.HttpRequest) -> func.HttpResponse:
    def correct():
        transaction, created = _portfolio_service().correct_transaction(
            _principal(req),
            req.route_params["transaction_id"],
            _request_json(req),
        )
        return {"transaction": transaction.public_payload(), "created": created}

    result = execute(correct, success_status=201)
    return _json_response(result.payload, result.status_code)


@app.route(route="transaction_summary", methods=["GET"])
def transaction_summary(req: func.HttpRequest) -> func.HttpResponse:
    result = execute(lambda: _portfolio_service().quick_summary(_principal(req)))
    return _json_response(result.payload, result.status_code)


@app.route(route="portfolio_summary", methods=["GET"])
def portfolio_summary(req: func.HttpRequest) -> func.HttpResponse:
    result = execute(lambda: _portfolio_service().portfolio_summary(_principal(req)))
    return _json_response(result.payload, result.status_code)


@app.route(route="recommendations", methods=["GET"])
def recommendations(req: func.HttpRequest) -> func.HttpResponse:
    result = execute(lambda: _recommendation_service().recommendations(_principal(req)))
    return _json_response(result.payload, result.status_code)


@app.route(route="recommendations/{recommendation_id}/explain", methods=["POST"])
def explain_recommendation(req: func.HttpRequest) -> func.HttpResponse:
    result = execute(lambda: _grounded_recommendation_agent().explain(
        _principal(req),
        req.route_params["recommendation_id"],
    ))
    return _json_response(result.payload, result.status_code)


@app.route(route="recommendations/{recommendation_id}/disposition", methods=["POST"])
def recommendation_disposition(req: func.HttpRequest) -> func.HttpResponse:
    def record():
        event, created = _recommendation_experience_service().record_disposition(
            _principal(req),
            req.route_params["recommendation_id"],
            _request_json(req),
        )
        return {"event": event, "created": created}

    result = execute(record, success_status=201)
    return _json_response(result.payload, result.status_code)


@app.route(route="recommendation_history", methods=["GET"])
def recommendation_history(req: func.HttpRequest) -> func.HttpResponse:
    result = execute(lambda: _recommendation_experience_service().history(_principal(req)))
    return _json_response(result.payload, result.status_code)


@app.route(route="metric_metadata", methods=["GET"])
def metric_metadata(req: func.HttpRequest) -> func.HttpResponse:
    def metadata():
        _identity_service().product_user(_principal(req))
        return {"metrics": metric_metadata_payload()}

    result = execute(metadata)
    return _json_response(result.payload, result.status_code)


@app.route(route="discussion/turns", methods=["GET", "POST"])
def discussion_turns(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "POST":
        def discuss():
            exchange, created = _discussion_service().discuss(
                _principal(req), _request_json(req)
            )
            return {"exchange": exchange, "created": created}

        result = execute(discuss, success_status=201)
    else:
        result = execute(lambda: {
            "exchanges": _discussion_service().history(
                _principal(req),
                (req.params.get("conversation_id") or "").strip(),
            )
        })
    return _json_response(result.payload, result.status_code)


@app.route(route="advisor_profile", methods=["GET", "POST"])
def advisor_profile(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "POST":
        result = execute(lambda: _discussion_service().update_advisor_profile(
            _principal(req), _request_json(req)
        ))
    else:
        result = execute(lambda: _discussion_service().advisor_profile(_principal(req)))
    return _json_response(result.payload, result.status_code)


@app.route(route="advisor_profile/reset", methods=["POST"])
def reset_advisor_profile(req: func.HttpRequest) -> func.HttpResponse:
    result = execute(lambda: _discussion_service().reset_advisor_profile(_principal(req)))
    return _json_response(result.payload, result.status_code)


@app.route(route="morning_summary", methods=["GET"])
def morning_summary(req: func.HttpRequest) -> func.HttpResponse:
    result = execute(lambda: _notification_service().morning_summary(_principal(req)))
    return _json_response(result.payload, result.status_code)


@app.route(route="notification_preferences", methods=["GET", "POST"])
def notification_preferences(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "POST":
        result = execute(lambda: _notification_service().update_preferences(
            _principal(req), _request_json(req)
        ))
    else:
        result = execute(lambda: _notification_service().preferences(_principal(req)))
    return _json_response(result.payload, result.status_code)


@app.route(route="stock/{code}/lookup", methods=["GET"])
def stock_lookup(req: func.HttpRequest) -> func.HttpResponse:
    def lookup():
        security = _portfolio_service().lookup_security(
            _principal(req),
            req.route_params["code"],
        )
        return {
            "security_sk": security.security_sk,
            "ticker": security.ticker,
            "isin": security.isin,
            "company_name": security.company_name,
            "currency": security.currency,
            "exchange": security.exchange,
        }
    result = execute(lookup)
    return _json_response(result.payload, result.status_code)


@app.route(route="stock/search", methods=["GET"])
def stock_search(req: func.HttpRequest) -> func.HttpResponse:
    def search():
        return [
            {
                "security_sk": security.security_sk,
                "ticker": security.ticker,
                "isin": security.isin,
                "company_name": security.company_name,
                "currency": security.currency,
                "exchange": security.exchange,
            }
            for security in _portfolio_service().search_securities(
                _principal(req), req.params.get("q") or ""
            )
        ]
    result = execute(search)
    return _json_response(result.payload, result.status_code)


@app.route(route="evidence", methods=["GET"])
def evidence(req: func.HttpRequest) -> func.HttpResponse:
    def retrieve():
        _identity_service().product_user(_principal(req))
        as_of_text = (req.params.get("as_of") or "").strip()
        as_of = date.fromisoformat(as_of_text) if as_of_text else datetime.now(timezone.utc).date()
        security_text = (req.params.get("security_sk") or "").strip()
        security_sks = [int(security_text)] if security_text else []
        source_types = [
            value.strip()
            for value in (req.params.get("source_type") or "").split(",")
            if value.strip()
        ]
        limit = int(req.params.get("limit") or 10)
        citations = _evidence_service().retrieve(
            query=req.params.get("q") or "",
            as_of=as_of,
            security_sks=security_sks,
            source_types=source_types,
            limit=limit,
        )
        return {"as_of": as_of.isoformat(), "citations": citations}

    result = execute(retrieve)
    return _json_response(result.payload, result.status_code)


@app.route(route="registration_queue", methods=["GET"])
def admin_registrations(req: func.HttpRequest) -> func.HttpResponse:
    status = req.params.get("status") or "pending"
    result = execute(lambda: [
        registration_payload(user)
        for user in _identity_service().list_registrations(_principal(req), status)
    ])
    return _json_response(result.payload, result.status_code)


def _review(req: func.HttpRequest, action: str) -> func.HttpResponse:
    body = _request_json(req) if req.get_body() else {}
    result = execute(lambda: registration_payload(
        _identity_service().review_user(
            _principal(req), req.route_params["user_sk"], action, note=body.get("note")
        )
    ))
    return _json_response(result.payload, result.status_code)


@app.route(route="approve_registration/{user_sk}", methods=["POST"])
def approve_registration(req: func.HttpRequest) -> func.HttpResponse:
    return _review(req, "approve")


@app.route(route="reject_registration/{user_sk}", methods=["POST"])
def reject_registration(req: func.HttpRequest) -> func.HttpResponse:
    return _review(req, "reject")


@app.route(route="suspend_user/{user_sk}", methods=["POST"])
def suspend_user(req: func.HttpRequest) -> func.HttpResponse:
    return _review(req, "suspend")


@app.route(route="restore_user/{user_sk}", methods=["POST"])
def restore_user(req: func.HttpRequest) -> func.HttpResponse:
    return _review(req, "restore")
