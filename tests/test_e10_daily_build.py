import json
import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "connectors"))

from shared.daily_build import (
    alpha_vantage_profiles,
    daily_build_orchestrator,
    scheduled_source_ids,
)


class FakeContext:
    def call_activity(self, name, payload=None):
        return ("activity", name, payload)


class DailyBuildOrchestratorTests(unittest.TestCase):
    def test_ingestion_function_keeps_durable_group_ready(self):
        function_module = (ROOT / "infra" / "modules" / "functionapp.bicep").read_text(
            encoding="utf-8"
        )

        self.assertIn("alwaysReady: isIngestion", function_module)
        self.assertIn("name: 'durable'", function_module)
        self.assertIn("instanceCount: 1", function_module)
        self.assertIn("maximumInstanceCount: isIngestion ? 2 : 100", function_module)
        self.assertIn("param alphaVantageRequestsPerMinute string = '75'", function_module)

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
        self.assertEqual(next_step[1], "start_fabric_daily_pipeline")
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

    def test_failed_pipeline_still_suspends_capacity(self):
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
                "start_fabric_daily_pipeline",
                {
                    "as_of_date": "2026-07-29",
                    "pipeline_name": "auspex_daily_build",
                },
            ),
        )
        self.assertEqual(
            orchestration.send({"job_id": "job-1"}),
            (
                "activity",
                "get_fabric_daily_pipeline_status",
                {"job_id": "job-1", "pipeline_name": "auspex_daily_build"},
            ),
        )
        self.assertEqual(
            orchestration.send({"status": "Failed", "failure_reason": "boom"}),
            (
                "activity",
                "record_daily_build_failure",
                {
                    "as_of_date": "2026-07-29",
                    "error": "Fabric pipeline auspex_daily_build failed: boom",
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
        ])
        self.assertEqual(core_names[-2:], [
            "nb_08_portfolio_derive",
            "nb_09_fundamental_anchor",
        ])
        self.assertNotRegex(json.dumps(manifest), r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-")

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
        self.assertIn('params={"jobType": "Pipeline"}', daily_build)
        self.assertNotIn('params={"jobType": "DefaultJob"}', daily_build)

    def test_scheduler_settings_are_provisioned(self):
        module = (ROOT / "infra" / "modules" / "functionapp.bicep").read_text(encoding="utf-8")
        for setting in [
            "FABRIC_CAPACITY_RESOURCE_GROUP",
            "AZURE_SUBSCRIPTION_ID",
            "FABRIC_WORKSPACE_ID",
            "FABRIC_DAILY_PIPELINE_NAME",
            "DAILY_BUILD_SCHEDULE",
        ]:
            self.assertIn(setting, module)

        host = json.loads((ROOT / "connectors" / "host.json").read_text(encoding="utf-8"))
        self.assertEqual(
            host["extensions"]["durableTask"]["storageProvider"]["maxQueuePollingInterval"],
            "00:00:05",
        )

    def test_scheduled_profiles_consume_every_symbol_page(self):
        function_app = (ROOT / "connectors" / "function_app.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("body[offset_field] = page_offset", function_app)
        self.assertIn("body[page_field] = page_limit", function_app)
        self.assertIn('not result.get("has_more")', function_app)
        self.assertIn("page_offset += page_limit", function_app)
        self.assertIn('run_id_parts.append(f"offset-{page_offset}")', function_app)

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
            "az bicep build",
        ]:
            self.assertIn(value, ci)
        for value in [
            "azure/login@v2",
            "Resume existing Fabric capacity before infrastructure update",
            "az deployment sub create",
            "config-zip",
            "deploy_fabric_items.py",
            "deploy_fabric_pipeline.py",
            "deploy_warehouse_schema.py",
            "migrate_e19_cosmos_rbac.ps1",
            "swa deploy",
        ]:
            self.assertIn(value, deploy)
        self.assertIn("AZURE_SUBSCRIPTION_ID", deploy)
        self.assertNotIn("cloudcherry-prod", deploy)

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

    def test_warehouse_deployers_do_not_bind_a_live_endpoint(self):
        scripts = [
            "deploy_e14_e6b_warehouse.py",
            "deploy_e21_warehouse.py",
            "deploy_e22_warehouse.py",
            "deploy_portfolio_warehouse.py",
            "deploy_warehouse_schema.py",
        ]
        for name in scripts:
            with self.subTest(script=name):
                source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
                self.assertIn("FABRIC_WAREHOUSE_SERVER", source)
                self.assertNotRegex(
                    source,
                    r'(?i)DEFAULT_SERVER\s*=\s*["\'](?!["\'])',
                )


if __name__ == "__main__":
    unittest.main()
