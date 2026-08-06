import json
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "connectors"))
NOTEBOOK_PIPELINES = {
    pipeline["display_name"]: pipeline["notebooks"]
    for pipeline in json.loads(
        (ROOT / "connectors" / "shared" / "notebook_pipelines.json").read_text(
            encoding="utf-8"
        )
    )["pipelines"]
}

from shared.daily_build import (
    FabricDailyBuildClient,
    alpha_vantage_profiles,
    daily_build_instance_action,
    daily_build_orchestrator,
    scheduled_source_ids,
)


class FakeContext:
    def call_activity(self, name, payload=None):
        return ("activity", name, payload)


class DailyBuildOrchestratorTests(unittest.TestCase):
    def test_scheduler_starts_absent_and_ghost_instances(self):
        ghost = type("Status", (), {"runtime_status": None})()
        self.assertEqual(daily_build_instance_action(None), "start")
        self.assertEqual(daily_build_instance_action(ghost), "start")

    def test_scheduler_skips_active_and_restarts_terminal_instances(self):
        def status(value):
            return type(
                "Status",
                (),
                {"runtime_status": type("RuntimeStatus", (), {"value": value})()},
            )()

        self.assertEqual(daily_build_instance_action(status("Running")), "skip")
        self.assertEqual(daily_build_instance_action(status("Completed")), "skip")
        for value in ("Failed", "failed", "Canceled", "canceled", "Terminated"):
            self.assertEqual(
                daily_build_instance_action(status(value)),
                "purge_and_start",
            )

    def test_completion_telemetry_includes_engine_diagnostics(self):
        function_app = (ROOT / "connectors" / "function_app.py").read_text(encoding="utf-8")
        daily_build = (ROOT / "connectors" / "shared" / "daily_build.py").read_text(encoding="utf-8")

        self.assertIn("max_pc1_variance_share", function_app)
        self.assertIn("score_movement_rows", function_app)
        self.assertIn("financing_ready", daily_build)
        self.assertIn('"diagnostics": warehouse.get("diagnostics", {})', daily_build)

    def _complete_notebook_pipeline(self, orchestration, action, pipeline_name):
        for index, notebook in enumerate(NOTEBOOK_PIPELINES[pipeline_name]):
            self.assertEqual(action[1], "start_fabric_notebook")
            self.assertEqual(action[2]["notebook"], notebook)
            status = orchestration.send({
                "job_id": f"{pipeline_name}-{index}",
                "notebook_id": f"notebook-{index}",
                "notebook_name": notebook["notebook"],
            })
            self.assertEqual(status[1], "get_fabric_notebook_status")
            action = orchestration.send({"status": "Completed"})
        return action

    def test_active_market_sync_runs_immediately_after_core_pipeline(self):
        orchestration = daily_build_orchestrator(
            FakeContext(),
            {"as_of_date": "2026-08-05", "source_ids": []},
        )

        self.assertEqual(next(orchestration), ("activity", "resume_fabric_capacity", None))
        start = orchestration.send({"status": "Active"})
        active_sync = self._complete_notebook_pipeline(
            orchestration, start, "auspex_daily_build"
        )
        self.assertEqual(
            active_sync,
            ("activity", "sync_daily_active_market_projections", None),
        )
        narrative = orchestration.send({"status": "ok"})
        self.assertEqual(narrative[1], "score_daily_narrative_page")

        self.assertEqual(
            orchestration.throw(RuntimeError("stop test")),
            (
                "activity",
                "record_daily_build_failure",
                {"as_of_date": "2026-08-05", "error": "stop test"},
            ),
        )
        self.assertEqual(
            orchestration.send({"status": "recorded"}),
            ("activity", "suspend_fabric_capacity", None),
        )
        with self.assertRaisesRegex(RuntimeError, "stop test"):
            orchestration.send({"status": "Suspended"})

    def test_recovery_can_resume_core_pipeline_at_named_notebook(self):
        orchestration = daily_build_orchestrator(
            FakeContext(),
            {
                "as_of_date": "2026-08-05",
                "source_ids": [],
                "core_notebook_start_at": "nb_10_evidence_and_iq",
            },
        )

        self.assertEqual(next(orchestration), ("activity", "resume_fabric_capacity", None))
        start = orchestration.send({"status": "Active"})
        self.assertEqual(start[1], "start_fabric_notebook")
        self.assertEqual(start[2]["notebook"]["notebook"], "nb_10_evidence_and_iq")

        self.assertEqual(
            orchestration.throw(RuntimeError("stop test"))[1],
            "record_daily_build_failure",
        )
        self.assertEqual(
            orchestration.send({"status": "recorded"}),
            ("activity", "suspend_fabric_capacity", None),
        )
        with self.assertRaisesRegex(RuntimeError, "stop test"):
            orchestration.send({"status": "Suspended"})

    def test_ingestion_function_keeps_durable_group_ready(self):
        function_module = (ROOT / "infra" / "modules" / "functionapp.bicep").read_text(
            encoding="utf-8"
        )

        self.assertIn("alwaysReady: isIngestion", function_module)
        self.assertIn("name: 'durable'", function_module)
        self.assertIn("instanceCount: 1", function_module)
        self.assertIn("maximumInstanceCount: isIngestion ? 2 : 100", function_module)
        self.assertIn("param alphaVantageRequestsPerMinute string = '75'", function_module)

    def test_web_api_financing_policy_is_externally_configured_and_fail_closed(self):
        function_module = (ROOT / "infra" / "modules" / "functionapp.bicep").read_text(
            encoding="utf-8"
        )
        main = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")

        for name in (
            "FINANCING_MAX_DILUTED_SHARE_GROWTH",
            "FINANCING_MIN_CASH_RUNWAY_YEARS",
            "FINANCING_MAX_SHELF_AGE_DAYS",
        ):
            self.assertIn(name, function_module)
        self.assertIn("financingMaxDilutedShareGrowth string = ''", main)
        self.assertIn("financingMinCashRunwayYears string = ''", main)
        self.assertIn("financingMaxShelfAgeDays string = ''", main)

    def test_fabric_client_starts_parameterized_notebook_job(self):
        credential = Mock()
        credential.get_token.return_value.token = "token"
        items = Mock()
        items.json.return_value = {
            "value": [{
                "id": "notebook-id",
                "displayName": "nb_09_fundamental_anchor",
                "type": "Notebook",
            }],
        }
        accepted = Mock(
            headers={"Location": "https://fabric/jobs/job-id"},
        )
        http = Mock()
        http.get.return_value = items
        http.post.return_value = accepted
        client = FabricDailyBuildClient(
            subscription_id="subscription-id",
            capacity_resource_group="resource-group",
            capacity_name="capacity",
            workspace_id="workspace-id",
            pipeline_name="auspex_daily_build",
            publish_pipeline_name="auspex_daily_publish",
            credential=credential,
            http_client=http,
        )

        result = client.start_notebook("2026-08-05", {
            "notebook": "nb_09_fundamental_anchor",
            "parameters": {
                "from_date": "@pipeline().parameters.as_of_date",
                "to_date": "@pipeline().parameters.as_of_date",
                "max_anchor_dates": "1",
            },
        })

        self.assertEqual(result["job_id"], "job-id")
        self.assertEqual(result["notebook_id"], "notebook-id")
        request = http.post.call_args
        self.assertEqual(request.kwargs["params"], {"jobType": "RunNotebook"})
        self.assertEqual(
            request.kwargs["json"]["executionData"]["parameters"],
            {
                "from_date": {"value": "2026-08-05", "type": "string"},
                "to_date": {"value": "2026-08-05", "type": "string"},
                "max_anchor_dates": {"value": "1", "type": "string"},
            },
        )

    @patch("shared.daily_build.time.sleep")
    def test_fabric_notebook_status_retries_connect_timeout(self, sleep):
        credential = Mock()
        credential.get_token.return_value.token = "token"
        completed = Mock()
        completed.json.return_value = {
            "status": "Completed",
            "failureReason": None,
        }
        http = Mock()
        http.get.side_effect = [
            httpx.ConnectTimeout(
                "timed out",
                request=httpx.Request("GET", "https://api.fabric.microsoft.com"),
            ),
            completed,
        ]
        client = FabricDailyBuildClient(
            subscription_id="subscription-id",
            capacity_resource_group="resource-group",
            capacity_name="capacity",
            workspace_id="workspace-id",
            pipeline_name="auspex_daily_build",
            publish_pipeline_name="auspex_daily_publish",
            credential=credential,
            http_client=http,
        )

        result = client.get_notebook_status("job-id", "notebook-id")

        self.assertEqual(result, {"status": "Completed", "failure_reason": None})
        self.assertEqual(http.get.call_count, 2)
        sleep.assert_called_once_with(1)

    @patch("shared.daily_build.time.sleep")
    def test_fabric_notebook_status_retries_server_error(self, sleep):
        credential = Mock()
        credential.get_token.return_value.token = "token"
        request = httpx.Request("GET", "https://api.fabric.microsoft.com")
        unavailable = Mock()
        unavailable.raise_for_status.side_effect = httpx.HTTPStatusError(
            "service unavailable",
            request=request,
            response=httpx.Response(503, request=request),
        )
        completed = Mock()
        completed.json.return_value = {
            "status": "Completed",
            "failureReason": None,
        }
        http = Mock()
        http.get.side_effect = [unavailable, completed]
        client = FabricDailyBuildClient(
            subscription_id="subscription-id",
            capacity_resource_group="resource-group",
            capacity_name="capacity",
            workspace_id="workspace-id",
            pipeline_name="auspex_daily_build",
            publish_pipeline_name="auspex_daily_publish",
            credential=credential,
            http_client=http,
        )

        result = client.get_notebook_status("job-id", "notebook-id")

        self.assertEqual(result["status"], "Completed")
        self.assertEqual(http.get.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_failed_resume_still_requests_capacity_suspension(self):
        orchestration = daily_build_orchestrator(
            FakeContext(),
            {"as_of_date": "2026-07-29"},
        )

        self.assertEqual(
            next(orchestration),
            ("activity", "resume_fabric_capacity", None),
        )
        self.assertEqual(
            orchestration.throw(RuntimeError("resume failed")),
            (
                "activity",
                "record_daily_build_failure",
                {"as_of_date": "2026-07-29", "error": "resume failed"},
            ),
        )
        self.assertEqual(
            orchestration.send({"status": "recorded"}),
            ("activity", "suspend_fabric_capacity", None),
        )
        with self.assertRaisesRegex(RuntimeError, "resume failed"):
            orchestration.send({"status": "Suspended"})

    def test_failed_connector_stops_before_fabric_and_suspends_capacity(self):
        orchestration = daily_build_orchestrator(
            FakeContext(),
            {
                "as_of_date": "2026-07-29",
                "source_ids": ["sec_form4"],
            },
        )

        next(orchestration)
        self.assertEqual(
            orchestration.send({"status": "Active"}),
            (
                "activity",
                "run_scheduled_connector",
                {
                    "source_id": "sec_form4",
                    "as_of_date": "2026-07-29",
                    "run_namespace": None,
                    "profiles": [None],
                    "options": {},
                    "single_page": False,
                },
            ),
        )
        self.assertEqual(
            orchestration.send({"status": "failed"}),
            (
                "activity",
                "record_daily_build_failure",
                {
                    "as_of_date": "2026-07-29",
                    "error": "Required connectors failed: sec_form4",
                },
            ),
        )
        self.assertEqual(
            orchestration.send({"status": "recorded"}),
            ("activity", "suspend_fabric_capacity", None),
        )
        with self.assertRaisesRegex(RuntimeError, "sec_form4"):
            orchestration.send({"status": "Suspended"})

    def test_optional_connector_failure_continues_to_fabric(self):
        orchestration = daily_build_orchestrator(
            FakeContext(),
            {
                "as_of_date": "2026-08-05",
                "source_ids": ["theme_classifier"],
                "optional_source_ids": ["theme_classifier"],
            },
        )

        next(orchestration)
        connector = orchestration.send({"status": "Active"})
        self.assertEqual(connector[1], "run_scheduled_connector")
        fabric = orchestration.send({"status": "failed"})
        self.assertEqual(fabric[1], "start_fabric_notebook")

        self.assertEqual(
            orchestration.throw(RuntimeError("stop test")),
            (
                "activity",
                "record_daily_build_failure",
                {"as_of_date": "2026-08-05", "error": "stop test"},
            ),
        )
        self.assertEqual(
            orchestration.send({"status": "recorded"}),
            ("activity", "suspend_fabric_capacity", None),
        )
        with self.assertRaisesRegex(RuntimeError, "stop test"):
            orchestration.send({"status": "Suspended"})

    def test_profile_specific_limit_checkpoints_alpha_pages(self):
        orchestration = daily_build_orchestrator(
            FakeContext(),
            {
                "as_of_date": "2026-08-05",
                "source_ids": ["alpha_vantage"],
                "source_profiles": {"alpha_vantage": ["news_daily"]},
                "source_profile_options": {
                    "alpha_vantage": {"news_daily": {"symbol_limit": 2}},
                },
            },
        )

        next(orchestration)
        first_page = orchestration.send({"status": "Active"})
        self.assertEqual(first_page[2]["options"]["symbol_offset"], 0)
        second_page = orchestration.send({
            "status": "completed",
            "has_more": True,
            "last_event_ts": "2026-08-05",
            "last_cursor": "2026-08-05",
        })
        self.assertEqual(second_page[2]["options"]["symbol_offset"], 2)

    def test_recovery_can_override_alpha_vantage_profiles(self):
        orchestration = daily_build_orchestrator(
            FakeContext(),
            {
                "as_of_date": "2026-08-03",
                "source_ids": ["alpha_vantage"],
                "source_profiles": {
                    "alpha_vantage": ["themes_weekly", "fundamentals_quarterly"],
                },
                "source_options": {
                    "alpha_vantage": {"since_date": "2026-07-29", "symbol_limit": 25},
                },
                "run_namespace": "freshness-20260803-v2",
            },
        )

        next(orchestration)
        self.assertEqual(
            orchestration.send({"status": "Active"}),
            (
                "activity",
                "run_scheduled_connector",
                {
                    "source_id": "alpha_vantage",
                    "as_of_date": "2026-08-03",
                    "run_namespace": "freshness-20260803-v2",
                    "profiles": ["themes_weekly"],
                    "options": {
                        "since_date": "2026-07-29",
                        "symbol_limit": 25,
                        "symbol_offset": 0,
                    },
                    "single_page": True,
                },
            ),
        )
        self.assertEqual(
            orchestration.throw(RuntimeError("stop test")),
            (
                "activity",
                "record_daily_build_failure",
                {"as_of_date": "2026-08-03", "error": "stop test"},
            ),
        )
        self.assertEqual(
            orchestration.send({"status": "recorded"}),
            ("activity", "suspend_fabric_capacity", None),
        )
        with self.assertRaisesRegex(RuntimeError, "stop test"):
            orchestration.send({"status": "Suspended"})

    def test_paged_source_commits_watermark_only_after_terminal_page(self):
        orchestration = daily_build_orchestrator(
            FakeContext(),
            {
                "as_of_date": "2026-08-03",
                "run_namespace": "freshness-test",
                "source_ids": ["prices_eod"],
                "source_options": {
                    "prices_eod": {"since_date": "2026-07-15", "symbol_limit": 2},
                },
            },
        )

        next(orchestration)
        first_page = orchestration.send({"status": "Active"})
        self.assertEqual(first_page[1], "run_scheduled_connector")
        self.assertEqual(first_page[2]["options"]["symbol_offset"], 0)
        second_page = orchestration.send({
            "status": "completed",
            "has_more": True,
            "last_event_ts": "2026-07-31",
            "last_cursor": "2026-07-31",
        })
        self.assertEqual(second_page[1], "run_scheduled_connector")
        self.assertEqual(second_page[2]["options"]["symbol_offset"], 2)
        commit = orchestration.send({
            "status": "completed",
            "has_more": False,
            "last_event_ts": "2026-07-31",
            "last_cursor": "2026-07-31",
        })
        self.assertEqual(
            commit,
            (
                "activity",
                "commit_scheduled_watermark",
                {
                    "watermark_source_id": "prices_eod",
                    "run_id": "freshness-test-prices_eod-watermark",
                    "last_event_ts": "2026-07-31",
                    "last_cursor": "2026-07-31",
                },
            ),
        )
        next_step = orchestration.send({"status": "committed"})
        self.assertEqual(next_step[1], "start_fabric_notebook")
        self.assertEqual(
            orchestration.throw(RuntimeError("stop test"))[1],
            "record_daily_build_failure",
        )
        self.assertEqual(
            orchestration.send({"status": "recorded"}),
            ("activity", "suspend_fabric_capacity", None),
        )
        with self.assertRaisesRegex(RuntimeError, "stop test"):
            orchestration.send({"status": "Suspended"})

    def test_failed_notebook_still_suspends_capacity(self):
        orchestration = daily_build_orchestrator(
            FakeContext(),
            {"as_of_date": "2026-07-29"},
        )

        self.assertEqual(
            next(orchestration),
            ("activity", "resume_fabric_capacity", None),
        )
        self.assertEqual(
            orchestration.send({"status": "Resumed"}),
            (
                "activity",
                "start_fabric_notebook",
                {
                    "as_of_date": "2026-07-29",
                    "pipeline_name": "auspex_daily_build",
                    "notebook": NOTEBOOK_PIPELINES["auspex_daily_build"][0],
                },
            ),
        )
        self.assertEqual(
            orchestration.send({
                "job_id": "job-1",
                "notebook_id": "notebook-1",
                "notebook_name": "nb_00_bronze_health",
            }),
            (
                "activity",
                "get_fabric_notebook_status",
                {
                    "job_id": "job-1",
                    "notebook_id": "notebook-1",
                    "notebook_name": "nb_00_bronze_health",
                },
            ),
        )
        self.assertEqual(
            orchestration.send({"status": "Failed", "failure_reason": "boom"}),
            (
                "activity",
                "record_daily_build_failure",
                {
                    "as_of_date": "2026-07-29",
                    "error": "Fabric notebook nb_00_bronze_health failed: boom",
                },
            ),
        )
        self.assertEqual(
            orchestration.send({"status": "recorded"}),
            ("activity", "suspend_fabric_capacity", None),
        )
        with self.assertRaisesRegex(RuntimeError, "boom"):
            orchestration.send({"status": "Suspended"})

    def test_pipeline_manifest_is_serial_and_environment_neutral(self):
        manifest = json.loads(
            (ROOT / "fabric" / "pipelines" / "daily_build.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [pipeline["display_name"] for pipeline in manifest["pipelines"]],
            ["auspex_daily_build", "auspex_daily_publish"],
        )
        core_names = [entry["notebook"] for entry in manifest["pipelines"][0]["notebooks"]]
        publish_names = [entry["notebook"] for entry in manifest["pipelines"][1]["notebooks"]]
        self.assertEqual(publish_names, [
            "nb_11_narrative_intensity",
            "nb_12_narrative_premium",
            "nb_04_metrics",
            "nb_08_portfolio_derive",
        ])
        self.assertEqual(core_names[-2:], [
            "nb_08_portfolio_derive",
            "nb_09_fundamental_anchor",
        ])
        self.assertNotRegex(json.dumps(manifest), r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-")
        self.assertEqual(
            json.loads(
                (ROOT / "connectors" / "shared" / "notebook_pipelines.json").read_text(
                    encoding="utf-8"
                )
            )["pipelines"],
            [
                {"display_name": pipeline["display_name"], "notebooks": pipeline["notebooks"]}
                for pipeline in manifest["pipelines"]
            ],
        )

    def test_pipeline_definition_uses_fabric_expression_parameters(self):
        script_path = ROOT / "scripts" / "deploy_fabric_pipeline.py"
        spec = importlib.util.spec_from_file_location("deploy_fabric_pipeline", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        manifest = json.loads(
            (ROOT / "fabric" / "pipelines" / "daily_build.json").read_text(encoding="utf-8")
        )
        core = manifest["pipelines"][0]
        notebook_ids = {
            entry["notebook"]: f"id-{index}"
            for index, entry in enumerate(core["notebooks"])
        }
        definition = module.build_pipeline_definition(core, "workspace-id", notebook_ids)
        to_date = definition["properties"]["activities"][0]["typeProperties"]["parameters"]["to_date"]
        self.assertEqual(
            to_date,
            {"value": "@pipeline().parameters.as_of_date", "type": "Expression"},
        )

        daily_build = (ROOT / "connectors" / "shared" / "daily_build.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('params={"jobType": "RunNotebook"}', daily_build)
        self.assertIn("start_notebook", daily_build)
        self.assertIn("get_notebook_status", daily_build)

    def test_scheduler_settings_are_provisioned(self):
        module = (ROOT / "infra" / "modules" / "functionapp.bicep").read_text(encoding="utf-8")
        for setting in [
            "FABRIC_CAPACITY_RESOURCE_GROUP",
            "AZURE_SUBSCRIPTION_ID",
            "FABRIC_WORKSPACE_ID",
            "FABRIC_DAILY_PIPELINE_NAME",
            "DAILY_BUILD_SCHEDULE",
            "DAILY_BUILD_PRICE_PAGE_SIZE",
            "DAILY_BUILD_SEC_PAGE_SIZE",
            "DAILY_BUILD_NARRATIVE_MAX_WORKERS",
        ]:
            self.assertIn(setting, module)
        self.assertEqual(module.count("name: 'DAILY_BUILD_NARRATIVE_MAX_WORKERS'"), 1)

        host = json.loads((ROOT / "connectors" / "host.json").read_text(encoding="utf-8"))
        self.assertEqual(
            host["extensions"]["durableTask"]["storageProvider"]["maxQueuePollingInterval"],
            "00:00:05",
        )

    def test_scheduled_profiles_consume_every_symbol_page(self):
        function_app = (ROOT / "connectors" / "function_app.py").read_text(
            encoding="utf-8"
        )
        daily_build = (ROOT / "connectors" / "shared" / "daily_build.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("body[offset_field] = page_offset", function_app)
        self.assertIn("body[page_field] = page_limit", function_app)
        self.assertIn('not result.get("has_more")', function_app)
        self.assertIn('bool(results[-1].get("has_more")) if results else False', function_app)
        self.assertNotIn('any(result.get("has_more") for result in results)', function_app)
        self.assertIn("page_offset += page_limit", function_app)
        self.assertIn('run_id_parts.append(f"offset-{page_offset}")', function_app)
        self.assertIn('os.environ.get("DAILY_BUILD_PRICE_PAGE_SIZE", "50")', function_app)
        self.assertIn('os.environ.get("DAILY_BUILD_SEC_PAGE_SIZE", "50")', function_app)
        self.assertIn('("sec_13f", "sec_13dg", "sec_8k", "sec_s1")', function_app)
        self.assertIn('"sec_companyfacts": {', function_app)
        self.assertIn('"optional_source_ids"', function_app)
        self.assertIn('"source_profile_options"', function_app)
        self.assertIn("profile_options", daily_build)
        self.assertIn("optional_connector_failures", daily_build)
        self.assertIn("OptionalConnectorFailed", function_app)
        self.assertIn('os.environ.get("DAILY_BUILD_NARRATIVE_PAGE_SIZE", "5")', function_app)
        self.assertIn('os.environ.get("DAILY_BUILD_NARRATIVE_MAX_WORKERS", "1")', function_app)
        self.assertIn("await client.purge_instance_history(instance_id)", function_app)

    def test_source_cadence_and_alpha_vantage_profiles_are_scoped(self):
        sources = [
            {"source_id": "daily", "schedule": "daily", "enabled": True},
            {"source_id": "weekly", "schedule": "weekly", "enabled": True},
            {"source_id": "quarterly", "schedule": "quarterly", "enabled": True},
            {"source_id": "manual", "schedule": "on_change", "enabled": True},
        ]
        self.assertEqual(
            scheduled_source_ids(sources, "2026-07-05"),
            ["daily", "weekly", "quarterly"],
        )
        self.assertEqual(scheduled_source_ids(sources, "2026-07-06"), ["daily"])
        self.assertEqual(
            alpha_vantage_profiles("2026-07-05"),
            [
                "news_daily",
                "macro_daily",
                "themes_weekly",
                "fundamentals_quarterly",
                "holdings_quarterly",
            ],
        )
        self.assertEqual(
            alpha_vantage_profiles("2026-07-06"),
            ["news_daily", "macro_daily"],
        )

    def test_ci_and_deploy_workflows_cover_the_release(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        deploy = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
        for value in [
            "python -m unittest discover",
            "npm run lint",
            "npm run build",
            "bootstrap-fabric.bicep",
            "az bicep build",
        ]:
            self.assertIn(value, ci)
        for value in [
            "azure/login@v2",
            "Resume existing Fabric capacity before infrastructure update",
            "az deployment sub create",
            "config-zip",
            "Seed required ETF linkage inputs",
            "etf_holdings",
            "deploy_fabric_items.py",
            "deploy_fabric_pipeline.py",
            "run_fabric_schema_refresh.py",
            "deploy_warehouse_schema.py",
            "migrate_e19_cosmos_rbac.ps1",
            "ALPHAVANTAGE_API_KEY",
            "FINNHUB_API_KEY",
            "swa deploy",
        ]:
            self.assertIn(value, deploy)
        self.assertIn("AZURE_SUBSCRIPTION_ID", deploy)
        self.assertNotIn("cloudcherry-prod", deploy)

    def test_fabric_schema_refresh_precedes_warehouse_deployment(self):
        deploy = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(
            encoding="utf-8"
        )
        self.assertLess(
            deploy.index("ensure_fabric_workspace_access.py"),
            deploy.index("Seed required ETF linkage inputs"),
        )
        self.assertLess(
            deploy.index("Seed required ETF linkage inputs"),
            deploy.index("deploy_fabric_items.py"),
        )
        self.assertLess(
            deploy.index("run_fabric_schema_refresh.py"),
            deploy.index("deploy_warehouse_schema.py"),
        )
        refresh = (ROOT / "scripts" / "run_fabric_schema_refresh.py").read_text(
            encoding="utf-8"
        )
        for notebook in (
            "nb_13_source_history_to_silver",
            "nb_05_alpha_vantage_to_gold",
            "nb_09_fundamental_anchor",
            "nb_04_metrics",
        ):
            self.assertIn(notebook, refresh)

    def test_portable_fabric_bindings_are_injected_at_deploy_time(self):
        script_path = ROOT / "scripts" / "deploy_fabric_items.py"
        spec = importlib.util.spec_from_file_location("deploy_fabric_items", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source = (
            f'{{"workspaceId":"{module.WORKSPACE_TOKEN}",'
            f'"itemId":"{module.LAKEHOUSE_TOKEN}"}}'
        )
        bound = module.bind_definition_text(source, "workspace-guid", "lakehouse-guid")
        self.assertEqual(
            bound,
            '{"workspaceId":"workspace-guid","itemId":"lakehouse-guid"}',
        )

    def test_fabric_deployer_reads_async_operation_result(self):
        script_path = ROOT / "scripts" / "deploy_fabric_items.py"
        spec = importlib.util.spec_from_file_location("deploy_fabric_items", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        credential = Mock()
        credential.get_token.return_value.token = "token"
        accepted = Mock(status_code=202, headers={"Location": "https://operation/1"})
        operation = Mock(status_code=200, headers={})
        operation.json.return_value = {"status": "Succeeded"}
        operation_result = Mock(status_code=200)
        operation_result.json.return_value = {
            "definition": {"parts": [{"path": "graph.json"}]}
        }
        http = Mock()
        http.get.side_effect = [operation, operation_result]
        deployer = module.FabricItemDeployer(
            "workspace-id",
            "lakehouse-id",
            "Auspex/1.0 operations@example.com",
            credential=credential,
            http_client=http,
        )

        result = deployer._wait_for_operation(accepted)

        self.assertEqual(result["definition"]["parts"][0]["path"], "graph.json")
        self.assertEqual(http.get.call_args_list[1].args[0], "https://operation/1/result")

    def test_warehouse_deployers_do_not_bind_a_live_endpoint(self):
        source = (ROOT / "scripts" / "deploy_warehouse_schema.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("FABRIC_WAREHOUSE_SERVER", source)
        self.assertIn("Warehouse deployment failed in", source)
        self.assertIn("batch {batch_index}", source)
        self.assertNotRegex(
            source,
            r'(?i)DEFAULT_SERVER\s*=\s*["\'](?!["\'])',
        )


if __name__ == "__main__":
    unittest.main()
