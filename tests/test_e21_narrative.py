import json
from pathlib import Path
import unittest

from tests.fabric_notebook import notebook_cells, notebook_code


ROOT = Path(__file__).resolve().parents[1]


class E21NarrativeEngineTests(unittest.TestCase):
    def test_parser_returns_bounded_features_with_verbatim_citations(self):
        from engine.narrative_features import parse_narrative_response

        content = (
            "Management expects a massive addressable market next year. "
            "Revenue increased 12 percent this quarter."
        )
        response = json.dumps({
            "sentiment": 0.6,
            "relevance": 0.9,
            "forward_promise_ratio": 0.7,
            "hype_density": 0.4,
            "themes": [{"label": "AI Infrastructure", "evidence_index": 0}],
            "evidence_indices": {
                "sentiment": 1,
                "forward_promise_ratio": 0,
                "hype_density": 0,
            },
        })

        result = parse_narrative_response(response, content, max_excerpt_chars=70)

        self.assertEqual(result.sentiment, 0.6)
        self.assertEqual(result.forward_promise_ratio, 0.7)
        self.assertEqual(result.themes, ("ai_infrastructure",))
        self.assertTrue(all(quote in content for quote in result.evidence_quotes.values()))
        self.assertIn(result.theme_evidence["ai_infrastructure"], content)

    def test_messages_state_the_exact_valid_evidence_index_range(self):
        from engine.narrative_features import narrative_messages

        messages = narrative_messages(
            "Cloud demand",
            "First evidence excerpt. Second evidence excerpt.",
            "System prompt",
            max_excerpt_chars=25,
        )

        self.assertIn("Valid evidence indexes are integers from 0 through 1.", messages[1]["content"])

    def test_parser_rejects_invalid_ranges_indexes_and_theme_contract(self):
        from engine.narrative_features import parse_narrative_response

        content = "Demand grew rapidly. Guidance remains unchanged."
        valid = {
            "sentiment": 0.1,
            "relevance": 0.8,
            "forward_promise_ratio": 0.2,
            "hype_density": 0.1,
            "themes": [{"label": "Cloud", "evidence_index": 0}],
            "evidence_indices": {
                "sentiment": 0,
                "forward_promise_ratio": 0,
                "hype_density": 0,
            },
        }
        invalid_range = dict(valid, hype_density=1.2)
        with self.assertRaisesRegex(ValueError, "hype_density"):
            parse_narrative_response(json.dumps(invalid_range), content)
        invalid_index = json.loads(json.dumps(valid))
        invalid_index["evidence_indices"]["sentiment"] = 99
        with self.assertRaisesRegex(ValueError, "evidence index"):
            parse_narrative_response(json.dumps(invalid_index), content)
        too_many_themes = dict(valid, themes=[
            {"label": f"Theme {index}", "evidence_index": 0}
            for index in range(6)
        ])
        with self.assertRaisesRegex(ValueError, "at most five"):
            parse_narrative_response(json.dumps(too_many_themes), content)

    def test_cache_key_is_versioned_and_replay_stable(self):
        from engine.narrative_features import narrative_cache_key

        first = narrative_cache_key("a" * 64, "gpt-4o:2024-11-20", "e21_narrative_v1")
        replay = narrative_cache_key("a" * 64, "gpt-4o:2024-11-20", "e21_narrative_v1")
        changed_prompt = narrative_cache_key("a" * 64, "gpt-4o:2024-11-20", "e21_narrative_v2")

        self.assertEqual(first, replay)
        self.assertNotEqual(first, changed_prompt)

    def test_composite_is_deterministic_partial_and_withheld_below_coverage(self):
        from engine.narrative_features import NarrativeInputs, compute_narrative_intensity

        inputs = NarrativeInputs(
            eligible_document_count=10,
            extracted_document_count=10,
            sentiment_level=0.5,
            sentiment_velocity_z=1.5,
            theme_concentration=0.8,
            forward_promise_ratio=0.7,
            hype_density=0.4,
            news_volume_z_30d=2.0,
            insider_net_buy_ratio_90d=-0.6,
            mgmt_reality_gap=None,
            revision_dispersion_z=None,
            options_skew=None,
        )

        first = compute_narrative_intensity(inputs)
        replay = compute_narrative_intensity(inputs)

        self.assertEqual(first, replay)
        self.assertEqual(first.coverage_status, "PARTIAL")
        self.assertIsNotNone(first.narrative_intensity)
        self.assertGreaterEqual(first.narrative_intensity, 0.0)
        self.assertLessEqual(first.narrative_intensity, 100.0)
        self.assertTrue(any(reason.startswith("mgmt_reality_gap:") for reason in first.coverage_reasons))
        self.assertTrue(any(reason.startswith("revision_dispersion_z:") for reason in first.coverage_reasons))
        self.assertTrue(any(reason.startswith("options_skew:") for reason in first.coverage_reasons))

        withheld = compute_narrative_intensity(NarrativeInputs(
            eligible_document_count=2,
            extracted_document_count=2,
            sentiment_level=0.5,
            sentiment_velocity_z=None,
            theme_concentration=None,
            forward_promise_ratio=0.7,
            hype_density=0.4,
            news_volume_z_30d=None,
            insider_net_buy_ratio_90d=None,
            mgmt_reality_gap=None,
            revision_dispersion_z=None,
            options_skew=None,
        ))
        self.assertEqual(withheld.coverage_status, "WITHHELD")
        self.assertIsNone(withheld.narrative_intensity)

    def test_service_scores_once_then_reuses_immutable_cache(self):
        from search.narrative import NarrativeFeatureService

        class FakeChat:
            def __init__(self):
                self.calls = 0

            def complete_json(self, messages):
                self.calls += 1
                return json.dumps({
                    "sentiment": 0.2,
                    "relevance": 0.8,
                    "forward_promise_ratio": 0.3,
                    "hype_density": 0.1,
                    "themes": [{"label": "Cloud", "evidence_index": 0}],
                    "evidence_indices": {
                        "sentiment": 0,
                        "forward_promise_ratio": 0,
                        "hype_density": 0,
                    },
                })

        class FakeCache:
            def __init__(self):
                self.documents = {}

            def get(self, key):
                return self.documents.get(key)

            def create(self, document):
                self.documents[document["id"]] = document
                return document

        document = {
            "id": "doc-1",
            "security_sk": 42,
            "source_id": "news:42",
            "source_type": "news",
            "revision_hash": "b" * 64,
            "title": "Cloud demand",
            "content": "Cloud demand increased and guidance remained stable.",
            "event_date": "2026-07-20",
            "knowledge_date": "2026-07-21",
            "generation": "e7-generation",
        }
        chat = FakeChat()
        service = NarrativeFeatureService(
            chat,
            FakeCache(),
            model_version="gpt-4o:2024-11-20",
            prompt_text=(ROOT / "prompts" / "narrative" / "e21_v1.txt").read_text(encoding="utf-8"),
        )

        first, first_cached = service.score(document)
        replay, replay_cached = service.score(document)

        self.assertFalse(first_cached)
        self.assertTrue(replay_cached)
        self.assertEqual(first, replay)
        self.assertEqual(chat.calls, 1)
        self.assertEqual(first["input_generation"], "e7-generation")
        self.assertEqual(first["prompt_version"], "e21_narrative_v1")
        self.assertEqual(first["prompt_sha256"], "70987525ba240b9008ec684c5cab346cfd02b10f8315d7c2f66adff381c930a5")

    def test_service_repairs_two_invalid_responses_before_caching(self):
        from search.narrative import NarrativeFeatureService

        invalid = json.dumps({"sentiment": 2})
        valid = json.dumps({
            "sentiment": 0.2,
            "relevance": 0.8,
            "forward_promise_ratio": 0.3,
            "hype_density": 0.1,
            "themes": [{"label": "Cloud", "evidence_index": 0}],
            "evidence_indices": {
                "sentiment": 0,
                "forward_promise_ratio": 0,
                "hype_density": 0,
            },
        })

        class FakeChat:
            def __init__(self):
                self.responses = [invalid, invalid, valid]

            def complete_json(self, messages):
                return self.responses.pop(0)

        class FakeCache:
            def get(self, key):
                return None

            def create(self, document):
                return document

        service = NarrativeFeatureService(
            FakeChat(),
            FakeCache(),
            model_version="gpt-4o:2024-11-20",
            prompt_text=(ROOT / "prompts" / "narrative" / "e21_v1.txt").read_text(encoding="utf-8"),
        )
        result, cached = service.score({
            "id": "doc-repair",
            "security_sk": 42,
            "source_id": "news:repair",
            "source_type": "news",
            "revision_hash": "c" * 64,
            "title": "Cloud demand",
            "content": "Cloud demand increased.",
            "event_date": "2026-07-20",
            "knowledge_date": "2026-07-21",
            "generation": "e7-generation",
        })

        self.assertFalse(cached)
        self.assertEqual(result["sentiment"], 0.2)

    def test_projection_requires_complete_current_cache_and_ignores_stale_audit_rows(self):
        from search.narrative import build_narrative_projection

        evidence = [{
            "id": "doc-1",
            "security_sk": 1,
            "source_type": "news",
            "source_id": "news:1",
            "revision_hash": "a" * 64,
            "event_date": "2026-07-23",
            "knowledge_date": "2026-07-24",
            "generation": "e7-current",
        }]
        current = {
            "id": "cache-1",
            "document_id": "doc-1",
            "document_revision_hash": "a" * 64,
            "created_at": "2026-07-24T00:00:00+00:00",
        }
        stale = {
            "id": "cache-old",
            "document_id": "doc-old",
            "document_revision_hash": "b" * 64,
            "created_at": "2026-07-23T00:00:00+00:00",
        }

        projection, manifest = build_narrative_projection(evidence, [current, stale])
        self.assertEqual([document["document_id"] for document in projection], ["doc-1"])
        self.assertEqual(manifest["document_count"], 1)
        self.assertEqual(manifest["stale_cache_count"], 1)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            build_narrative_projection(evidence, [stale])

    def test_narrative_pagination_caps_each_security_to_three_newest_documents(self):
        from search.narrative import page_narrative_documents

        documents = [
            {
                "id": f"a{index}",
                "security_sk": 1,
                "source_type": "news",
                "event_date": f"2026-07-{index:02d}",
                "knowledge_date": f"2026-07-{index:02d}",
                "revision_hash": str(index),
            }
            for index in range(1, 6)
        ] + [
            {
                "id": "b1",
                "security_sk": 2,
                "source_type": "news",
                "event_date": "2026-07-01",
                "knowledge_date": "2026-07-01",
                "revision_hash": "1",
            },
            {"id": "filing", "security_sk": 1, "source_type": "sec_filing"},
        ]

        first, cursor, has_more = page_narrative_documents(documents, limit=2)
        second, _, second_has_more = page_narrative_documents(
            documents, limit=2, after_id=cursor
        )

        self.assertEqual([document["id"] for document in first], ["a3", "a4"])
        self.assertTrue(has_more)
        self.assertEqual([document["id"] for document in second], ["a5", "b1"])
        self.assertFalse(second_has_more)

    def test_narrative_pagination_excludes_news_outside_active_universe(self):
        from search.narrative import page_narrative_documents

        documents = [
            {
                "id": "active",
                "security_sk": 1,
                "symbol": "AAA",
                "source_type": "news",
                "event_date": "2026-08-03",
                "knowledge_date": "2026-08-04",
                "revision_hash": "a" * 64,
            },
            {
                "id": "inactive",
                "security_sk": 2,
                "symbol": "ZZZ",
                "source_type": "news",
                "event_date": "2026-08-03",
                "knowledge_date": "2026-08-04",
                "revision_hash": "b" * 64,
            },
        ]

        page, _, has_more = page_narrative_documents(
            documents,
            limit=10,
            eligible_symbols={"aaa"},
        )

        self.assertEqual([document["id"] for document in page], ["active"])
        self.assertFalse(has_more)


class E21ArtifactContractTests(unittest.TestCase):
    def test_notebook_is_canonical_and_enforces_snapshot_contract(self):
        notebook = notebook_code("nb_11_narrative_intensity")
        parameter_cells = [
            code
            for marker, code in notebook_cells("nb_11_narrative_intensity")
            if marker == "PARAMETERS CELL"
        ]

        self.assertEqual(parameter_cells, ['as_of_date = ""'])
        for value in [
            'NARRATIVE_PATH = "Files/serving/narrative_features"',
            'PROMPT_VERSION = "e21_narrative_v1"',
            'MODEL_VERSION = "gpt-4o:2024-11-20"',
            'CREATE TABLE IF NOT EXISTS fact_narrative_features',
            'CREATE TABLE IF NOT EXISTS fact_narrative_intensity',
            'CREATE TABLE IF NOT EXISTS narrative_snapshot_manifest',
            'source_unavailable',
            'coverage_status',
            'WITHHELD',
            'PARTIAL',
            'theme_concentration',
            'sentiment_velocity_z',
            'whenMatchedUpdate',
            'immutable_conflicts',
            'mssparkutils.notebook.exit(run_summary_json)',
        ]:
            with self.subTest(value=value):
                self.assertIn(value, notebook)

        self.assertNotIn('F.lit("READY")', notebook)
        self.assertIn('F.col("knowledge_date") <= as_of', notebook)
        self.assertIn('F.col("max_knowledge_date") <= as_of', notebook)
        self.assertIn('F.col("event_date") <= F.col("knowledge_date")', notebook)
        self.assertIn('t.cache_key = s.cache_key', notebook)
        self.assertIn('"sentiment_strength": 0.10', notebook)
        self.assertIn('"sentiment_velocity_strength": 0.10', notebook)
        self.assertIn('"theme_concentration": 0.15', notebook)
        self.assertIn('"forward_promise_ratio": 0.25', notebook)
        self.assertIn('"hype_density": 0.20', notebook)
        self.assertIn('"news_attention": 0.15', notebook)
        self.assertIn('"insider_divergence": 0.05', notebook)
        self.assertIn('_clamp(F.abs("sentiment_level"))', notebook)
        self.assertIn('_clamp(F.abs("sentiment_velocity_z") / F.lit(3.0))', notebook)
        self.assertIn('F.tanh(F.col("news_volume_z_30d") / F.lit(2.0))', notebook)
        self.assertIn('_clamp(-F.col("insider_net_buy_ratio_90d"))', notebook)
        self.assertIn('F.when(value.isNull()', notebook)

    def test_warehouse_contract_is_standalone_transactional_and_validated(self):
        warehouse = (
            ROOT / "fabric" / "warehouse" / "metrics" / "15_narrative_features.sql"
        ).read_text(encoding="utf-8")
        promotion = (
            ROOT / "fabric" / "warehouse" / "metrics" / "16_promote_narrative_snapshot.sql"
        ).read_text(encoding="utf-8")

        for value in [
            "CREATE TABLE dbo.fact_narrative_features",
            "CREATE TABLE dbo.fact_narrative_intensity",
            "CREATE OR ALTER VIEW dbo.v_narrative_intensity",
            "coverage_reasons_json",
            "evidence_document_ids_json",
            "input_generation",
            "extraction_generation",
        ]:
            with self.subTest(value=value):
                self.assertIn(value, warehouse)

        for value in [
            "CREATE OR ALTER PROCEDURE dbo.usp_promote_narrative_snapshot",
            "auspex_bronze.dbo.narrative_snapshot_manifest",
            "BEGIN TRANSACTION",
            "COMMIT TRANSACTION",
            "ROLLBACK TRANSACTION",
            "DELETE FROM dbo.fact_narrative_features",
            "DELETE FROM dbo.fact_narrative_intensity",
            "coverage_status = 'READY'",
            "mgmt_reality_gap IS NOT NULL",
            "revision_dispersion_z IS NOT NULL",
            "options_skew IS NOT NULL",
            "coverage_reasons_json IS NULL",
        ]:
            with self.subTest(value=value):
                self.assertIn(value, promotion)

        self.assertIn(
            "FROM auspex_bronze.dbo.fact_narrative_features\n        WHERE extraction_generation = @generation;",
            promotion,
        )
        self.assertIn(
            "FROM auspex_bronze.dbo.fact_narrative_intensity\n        WHERE extraction_generation = @generation\n          AND date_sk =",
            promotion,
        )
        self.assertIn("<> @expected_feature_count", promotion)
        self.assertIn("<> @expected_intensity_count", promotion)
        self.assertIn("WHERE f.extraction_generation = @generation", promotion)
        self.assertIn("WHERE i.extraction_generation = @generation", promotion)

    def test_prompt_notebook_warehouse_routes_and_infrastructure_exist(self):
        prompt = ROOT / "prompts" / "narrative" / "e21_v1.txt"
        notebook = ROOT / "fabric" / "nb_11_narrative_intensity.Notebook" / "notebook-content.py"
        warehouse = ROOT / "fabric" / "warehouse" / "metrics" / "15_narrative_features.sql"
        promotion = ROOT / "fabric" / "warehouse" / "metrics" / "16_promote_narrative_snapshot.sql"
        warehouse_runner = ROOT / "scripts" / "deploy_warehouse_schema.py"
        fabric_deployer = ROOT / "scripts" / "deploy_fabric_items.py"

        for path in [prompt, notebook, warehouse, promotion, warehouse_runner, fabric_deployer]:
            self.assertTrue(path.exists(), path)

        prompt_text = prompt.read_text(encoding="utf-8")
        notebook_text = notebook.read_text(encoding="utf-8")
        warehouse_text = warehouse.read_text(encoding="utf-8")
        promotion_text = promotion.read_text(encoding="utf-8")
        warehouse_runner_text = warehouse_runner.read_text(encoding="utf-8")
        fabric_deployer_text = fabric_deployer.read_text(encoding="utf-8")
        function_app = (ROOT / "connectors" / "function_app.py").read_text(encoding="utf-8")
        cosmos = (ROOT / "infra" / "modules" / "cosmos.bicep").read_text(encoding="utf-8")

        self.assertIn("evidence_indices", prompt_text)
        self.assertIn("CREATE TABLE IF NOT EXISTS fact_narrative_features", notebook_text)
        self.assertIn("CREATE TABLE IF NOT EXISTS fact_narrative_intensity", notebook_text)
        self.assertIn("coverage_status", notebook_text)
        self.assertIn("LOOKBACK_DAYS = 90", notebook_text)
        self.assertIn("NARRATIVE_DOCUMENTS_PER_SECURITY = 3", notebook_text)
        self.assertIn('ACTIVE_UNIVERSE_PATH = "Files/config/alpha_vantage_universe.json"', notebook_text)
        self.assertIn('F.upper(F.col("symbol")).isin(active_symbols)', notebook_text)
        self.assertIn('Window.partitionBy("security_sk")', notebook_text)
        self.assertIn(
            'F.col("revision_row_number") <= NARRATIVE_DOCUMENTS_PER_SECURITY',
            notebook_text,
        )
        self.assertIn("PROMPT_SHA256", notebook_text)
        self.assertIn("knowledge_date", notebook_text)
        self.assertIn("fact_narrative_features", warehouse_text)
        self.assertIn("fact_narrative_intensity", warehouse_text)
        self.assertIn("BEGIN TRANSACTION", promotion_text)
        self.assertIn('WAREHOUSE / "metrics" / "04_base_metrics.sql"', warehouse_runner_text)
        self.assertIn('WAREHOUSE / "05_promote_lakehouse_snapshot.sql"', warehouse_runner_text)
        self.assertIn('WAREHOUSE / "metrics" / "15_narrative_features.sql"', warehouse_runner_text)
        self.assertIn('WAREHOUSE / "metrics" / "16_promote_narrative_snapshot.sql"', warehouse_runner_text)
        self.assertIn('FABRIC_ROOT.glob("*.Notebook")', fabric_deployer_text)
        self.assertIn('route="score_narrative_features"', function_app)
        self.assertIn('route="publish_narrative_features"', function_app)
        self.assertLess(
            function_app.index("return SentimentService"),
            function_app.index("def _narrative_prompt_path"),
        )
        self.assertIn("narrative_feature_cache", cosmos)

    def test_daily_features_consume_only_completed_pit_safe_narrative_rows(self):
        metrics_notebook = notebook_code("nb_04_metrics")
        warehouse = (
            ROOT / "fabric" / "warehouse" / "metrics" / "04_base_metrics.sql"
        ).read_text(encoding="utf-8")
        promotion = (
            ROOT / "fabric" / "warehouse" / "05_promote_lakehouse_snapshot.sql"
        ).read_text(encoding="utf-8")

        for value in [
            'spark.table("narrative_snapshot_manifest")',
            'F.col("status") == F.lit("completed")',
            'Window.partitionBy("as_of_date").orderBy(',
            'F.col("completed_at").desc()',
            'F.col("generation").desc()',
            'F.col("i.coverage_status") != F.lit("WITHHELD")',
            'F.col("i.knowledge_date") <= F.col("m.as_of_date")',
            "narrative_feature_differences",
            "deleted_narrative_feature_dates",
            'feature_calendar_dates = (',
            'F.col("is_trading_day").alias("existing_trading_day")',
            'F.coalesce(F.col("existing_trading_day"), F.lit(False))',
            '_merge_all("dim_date", feature_date_df',
            "narrative_coverage_status",
            "narrative_coverage_reasons_json",
        ]:
            with self.subTest(value=value):
                self.assertIn(value, metrics_notebook)

        for value in ["narrative_coverage_status", "narrative_coverage_reasons_json"]:
            self.assertIn(value, warehouse)
            self.assertIn(value, promotion)

if __name__ == "__main__":
    unittest.main()