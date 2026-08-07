import json
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import azure.durable_functions as df
import azure.functions as func
from azure.identity import DefaultAzureCredential

from alpha_vantage.connector import AlphaVantageConnector
from benchmark_prices.connector import BenchmarkPricesConnector
from contracts.connector import ContractsConnector
from etf_holdings.connector import EtfHoldingsConnector
from news.connector import NewsConnector
from portfolio.connector import PortfolioConnector
from prices_eod.connector import PricesEodConnector
from prices_eod.blueprint import bp as prices_eod_bp
from sec_13dg.connector import Sec13DgConnector
from sec_13f.connector import Sec13FConnector
from sec_companyfacts.connector import SecCompanyFactsConnector
from sec_nport.connector import SecNportConnector
from sec_8k.connector import Sec8KConnector
from sec_form4.connector import SecForm4Connector
from sec_form4.blueprint import bp as sec_form4_bp
from sec_s1.connector import SecS1Connector
from theme_classifier.connector import ThemeClassifierConnector
from company_engine.orchestrator import company_engine_orchestrator
from company_engine.provider import FreshCompanyProvider
from company_engine.service import CompanyEngineService
from shared.clients import get_bronze_writer, get_control_plane
from shared.daily_build import (
	FabricDailyBuildClient,
	alpha_vantage_profiles,
	daily_build_instance_action,
	daily_build_orchestrator,
	daily_build_run_namespace,
	daily_publication_tail_orchestrator,
	promote_daily_warehouse_snapshot,
	scheduled_source_ids,
)
from shared.models import RunContext
from search.clients import AzureOpenAIChat, AzureOpenAIEmbeddings, AzureSearchRestClient, load_index_schema
from search.indexing import EvidenceIndexer
from search.narrative import (
	CosmosNarrativeFeatureCache,
	NarrativeFeatureService,
	build_narrative_projection,
	page_narrative_documents,
)
from search.sentiment import (
	CosmosSentimentCache,
	SentimentService,
	enrich_with_cached_sentiment,
	page_evidence_documents,
)
from engine.legacy_reset import (
	CONFIRMATION_TOKEN as RESET_CONFIRMATION_TOKEN,
	LegacyEngineReset,
)

app = df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)
app.register_blueprint(sec_form4_bp)
app.register_blueprint(prices_eod_bp)

_CONNECTORS = {
	"sec_form4": lambda cp, bw, body, source: SecForm4Connector(cp, bw, source_config=source, since_date=body.get("since_date") or None, to_date=body.get("to_date") or None),
	"sec_13f": lambda cp, bw, body, source: Sec13FConnector(cp, bw, since_date=body.get("since_date") or None, to_date=body.get("to_date") or None, filing_offset=body.get("filing_offset") or 0, filing_limit=body.get("filing_limit"), source_config=source),
	"sec_13dg": lambda cp, bw, body, source: Sec13DgConnector(cp, bw, since_date=body.get("since_date") or None, to_date=body.get("to_date") or None, filing_offset=body.get("filing_offset") or 0, filing_limit=body.get("filing_limit"), source_config=source),
	"sec_8k": lambda cp, bw, body, source: Sec8KConnector(cp, bw, since_date=body.get("since_date") or None, to_date=body.get("to_date") or None, filing_offset=body.get("filing_offset") or 0, filing_limit=body.get("filing_limit"), source_config=source),
	"sec_s1": lambda cp, bw, body, source: SecS1Connector(cp, bw, since_date=body.get("since_date") or None, to_date=body.get("to_date") or None, filing_offset=body.get("filing_offset") or 0, filing_limit=body.get("filing_limit"), source_config=source),
	"prices_eod": lambda cp, bw, body, source: PricesEodConnector(
		cp,
		bw,
		symbols=body.get("symbols") or None,
		since_date=body.get("since_date") or None,
		to_date=body.get("to_date") or None,
		symbol_offset=body.get("symbol_offset") or 0,
		symbol_limit=body.get("symbol_limit") or None,
		outputsize=body.get("outputsize") or None,
		source_config=source,
	),
	"benchmark_prices": lambda cp, bw, body, source: BenchmarkPricesConnector(
		cp,
		bw,
		symbols=body.get("symbols") or None,
		etf_symbols=body.get("etf_symbols") or None,
		since_date=body.get("since_date") or None,
		to_date=body.get("to_date") or None,
		symbol_offset=body.get("symbol_offset") or 0,
		symbol_limit=body.get("symbol_limit") or None,
		source_config=source,
	),
	"alpha_vantage": lambda cp, bw, body, source: AlphaVantageConnector(
		cp,
		bw,
		symbols=body.get("symbols") or None,
		etf_symbols=body.get("etf_symbols") or None,
		since_date=body.get("since_date") or None,
		to_date=body.get("to_date") or None,
		symbol_offset=body.get("symbol_offset") or 0,
		symbol_limit=body.get("symbol_limit") or None,
		include_etfs=body.get("include_etfs"),
		include_global=body.get("include_global"),
		profile=body.get("profile") or "combined",
		source_config=source,
	),
	"theme_classifier": lambda cp, bw, body, source: ThemeClassifierConnector(
		cp, bw, source_config=source
	),
	"news": lambda cp, bw, body, source: NewsConnector(
		cp,
		bw,
		symbols=body.get("symbols") or None,
		since_date=body.get("since_date") or None,
		to_date=body.get("to_date") or None,
		symbol_offset=body.get("symbol_offset") or 0,
		symbol_limit=body.get("symbol_limit") or None,
		source_config=source,
	),
	"contracts": lambda cp, bw, body, source: ContractsConnector(
		cp,
		bw,
		search_terms=body.get("search_terms") or None,
		since_date=body.get("since_date") or None,
		to_date=body.get("to_date") or None,
		source_config=source,
	),
	"etf_holdings": lambda cp, bw, body, source: EtfHoldingsConnector(
		cp,
		bw,
		etf_symbols=body.get("etf_symbols") or None,
		source_config=source,
	),
	"sec_companyfacts": lambda cp, bw, body, source: SecCompanyFactsConnector(
		cp,
		bw,
		symbols=body.get("symbols") or None,
		since_date=body.get("since_date") or None,
		to_date=body.get("to_date") or None,
		symbol_offset=body.get("symbol_offset") or 0,
		symbol_limit=body.get("symbol_limit") or None,
		source_config=source,
	),
	"sec_nport": lambda cp, bw, body, source: SecNportConnector(
		cp,
		bw,
		etf_series=body.get("etf_series") or None,
		since_date=body.get("since_date") or None,
		to_date=body.get("to_date") or None,
		filing_offset=body.get("filing_offset") or 0,
		filing_limit=body.get("filing_limit"),
		source_config=source,
	),
	"portfolio": lambda cp, bw, body, source: PortfolioConnector(
		cp,
		bw,
		source_config=source,
	),
}

_EXPECTED_SCHEMA_VERSIONS = {
	"benchmark_prices": 1,
	"sec_companyfacts": 1,
	"sec_nport": 1,
	"sec_13f": 2,
	"sec_13dg": 2,
	"sec_8k": 2,
	"sec_s1": 2,
	"contracts": 2,
	"portfolio": 5,
}

_SOURCE_SEEDS = {
	source["source_id"]: source
	for source in json.loads(
		(Path(__file__).parent / "shared" / "sources_seed.json").read_text(encoding="utf-8")
	)
}

def _json_response(payload: dict, status_code: int = 200) -> func.HttpResponse:
	return func.HttpResponse(json.dumps(payload), mimetype="application/json", status_code=status_code)


@lru_cache(maxsize=1)
def _evidence_indexer() -> EvidenceIndexer:
	search_endpoint = os.environ.get("AI_SEARCH_ENDPOINT", "").strip()
	openai_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
	index_name = os.environ.get("AI_SEARCH_EVIDENCE_INDEX", "idx-news-filings").strip()
	embedding_deployment = os.environ.get(
		"AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-large"
	).strip()
	if not search_endpoint or not openai_endpoint:
		raise RuntimeError("AI_SEARCH_ENDPOINT and AZURE_OPENAI_ENDPOINT are required")
	credential = DefaultAzureCredential()
	return EvidenceIndexer(
		AzureSearchRestClient(search_endpoint, index_name, credential=credential),
		AzureOpenAIEmbeddings(openai_endpoint, embedding_deployment, credential=credential),
		load_index_schema(openai_endpoint, embedding_deployment),
	)


@lru_cache(maxsize=1)
def _sentiment_service() -> SentimentService:
	openai_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
	deployment = os.environ.get("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4o").strip()
	model_version = os.environ.get(
		"AZURE_OPENAI_CHAT_MODEL_VERSION", "gpt-4o:2024-11-20"
	).strip()
	if not openai_endpoint:
		raise RuntimeError("AZURE_OPENAI_ENDPOINT is required")
	credential = DefaultAzureCredential()
	container = get_control_plane().container(
		os.environ.get("SENTIMENT_CACHE_CONTAINER", "sentiment_cache")
	)
	return SentimentService(
		AzureOpenAIChat(openai_endpoint, deployment, credential=credential),
		CosmosSentimentCache(container),
		model_version,
	)


def _narrative_prompt_path() -> Path:
	local_path = Path(__file__).parent.parent / "prompts" / "narrative" / "e21_v1.txt"
	deployed_path = Path(__file__).parent / "prompts" / "narrative" / "e21_v1.txt"
	return local_path if local_path.exists() else deployed_path


@lru_cache(maxsize=1)
def _narrative_service() -> NarrativeFeatureService:
	openai_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
	deployment = os.environ.get("AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-4o").strip()
	model_version = os.environ.get(
		"AZURE_OPENAI_CHAT_MODEL_VERSION", "gpt-4o:2024-11-20"
	).strip()
	if not openai_endpoint:
		raise RuntimeError("AZURE_OPENAI_ENDPOINT is required")
	prompt_path = _narrative_prompt_path()
	if not prompt_path.exists():
		raise RuntimeError("E21 narrative prompt is missing")
	credential = DefaultAzureCredential()
	container = get_control_plane().container(
		os.environ.get("NARRATIVE_FEATURE_CACHE_CONTAINER", "narrative_feature_cache")
	)
	return NarrativeFeatureService(
		AzureOpenAIChat(openai_endpoint, deployment, credential=credential),
		CosmosNarrativeFeatureCache(container),
		model_version=model_version,
		prompt_text=prompt_path.read_text(encoding="utf-8"),
	)


def _execute_connector(body: dict) -> tuple[dict, int]:
	source_id = body.get("source_id")
	if not source_id:
		return {"status": "failed", "error": "source_id is required"}, 400

	factory = _CONNECTORS.get(source_id)
	if factory is None:
		return {"source_id": source_id, "status": "failed", "error": "source is not implemented"}, 404

	run_id = body.get("run_id") or f"{source_id}-{uuid.uuid4().hex[:12]}"
	mode = body.get("mode") or "run"
	if mode not in {"run", "backfill"}:
		return {"run_id": run_id, "source_id": source_id, "status": "failed", "error": f"unsupported mode: {mode}"}, 400

	cp = get_control_plane()
	source = cp.get_source(source_id)
	if source is None and source_id in _SOURCE_SEEDS:
		source = dict(_SOURCE_SEEDS[source_id])
		cp.upsert_source(source)
	if source is None:
		return {"run_id": run_id, "source_id": source_id, "status": "failed", "error": "source is not registered"}, 404
	expected_schema_version = _EXPECTED_SCHEMA_VERSIONS.get(source_id)
	if source_id == "portfolio":
		source.update(_SOURCE_SEEDS["portfolio"])
		cp.upsert_source(source)
	canonical_seed = _SOURCE_SEEDS.get(source_id, {})
	canonical_fields = (
		"enabled", "schedule", "schema_version", "etf_symbols", "etf_series",
		"profiles", "rate_limit", "max_lookback_days", "search_terms", "required",
	)
	contract_changed = False
	for field in canonical_fields:
		canonical_value = canonical_seed.get(field)
		if canonical_value is not None and source.get(field) != canonical_value:
			source[field] = canonical_value
			contract_changed = True
	if expected_schema_version is not None and source.get("schema_version") != expected_schema_version:
		source["schema_version"] = expected_schema_version
		contract_changed = True
	if contract_changed:
		cp.upsert_source(source)
	if not source.get("enabled", False):
		return {
			"run_id": run_id,
			"source_id": source_id,
			"schema_version": source.get("schema_version"),
			"schedule": source.get("schedule"),
			"status": "skipped",
			"error": "source is disabled",
		}, 200

	connector = factory(cp, get_bronze_writer(), body, source)
	result = connector.run(RunContext(run_id=run_id, source_id=source_id, mode=mode))
	payload = {
		"run_id": run_id,
		"source_id": source_id,
		"schema_version": source.get("schema_version"),
		"status": result.status,
		"records_in": result.records_in,
		"bytes_written": result.bytes_written,
		"error": result.error,
		"has_more": result.has_more,
		"last_event_ts": result.last_event_ts,
		"last_cursor": result.last_cursor,
	}
	return payload, 500 if result.status == "failed" else 200


@app.route(route="run", methods=["POST"])
def run_connector(req: func.HttpRequest) -> func.HttpResponse:
	body = req.get_json() if req.get_body() else {}
	payload, status_code = _execute_connector(body)
	return _json_response(payload, status_code)


def _sync_serving_projections() -> dict:
	cp = get_control_plane()
	bw = get_bronze_writer()
	security_documents = bw.read_serving_projection("security_catalog")
	market_documents = (
		bw.read_serving_projection("market_data")
		+ bw.read_serving_projection("market_history")
	)
	if any(not str(document.get("id") or "").startswith(("ticker:", "isin:", "security:")) for document in security_documents):
		raise ValueError("invalid security projection id")
	if any(not str(document.get("id") or "").startswith(("quote:", "history:", "fx:", "score:security:", "classification:security:")) for document in market_documents):
		raise ValueError("invalid market projection id")
	security_generations = {document.get("generation") for document in security_documents}
	market_generations = {document.get("generation") for document in market_documents}
	if len(security_generations) != 1 or None in security_generations:
		raise ValueError("security projection generation is invalid")
	if len(market_generations) != 1 or None in market_generations:
		raise ValueError("market projection generation is invalid")
	with ThreadPoolExecutor(max_workers=16) as executor:
		list(executor.map(cp.upsert_security_catalog, security_documents))
		list(executor.map(cp.upsert_market_data, market_documents))
	deleted_security_documents = cp.delete_stale_projection_generation(
		"security_catalog", next(iter(security_generations))
	)
	deleted_market_documents = cp.delete_stale_projection_generation(
		"market_data", next(iter(market_generations))
	)
	return {
		"status": "ok",
		"security_documents": len(security_documents),
		"market_documents": len(market_documents),
		"deleted_security_documents": deleted_security_documents,
		"deleted_market_documents": deleted_market_documents,
	}


@app.route(route="sync_serving_projections", methods=["POST"])
def sync_serving_projections(req: func.HttpRequest) -> func.HttpResponse:
	try:
		return _json_response(_sync_serving_projections())
	except ValueError as exc:
		return _json_response({"status": "failed", "error": str(exc)}, 400)


def _sync_active_market_projections() -> dict:
	cp = get_control_plane()
	bw = get_bronze_writer()
	active_symbols = {
		str(symbol).strip().upper()
		for symbol in bw.read_universe("alpha_vantage", "active")
		if str(symbol).strip()
	}
	if not active_symbols:
		raise ValueError("active market universe is empty")
	market_documents = [
		document
		for document in (
			bw.read_serving_projection("market_data")
			+ bw.read_serving_projection("market_history")
		)
		if str(document.get("ticker") or "").strip().upper() in active_symbols
	]
	if any(not str(document.get("id") or "").startswith(("quote:", "history:", "score:security:", "classification:security:")) for document in market_documents):
		raise ValueError("invalid active market projection id")
	market_generations = {document.get("generation") for document in market_documents}
	if len(market_generations) != 1 or None in market_generations:
		raise ValueError("active market projection generation is invalid")
	quote_symbols = {
		str(document.get("ticker") or "").strip().upper()
		for document in market_documents
		if str(document.get("id") or "").startswith("quote:security:")
	}
	history_symbols = {
		str(document.get("ticker") or "").strip().upper()
		for document in market_documents
		if str(document.get("id") or "").startswith("history:security:")
	}
	missing_quotes = sorted(active_symbols - quote_symbols)
	missing_histories = sorted(active_symbols - history_symbols)
	if missing_quotes or missing_histories:
		raise ValueError(
			"active market projection is incomplete: "
			f"missing_quotes={len(missing_quotes)}, "
			f"missing_histories={len(missing_histories)}"
		)
	with ThreadPoolExecutor(max_workers=16) as executor:
		list(executor.map(cp.upsert_market_data, market_documents))
	return {
		"status": "ok",
		"active_symbols": len(active_symbols),
		"market_documents": len(market_documents),
		"generation": next(iter(market_generations)),
	}


@app.route(route="sync_active_market_projections", methods=["POST"])
def sync_active_market_projections(req: func.HttpRequest) -> func.HttpResponse:
	try:
		return _json_response(_sync_active_market_projections())
	except ValueError as exc:
		return _json_response({"status": "failed", "error": str(exc)}, 400)


@app.route(route="serving_projection_status", methods=["GET"])
def serving_projection_status(req: func.HttpRequest) -> func.HttpResponse:
	cp = get_control_plane()
	return _json_response({
		"security_documents": cp.count_documents("security_catalog"),
		"market_documents": cp.count_documents("market_data"),
		"universe_symbols": cp.count_documents("ingestion_universe"),
	})


def _sync_evidence_index() -> dict:
	documents = get_bronze_writer().read_serving_projection("evidence")
	sentiment_documents = enrich_with_cached_sentiment(documents, _sentiment_service())
	return {
		"status": "ok",
		"sentiment_documents": sentiment_documents,
		**_evidence_indexer().sync(
			documents,
			batch_size=int(os.environ.get("AI_SEARCH_BATCH_SIZE", "128")),
			embedding_workers=int(
				os.environ.get("AI_SEARCH_EMBEDDING_WORKERS", "2")
			),
		),
	}


@app.route(route="sync_evidence_index", methods=["POST"])
def sync_evidence_index(req: func.HttpRequest) -> func.HttpResponse:
	try:
		return _json_response(_sync_evidence_index())
	except ValueError as exc:
		return _json_response({"status": "failed", "error": str(exc)}, 400)
	except Exception as exc:
		return _json_response({"status": "failed", "error": str(exc)}, 500)


@app.route(route="score_evidence_sentiment", methods=["POST"])
def score_evidence_sentiment(req: func.HttpRequest) -> func.HttpResponse:
	try:
		body = req.get_json() if req.get_body() else {}
		limit = int(body.get("limit", 25))
		if limit < 1 or limit > 200:
			raise ValueError("limit must be between 1 and 200")
		after_id = str(body.get("after_id") or "").strip()
		documents, next_after_id, has_more = page_evidence_documents(
			get_bronze_writer().read_serving_projection("evidence"),
			limit=limit,
			after_id=after_id,
		)
		cache_hits = 0
		for document in documents:
			_, cached = _sentiment_service().score(document)
			cache_hits += int(cached)
		return _json_response({
			"status": "ok",
			"documents": len(documents),
			"cache_hits": cache_hits,
			"scored": len(documents) - cache_hits,
			"next_after_id": next_after_id,
			"has_more": has_more,
		})
	except ValueError as exc:
		return _json_response({"status": "failed", "error": str(exc)}, 400)
	except Exception as exc:
		return _json_response({"status": "failed", "error": str(exc)}, 500)


@app.route(route="score_narrative_features", methods=["POST"])
def score_narrative_features(req: func.HttpRequest) -> func.HttpResponse:
	try:
		body = req.get_json() if req.get_body() else {}
		return _json_response(_score_narrative_page(body))
	except ValueError as exc:
		return _json_response({"status": "failed", "error": str(exc)}, 400)
	except Exception as exc:
		return _json_response({"status": "failed", "error": str(exc)}, 500)


@app.route(route="publish_narrative_features", methods=["POST"])
def publish_narrative_features(req: func.HttpRequest) -> func.HttpResponse:
	try:
		return _json_response(_publish_narrative_features())
	except ValueError as exc:
		return _json_response({"status": "failed", "error": str(exc)}, 400)
	except Exception as exc:
		return _json_response({"status": "failed", "error": str(exc)}, 500)


def _score_narrative_page(body: dict) -> dict:
	limit = int(body.get("limit", 25))
	max_workers = int(body.get("max_workers", 4))
	if limit < 1 or limit > 100:
		raise ValueError("limit must be between 1 and 100")
	if max_workers < 1 or max_workers > 8:
		raise ValueError("max_workers must be between 1 and 8")
	after_id = str(body.get("after_id") or "").strip()
	bw = get_bronze_writer()
	eligible_symbols = {
		str(symbol).strip().upper()
		for symbol in bw.read_universe("alpha_vantage", "active")
		if str(symbol).strip()
	}
	if not eligible_symbols:
		raise ValueError("active narrative universe is empty")
	documents, next_after_id, has_more = page_narrative_documents(
		bw.read_serving_projection("evidence"),
		limit=limit,
		after_id=after_id,
		eligible_symbols=eligible_symbols,
	)
	service = _narrative_service()
	with ThreadPoolExecutor(max_workers=max_workers) as executor:
		results = list(executor.map(service.score, documents))
	cache_hits = sum(int(cached) for _, cached in results)
	return {
		"status": "ok",
		"documents": len(documents),
		"cache_hits": cache_hits,
		"scored": len(documents) - cache_hits,
		"next_after_id": next_after_id,
		"has_more": has_more,
	}


def _publish_narrative_features() -> dict:
	bw = get_bronze_writer()
	service = _narrative_service()
	eligible_symbols = {
		str(symbol).strip().upper()
		for symbol in bw.read_universe("alpha_vantage", "active")
		if str(symbol).strip()
	}
	if not eligible_symbols:
		raise ValueError("active narrative universe is empty")
	projection, manifest = build_narrative_projection(
		bw.read_serving_projection("evidence"),
		service.list_cached(),
		eligible_symbols=eligible_symbols,
	)
	bytes_written = bw.write_serving_projection("narrative_features", projection)
	return {"status": "ok", **manifest, "bytes_written": bytes_written}


@lru_cache(maxsize=1)
def _daily_build_client() -> FabricDailyBuildClient:
	return FabricDailyBuildClient()


@app.timer_trigger(
	schedule="%DAILY_BUILD_SCHEDULE%",
	arg_name="timer",
	run_on_startup=False,
	use_monitor=True,
)
@app.durable_client_input(client_name="client")
async def daily_build_schedule(timer: func.TimerRequest, client):
	triggered_at = datetime.now(timezone.utc)
	as_of_date = triggered_at.date().isoformat()
	instance_id = f"company-engine-{as_of_date}"
	logging.info(
		"DailyBuildScheduleTriggered instance_id=%s past_due=%s",
		instance_id,
		bool(getattr(timer, "past_due", False)),
	)
	status = await client.get_status(instance_id)
	action = daily_build_instance_action(status)
	if action != "start":
		runtime_status = getattr(status, "runtime_status", None)
		runtime_status = getattr(runtime_status, "value", runtime_status)
		logging.info(
			"DailyBuildScheduleExistingInstance instance_id=%s runtime_status=%s action=%s",
			instance_id,
			runtime_status,
			action,
		)
		if action == "skip":
			return
		await client.purge_instance_history(instance_id)
		logging.info("DailyBuildSchedulePurged instance_id=%s", instance_id)
	await client.start_new(
		"company_engine",
		instance_id,
		{
			"as_of_date": as_of_date,
		},
	)
	logging.info("DailyBuildScheduleStarted instance_id=%s", instance_id)


@app.orchestration_trigger(context_name="context")
def daily_build(context):
	return daily_build_orchestrator(context)


@app.orchestration_trigger(context_name="context")
def company_engine(context):
	return company_engine_orchestrator(context)


@app.orchestration_trigger(context_name="context")
def legacy_engine_reset(context):
	payload = context.get_input() or {}
	result = yield context.call_activity("execute_legacy_engine_reset", payload)
	return result


@lru_cache(maxsize=1)
def _company_engine_service() -> CompanyEngineService:
	return CompanyEngineService(
		get_control_plane(),
		FreshCompanyProvider(
			alpha_vantage_api_key=os.environ["ALPHAVANTAGE_API_KEY"],
			finnhub_api_key=os.environ["FINNHUB_API_KEY"],
			requests_per_minute=int(
				os.environ.get("ALPHAVANTAGE_REQUESTS_PER_MINUTE", "75")
			),
		),
	)


@app.activity_trigger(input_name="payload")
def refresh_company_engine(payload: dict):
	as_of = datetime.fromisoformat(payload["as_of_date"]).date()
	result = _company_engine_service().refresh(as_of)
	logging.info(
		"CompanyEngineCompleted as_of=%s companies=%s changed=%s ready=%s partial=%s withheld=%s",
		result["as_of"],
		result["companies"],
		result["changed_packages"],
		result["ready"],
		result["partial"],
		result["withheld"],
	)
	return result


@app.activity_trigger(input_name="payload")
def execute_legacy_engine_reset(payload: dict):
	if payload.get("confirmation") != RESET_CONFIRMATION_TOKEN:
		raise ValueError("legacy reset confirmation is invalid")
	reset = LegacyEngineReset(
		cosmos_endpoint=os.environ["COSMOS_ENDPOINT"],
		cosmos_database=os.environ.get("COSMOS_DATABASE_NAME", "auspex"),
		workspace_id=os.environ["ONELAKE_WORKSPACE_ID"],
		lakehouse_id=os.environ["ONELAKE_LAKEHOUSE_NAME"],
		warehouse_server=os.environ["FABRIC_WAREHOUSE_SERVER"],
		warehouse_database=os.environ.get("FABRIC_WAREHOUSE_DATABASE", "auspex_gold"),
		search_endpoint=os.environ["AI_SEARCH_ENDPOINT"],
	)
	plan, preserved = reset.inspect()
	result = reset.apply(
		plan,
		Path("/tmp/auspex-portfolio-preservation.json"),
		preserved,
	)
	logging.warning(
		"LegacyEngineResetCompleted preservation_sha256=%s",
		result["preservation_sha256"],
	)
	return result


@app.orchestration_trigger(context_name="context")
def daily_publication_tail(context):
	return daily_publication_tail_orchestrator(context)


@app.activity_trigger(input_name="payload")
def resume_fabric_capacity(payload):
	result = _daily_build_client().set_capacity_state("resume")
	logging.info("CapacityResumed")
	return result


@app.activity_trigger(input_name="payload")
def run_scheduled_connector(payload: dict):
	source_id = payload["source_id"]
	run_namespace = str(payload.get("run_namespace") or "").strip()
	if not run_namespace:
		raise ValueError("run_namespace is required")
	profiles = (
		payload.get("profiles") or alpha_vantage_profiles(payload["as_of_date"])
		if source_id == "alpha_vantage"
		else [None]
	)
	results = []
	for profile in profiles:
		options = dict(payload.get("options") or {})
		configured_limit = (
			((_SOURCE_SEEDS.get(source_id, {}).get("profiles") or {}).get(profile, {}) or {})
			.get("symbol_limit")
		)
		page_field = "filing_limit" if options.get("filing_limit") else "symbol_limit"
		offset_field = "filing_offset" if page_field == "filing_limit" else "symbol_offset"
		page_limit = int(options.get(page_field) or configured_limit or 0)
		page_offset = int(options.get(offset_field) or 0)
		while True:
			run_id_parts = [f"{run_namespace}-{source_id}", profile]
			if page_limit:
				run_id_parts.append(f"offset-{page_offset}")
			body = {
				**options,
				"source_id": source_id,
				"run_id": "-".join(filter(None, run_id_parts)),
				"mode": "backfill",
				"to_date": payload["as_of_date"],
			}
			if profile:
				body["profile"] = profile
			if page_limit:
				body[offset_field] = page_offset
				body[page_field] = page_limit
			result, _ = _execute_connector(body)
			results.append(result)
			if (
				payload.get("single_page")
				or result.get("status") == "failed"
				or not result.get("has_more")
			):
				break
			if not page_limit:
				raise RuntimeError(f"Connector {source_id} returned has_more without a page limit")
			page_offset += page_limit
	if any(result.get("status") == "failed" for result in results):
		if (_SOURCE_SEEDS.get(source_id) or {}).get("required", True):
			logging.error("RequiredConnectorFailed source_id=%s", source_id)
		else:
			logging.warning("OptionalConnectorFailed source_id=%s", source_id)
		return {"status": "failed", "source_id": source_id, "results": results}
	return {
		"status": "completed",
		"source_id": source_id,
		"results": results,
		"has_more": bool(results[-1].get("has_more")) if results else False,
		"last_event_ts": max(
			(result.get("last_event_ts") for result in results if result.get("last_event_ts")),
			default=None,
		),
		"last_cursor": max(
			(result.get("last_cursor") for result in results if result.get("last_cursor")),
			default=None,
		),
	}


@app.activity_trigger(input_name="payload")
def commit_scheduled_watermark(payload: dict):
	get_control_plane().advance_watermark(
		payload["watermark_source_id"],
		payload["run_id"],
		last_event_ts=payload.get("last_event_ts"),
		last_cursor=payload.get("last_cursor"),
	)
	return {"status": "committed"}


@app.activity_trigger(input_name="payload")
def start_fabric_notebook(payload: dict):
	return _daily_build_client().start_notebook(
		payload["as_of_date"], payload["notebook"]
	)


@app.activity_trigger(input_name="payload")
def get_fabric_notebook_status(payload: dict):
	return _daily_build_client().get_notebook_status(
		payload["job_id"], payload["notebook_id"]
	)


@app.activity_trigger(input_name="payload")
def score_daily_narrative_page(payload: dict):
	return _score_narrative_page(payload)


@app.activity_trigger(input_name="payload")
def publish_daily_narrative_features(payload):
	return _publish_narrative_features()


@app.activity_trigger(input_name="payload")
def promote_daily_warehouse(payload: dict):
	return promote_daily_warehouse_snapshot(
		as_of_date=payload["as_of_date"],
		release_run_id=payload["release_run_id"],
	)


@app.activity_trigger(input_name="payload")
def record_daily_build_completion(payload: dict):
	diagnostics = payload.get("diagnostics") or {}
	logging.info(
		"DailyBuildCompleted as_of_date=%s financing_ready=%s "
		"financing_partial=%s max_pc1_variance_share=%s score_movement_rows=%s",
		payload["as_of_date"],
		diagnostics.get("financing_ready"),
		diagnostics.get("financing_partial"),
		diagnostics.get("max_pc1_variance_share"),
		diagnostics.get("score_movement_rows"),
	)
	return {"status": "recorded"}


@app.activity_trigger(input_name="payload")
def record_daily_build_failure(payload: dict):
	logging.error(
		"DailyBuildFailed as_of_date=%s error=%s",
		payload.get("as_of_date"),
		payload.get("error"),
	)
	return {"status": "recorded"}


@app.activity_trigger(input_name="payload")
def sync_daily_serving_projections(payload):
	return _sync_serving_projections()


@app.activity_trigger(input_name="payload")
def sync_daily_active_market_projections(payload):
	return _sync_active_market_projections()


@app.activity_trigger(input_name="payload")
def sync_daily_evidence_index(payload):
	return _sync_evidence_index()


@app.activity_trigger(input_name="payload")
def suspend_fabric_capacity(payload):
	result = _daily_build_client().set_capacity_state("suspend")
	logging.info("CapacitySuspended")
	return result
