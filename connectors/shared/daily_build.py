import os
import time
from datetime import date, timedelta

import httpx
from azure.identity import DefaultAzureCredential


_ARM_SCOPE = "https://management.azure.com/.default"
_FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
_TERMINAL_FAILURE_STATES = {"cancelled", "deduped", "failed"}


def schedule_is_due(schedule, as_of_date):
	if isinstance(as_of_date, str):
		as_of_date = date.fromisoformat(as_of_date)
	if schedule == "daily":
		return True
	if schedule == "weekly":
		return as_of_date.weekday() == 6
	if schedule == "quarterly":
		return (
			as_of_date.month in {1, 4, 7, 10}
			and as_of_date.day <= 7
			and as_of_date.weekday() == 6
		)
	return False


def scheduled_source_ids(source_configs, as_of_date):
	return [
		source["source_id"]
		for source in source_configs
		if source.get("enabled")
		and schedule_is_due(source.get("schedule"), as_of_date)
	]


def alpha_vantage_profiles(as_of_date):
	profiles = ["news_daily", "macro_daily"]
	if schedule_is_due("weekly", as_of_date):
		profiles.append("themes_weekly")
	if schedule_is_due("quarterly", as_of_date):
		profiles.extend(["fundamentals_quarterly", "holdings_quarterly"])
	return profiles


def _run_fabric_pipeline(context, *, as_of_date, pipeline_name, poll_seconds):
	job = yield context.call_activity(
		"start_fabric_daily_pipeline",
		{"as_of_date": as_of_date, "pipeline_name": pipeline_name},
	)
	status_payload = {
		"job_id": job["job_id"],
		"pipeline_name": pipeline_name,
	}
	while True:
		status = yield context.call_activity(
			"get_fabric_daily_pipeline_status",
			status_payload,
		)
		normalized_status = str(status.get("status") or "").lower()
		if normalized_status == "completed":
			return job
		if normalized_status in _TERMINAL_FAILURE_STATES:
			reason = status.get("failure_reason") or normalized_status
			raise RuntimeError(
				f"Fabric pipeline {pipeline_name} failed: {reason}"
			)
		deadline = context.current_utc_datetime + timedelta(seconds=poll_seconds)
		yield context.create_timer(deadline)


def daily_build_orchestrator(context, payload=None):
	payload = payload or context.get_input() or {}
	connector_failures = []
	try:
		yield context.call_activity("resume_fabric_capacity")
		for source_id in payload.get("source_ids", []):
			profile_override = (payload.get("source_profiles") or {}).get(source_id)
			profiles = profile_override or (
				alpha_vantage_profiles(payload["as_of_date"])
				if source_id == "alpha_vantage"
				else [None]
			)
			for profile in profiles:
				options = dict((payload.get("source_options") or {}).get(source_id) or {})
				page_field = "filing_limit" if options.get("filing_limit") else "symbol_limit"
				offset_field = "filing_offset" if page_field == "filing_limit" else "symbol_offset"
				page_limit = int(options.get(page_field) or 0)
				page_offset = int(options.get(offset_field) or 0)
				last_event_ts = None
				last_cursor = None
				while True:
					page_options = dict(options)
					if page_limit:
						page_options[page_field] = page_limit
						page_options[offset_field] = page_offset
					activity_payload = {
						"source_id": source_id,
						"as_of_date": payload["as_of_date"],
						"run_namespace": payload.get("run_namespace"),
						"profiles": [profile] if profile else [None],
						"options": page_options,
						"single_page": bool(page_limit),
					}
					result = yield context.call_activity(
						"run_scheduled_connector",
						activity_payload,
					)
					if result.get("status") == "failed":
						connector_failures.append(source_id)
						break
					if result.get("last_event_ts"):
						last_event_ts = max(last_event_ts or "", result["last_event_ts"])
					if result.get("last_cursor"):
						last_cursor = max(last_cursor or "", result["last_cursor"])
					if not result.get("has_more"):
						break
					if not page_limit:
						raise RuntimeError(
							f"Connector {source_id} returned has_more without a page limit"
						)
					page_offset += page_limit
				if source_id in connector_failures:
					break
				if last_event_ts or last_cursor:
					watermark_source_id = (
						f"{source_id}:{profile}"
						if source_id == "alpha_vantage" and profile and profile != "combined"
						else source_id
					)
					yield context.call_activity(
						"commit_scheduled_watermark",
						{
							"watermark_source_id": watermark_source_id,
							"run_id": f"{payload.get('run_namespace') or 'daily-' + payload['as_of_date']}-{watermark_source_id}-watermark",
							"last_event_ts": last_event_ts,
							"last_cursor": last_cursor,
						},
					)
		if connector_failures:
			raise RuntimeError(
				"Required connectors failed: " + ", ".join(connector_failures)
			)

		poll_seconds = int(payload.get("poll_seconds", 30))
		core_pipeline_name = payload.get(
			"core_pipeline_name", "auspex_daily_build"
		)
		publish_pipeline_name = payload.get(
			"publish_pipeline_name", "auspex_daily_publish"
		)
		core_job = yield from _run_fabric_pipeline(
			context,
			as_of_date=payload["as_of_date"],
			pipeline_name=core_pipeline_name,
			poll_seconds=poll_seconds,
		)

		narrative = {
			"pages": 0,
			"documents": 0,
			"scored": 0,
			"cache_hits": 0,
		}
		after_id = ""
		while True:
			page = yield context.call_activity(
				"score_daily_narrative_page",
				{
					"after_id": after_id,
					"limit": int(payload.get("narrative_page_size", 20)),
					"max_workers": int(payload.get("narrative_max_workers", 2)),
				},
			)
			narrative["pages"] += 1
			for name in ("documents", "scored", "cache_hits"):
				narrative[name] += int(page.get(name, 0))
			if not page.get("has_more"):
				break
			next_after_id = str(page.get("next_after_id") or "")
			if not next_after_id or next_after_id == after_id:
				raise RuntimeError("E21 narrative pagination did not advance")
			after_id = next_after_id
		narrative["publication"] = yield context.call_activity(
			"publish_daily_narrative_features"
		)

		publish_job = yield from _run_fabric_pipeline(
			context,
			as_of_date=payload["as_of_date"],
			pipeline_name=publish_pipeline_name,
			poll_seconds=poll_seconds,
		)
		warehouse = yield context.call_activity(
			"promote_daily_warehouse",
			{
				"as_of_date": payload["as_of_date"],
				"release_run_id": f"daily-{payload['as_of_date'].replace('-', '')}",
			},
		)

		serving = yield context.call_activity("sync_daily_serving_projections")
		evidence = yield context.call_activity("sync_daily_evidence_index")
		result = {
			"status": "completed",
			"connector_failures": [],
			"core_pipeline_job_id": core_job["job_id"],
			"publish_pipeline_job_id": publish_job["job_id"],
			"narrative": narrative,
			"warehouse": warehouse,
			"serving": serving,
			"evidence": evidence,
		}
		yield context.call_activity(
			"record_daily_build_completion",
			{
				"as_of_date": payload["as_of_date"],
				"core_pipeline_job_id": core_job["job_id"],
				"publish_pipeline_job_id": publish_job["job_id"],
			},
		)
		return result
	except Exception as exc:
		yield context.call_activity(
			"record_daily_build_failure",
			{"as_of_date": payload.get("as_of_date"), "error": str(exc)},
		)
		raise
	finally:
		yield context.call_activity("suspend_fabric_capacity")


def promote_daily_warehouse_snapshot(
	*,
	as_of_date,
	release_run_id,
	server=None,
	database=None,
	connect_factory=None,
):
	server = server or os.environ.get("FABRIC_WAREHOUSE_SERVER", "")
	database = database or os.environ.get("FABRIC_WAREHOUSE_DATABASE", "auspex_gold")
	if not server:
		raise RuntimeError("FABRIC_WAREHOUSE_SERVER is required")
	if connect_factory is None:
		from mssql_python import connect as connect_factory
	connection = connect_factory(
		f"Server={server};Database={database};"
		"Authentication=ActiveDirectoryDefault;Encrypt=yes;TrustServerCertificate=no;"
	)
	connection.autocommit = True
	try:
		cursor = connection.cursor()
		cursor.execute(
			"EXEC dbo.usp_promote_narrative_snapshot @as_of_date = ?",
			(as_of_date,),
		)
		cursor.execute(
			"""
			SELECT status, e22_generation, e22_fingerprint, e22_row_count,
			       gold_source_row_count, gold_target_row_count
			FROM dbo.e22_release_audit
			WHERE release_run_id = ?
			""",
			(release_run_id,),
		)
		existing_release = cursor.fetchone()
		if existing_release is None:
			cursor.execute(
				"""
				SELECT TOP 1 fingerprint
				FROM auspex_bronze.dbo.narrative_premium_snapshot_manifest
				WHERE status = 'completed' AND as_of_date = ?
				ORDER BY completed_at DESC, generation DESC
				""",
				(as_of_date,),
			)
			fingerprint = cursor.fetchone()
			if fingerprint is None:
				raise RuntimeError("No completed E22 manifest exists for daily release")
			cursor.execute(
				"""
				EXEC dbo.usp_promote_e22_release
					@as_of_date = ?,
					@release_run_id = ?,
					@expected_fingerprint = ?
				""",
				(as_of_date, release_run_id, fingerprint[0]),
			)
			release_columns = [description[0] for description in cursor.description]
			release = dict(zip(release_columns, cursor.fetchone()))
		else:
			if existing_release[0] != "SUCCEEDED":
				raise RuntimeError(
					f"Existing daily Warehouse release is {existing_release[0]}"
				)
			release = {
				"status": existing_release[0],
				"generation": existing_release[1],
				"fingerprint": existing_release[2],
				"row_count": int(existing_release[3]),
				"gold_source_row_count": int(existing_release[4]),
				"gold_target_row_count": int(existing_release[5]),
			}
		cursor.execute("EXEC dbo.usp_promote_portfolio_snapshot")
		cursor.execute(
			"""
			SELECT
				(SELECT COUNT_BIG(*) FROM dbo.fact_portfolio_transaction),
				(SELECT COUNT_BIG(*) FROM dbo.fact_portfolio_position),
				(SELECT COUNT_BIG(*) FROM dbo.fact_portfolio_valuation),
				@@TRANCOUNT
			"""
		)
		transactions, positions, valuations, open_transactions = cursor.fetchone()
		if int(open_transactions):
			raise RuntimeError(
				f"Daily Warehouse promotion left {open_transactions} transactions open"
			)
		return {
			"status": "promoted",
			"release": release,
			"portfolio": {
				"transactions": int(transactions),
				"positions": int(positions),
				"valuations": int(valuations),
			},
		}
	finally:
		connection.close()


class FabricDailyBuildClient:
	def __init__(
		self,
		*,
		subscription_id=None,
		capacity_resource_group=None,
		capacity_name=None,
		workspace_id=None,
		pipeline_name=None,
		publish_pipeline_name=None,
		credential=None,
		http_client=None,
	):
		self.subscription_id = subscription_id or os.environ.get("AZURE_SUBSCRIPTION_ID", "")
		self.capacity_resource_group = capacity_resource_group or os.environ.get(
			"FABRIC_CAPACITY_RESOURCE_GROUP", ""
		)
		self.capacity_name = capacity_name or os.environ.get("FABRIC_CAPACITY_NAME", "")
		self.workspace_id = workspace_id or os.environ.get("FABRIC_WORKSPACE_ID", "")
		self.pipeline_name = pipeline_name or os.environ.get(
			"FABRIC_DAILY_PIPELINE_NAME", "auspex_daily_build"
		)
		self.publish_pipeline_name = publish_pipeline_name or os.environ.get(
			"FABRIC_DAILY_PUBLISH_PIPELINE_NAME", "auspex_daily_publish"
		)
		missing = [
			name
			for name, value in {
				"AZURE_SUBSCRIPTION_ID": self.subscription_id,
				"FABRIC_CAPACITY_RESOURCE_GROUP": self.capacity_resource_group,
				"FABRIC_CAPACITY_NAME": self.capacity_name,
				"FABRIC_WORKSPACE_ID": self.workspace_id,
				"FABRIC_DAILY_PIPELINE_NAME": self.pipeline_name,
				"FABRIC_DAILY_PUBLISH_PIPELINE_NAME": self.publish_pipeline_name,
			}.items()
			if not value
		]
		if missing:
			raise RuntimeError(f"Missing daily build settings: {', '.join(missing)}")
		self.credential = credential or DefaultAzureCredential()
		self.http = http_client or httpx.Client(timeout=60)

	@property
	def _capacity_url(self):
		return (
			"https://management.azure.com/subscriptions/"
			f"{self.subscription_id}/resourceGroups/{self.capacity_resource_group}"
			f"/providers/Microsoft.Fabric/capacities/{self.capacity_name}"
		)

	@property
	def _workspace_url(self):
		return f"https://api.fabric.microsoft.com/v1/workspaces/{self.workspace_id}"

	def _headers(self, scope):
		return {
			"Authorization": f"Bearer {self.credential.get_token(scope).token}",
			"Content-Type": "application/json",
		}

	def _capacity_state(self):
		response = self.http.get(
			f"{self._capacity_url}?api-version=2023-11-01",
			headers=self._headers(_ARM_SCOPE),
		)
		response.raise_for_status()
		return str(response.json().get("properties", {}).get("state") or "")

	def set_capacity_state(self, action, *, attempts=60, interval_seconds=10):
		if action not in {"resume", "suspend"}:
			raise ValueError("Fabric capacity action must be resume or suspend")
		target_states = {"Active"} if action == "resume" else {"Paused", "Suspended"}
		state = self._capacity_state()
		if state in target_states:
			return {"status": state}
		response = self.http.post(
			f"{self._capacity_url}/{action}?api-version=2023-11-01",
			headers=self._headers(_ARM_SCOPE),
		)
		response.raise_for_status()
		for _ in range(attempts):
			state = self._capacity_state()
			if state in target_states:
				return {"status": state}
			time.sleep(interval_seconds)
		raise TimeoutError(f"Fabric capacity did not {action}; current state is {state}")

	def _pipeline_id(self, pipeline_name=None):
		pipeline_name = pipeline_name or self.pipeline_name
		response = self.http.get(
			f"{self._workspace_url}/items",
			headers=self._headers(_FABRIC_SCOPE),
			params={"type": "DataPipeline"},
		)
		response.raise_for_status()
		matches = [
			item
			for item in response.json().get("value", [])
			if item.get("displayName") == pipeline_name
			and item.get("type") == "DataPipeline"
		]
		if len(matches) != 1:
			raise RuntimeError(
				f"Expected one Fabric DataPipeline named {pipeline_name}, found {len(matches)}"
			)
		return matches[0]["id"]

	def start_pipeline(self, as_of_date, pipeline_name=None):
		pipeline_name = pipeline_name or self.pipeline_name
		pipeline_id = self._pipeline_id(pipeline_name)
		response = self.http.post(
			f"{self._workspace_url}/items/{pipeline_id}/jobs/instances",
			headers=self._headers(_FABRIC_SCOPE),
			params={"jobType": "Pipeline"},
			json={
				"executionData": {
					"parameters": {
						"as_of_date": {"value": as_of_date, "type": "string"}
					}
				}
			},
		)
		response.raise_for_status()
		location = response.headers.get("Location")
		if not location:
			raise RuntimeError("Fabric did not return a daily pipeline job location")
		return {
			"job_id": location.rstrip("/").split("/")[-1],
			"pipeline_name": pipeline_name,
		}

	def get_pipeline_status(self, job_id, pipeline_name=None):
		pipeline_id = self._pipeline_id(pipeline_name)
		response = self.http.get(
			f"{self._workspace_url}/items/{pipeline_id}/jobs/instances/{job_id}",
			headers=self._headers(_FABRIC_SCOPE),
		)
		response.raise_for_status()
		body = response.json()
		return {
			"status": body.get("status"),
			"failure_reason": body.get("failureReason"),
		}