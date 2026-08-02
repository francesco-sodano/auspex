from datetime import date
import json
import math
from statistics import fmean, stdev
from pathlib import Path
import unittest

from engine.narrative_premium import (
    PremiumObservation,
    PreviousPremiumState,
    build_narrative_premiums,
    classify_divergence,
    premium_input_snapshot_hash,
)
from tests.fabric_notebook import notebook_cells, notebook_code


AS_OF = date(2026, 7, 23)
ROOT = Path(__file__).resolve().parents[1]


def observation(
    security_sk: int,
    anchor_z: float | None,
    intensity: float | None,
    *,
    anchor_method: str = "regression",
    narrative_status: str = "READY",
    evidence_ids: tuple[str, ...] = ("doc-1",),
    component_mask: tuple[str, ...] = (
        "forward_promise_ratio",
        "hype_density",
        "news_attention",
        "sentiment_strength",
        "theme_concentration",
    ),
) -> PremiumObservation:
    return PremiumObservation(
        security_sk=security_sk,
        as_of=AS_OF,
        fundamental_anchor_z=anchor_z,
        anchor_method=anchor_method,
        narrative_intensity=intensity,
        narrative_coverage_status=narrative_status,
        narrative_available_weight=0.85,
        narrative_extraction_coverage=1.0,
        narrative_component_mask=component_mask,
        narrative_coverage_reasons=("options_skew:source_unavailable",),
        anchor_event_date=date(2026, 6, 30),
        anchor_knowledge_date=date(2026, 7, 20),
        anchor_n_peers=12,
        anchor_r2_sector=0.6,
        anchor_imputed_flags="",
        narrative_event_date=AS_OF,
        narrative_knowledge_date=AS_OF,
        evidence_document_ids=evidence_ids,
        e20_model_version="e20_v2",
        e20_generation="e20-generation",
        e20_manifest_fingerprint="f" * 64,
        e21_model_version="gpt-4o:2024-11-20",
        prompt_version="e21_narrative_v1",
        input_generation="e7-generation",
        extraction_generation="e21-generation",
        e21_manifest_fingerprint="e" * 64,
    )


class NarrativePremiumEngineTests(unittest.TestCase):
    def test_ols_attribution_reconciles_exactly_and_is_order_stable(self):
        intensities = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0]
        mean_intensity = fmean(intensities)
        intensity_stddev = stdev(intensities)
        rows = [
            observation(
                index + 1,
                0.2 + 0.75 * ((intensity - mean_intensity) / intensity_stddev),
                intensity,
                narrative_status="PARTIAL",
                evidence_ids=(f"doc-{index}",),
            )
            for index, intensity in enumerate(intensities)
        ]

        first = build_narrative_premiums(rows)
        replay = build_narrative_premiums(list(reversed(rows)))

        self.assertEqual(first, replay)
        self.assertTrue(all(result.coverage_status == "PARTIAL" for result in first))
        self.assertTrue(all(result.eligible_security_count == 8 for result in first))
        self.assertTrue(all(math.isclose(result.attribution_intercept, 0.2) for result in first))
        self.assertTrue(all(math.isclose(result.attribution_beta, 0.75) for result in first))
        self.assertTrue(all(math.isclose(result.attribution_r2, 1.0) for result in first))
        for result in first:
            reconstructed = (
                result.attribution_intercept
                + result.narrative_premium
                + result.unexplained_residual
            )
            self.assertAlmostEqual(result.fundamental_anchor_z, reconstructed, places=12)
            self.assertAlmostEqual(result.unexplained_residual, 0.0, places=12)
            self.assertEqual(
                result.evidence_pack["output"]["narrative_premium"],
                result.narrative_premium,
            )
            self.assertNotIn(
                "evidence_document_ids",
                result.evidence_pack["narrative"],
            )
            self.assertEqual(
                result.evidence_pack["narrative"]["evidence_document_count"],
                1,
            )

    def test_sparse_unanchorable_inputs_are_withheld_not_neutral(self):
        results = build_narrative_premiums([
            observation(1, None, 30.0, anchor_method="unanchorable", narrative_status="PARTIAL"),
            observation(2, None, 70.0, anchor_method="unanchorable", narrative_status="PARTIAL"),
        ])

        self.assertEqual(len(results), 2)
        self.assertTrue(all(result.coverage_status == "WITHHELD" for result in results))
        self.assertTrue(all(result.narrative_premium is None for result in results))
        self.assertTrue(all(result.divergence_state is None for result in results))
        self.assertTrue(all(
            result.evidence_pack["output"]["coverage_status"] == "WITHHELD"
            for result in results
        ))
        self.assertTrue(all(
            "fundamental_anchor:unusable" in result.coverage_reasons
            for result in results
        ))
        self.assertTrue(all(
            "attribution:insufficient_eligible_securities" in result.coverage_reasons
            for result in results
        ))

    def test_zero_intensity_variance_is_withheld(self):
        results = build_narrative_premiums([
            observation(index, index / 10, 50.0)
            for index in range(1, 9)
        ])

        self.assertTrue(all(result.coverage_status == "WITHHELD" for result in results))
        self.assertTrue(all(
            "attribution:zero_intensity_variance" in result.coverage_reasons
            for result in results
        ))

    def test_snapshot_hash_is_order_independent_and_evidence_sensitive(self):
        rows = [observation(1, 0.1, 30.0), observation(2, 0.2, 40.0)]
        replay = list(reversed(rows))
        changed = [rows[0], observation(2, 0.2, 40.0, evidence_ids=("doc-2",))]

        self.assertEqual(
            premium_input_snapshot_hash(rows),
            premium_input_snapshot_hash(replay),
        )
        self.assertNotEqual(
            premium_input_snapshot_hash(rows),
            premium_input_snapshot_hash(changed),
        )
        previous = {
            1: PreviousPremiumState(
                decision_id="a" * 64,
                as_of=date(2026, 7, 22),
                generation="e22-prior",
                narrative_premium=0.5,
                fit_context_hash="context-prior",
            )
        }
        self.assertNotEqual(
            build_narrative_premiums(rows)[0].decision_id,
            build_narrative_premiums(rows, previous_premiums=previous)[0].decision_id,
        )
        self.assertEqual(
            premium_input_snapshot_hash(rows),
            build_narrative_premiums(rows, previous_premiums=previous)[0].input_snapshot_hash,
        )

    def test_large_evidence_sets_keep_compact_pack_bounded(self):
        document_ids = tuple(f"document-{index:04d}-{'x' * 48}" for index in range(1000))
        rows = [
            observation(
                index + 1,
                index / 5,
                10.0 * (index + 1),
                evidence_ids=document_ids if index == 0 else (f"doc-{index}",),
            )
            for index in range(8)
        ]

        result = build_narrative_premiums(rows)[0]
        changed_rows = [
            observation(1, 0.0, 10.0, evidence_ids=(*document_ids[:-1], "changed-document")),
            *rows[1:],
        ]

        self.assertLess(len(json.dumps(result.evidence_pack, sort_keys=True)), 8000)
        self.assertEqual(
            result.evidence_pack["narrative"]["evidence_document_count"],
            1000,
        )
        self.assertNotEqual(
            result.input_snapshot_hash,
            premium_input_snapshot_hash(changed_rows),
        )

    def test_divergence_thresholds_have_total_deterministic_classification(self):
        self.assertEqual(
            classify_divergence(0.5, 0.5),
            "NARRATIVE_LED_OVEREXTENSION",
        )
        self.assertEqual(
            classify_divergence(-0.5, 0.5),
            "NARRATIVE_ON_STRONG_ANCHOR",
        )
        self.assertEqual(
            classify_divergence(-0.5, -0.5),
            "NARRATIVE_NEGLECTED",
        )
        self.assertEqual(classify_divergence(0.0, 0.0), "FUNDAMENTALLY_ANCHORED")
        self.assertEqual(classify_divergence(1.0, -0.5), "MIXED")

    def test_convergence_requires_previous_premium_compression(self):
        intensities = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0]
        rows = [
            observation(index + 1, index / 2, intensity)
            for index, intensity in enumerate(intensities)
        ]
        baseline = build_narrative_premiums(rows)
        baseline_context = baseline[0].fit_context_hash
        previous = {
            result.security_sk: PreviousPremiumState(
                decision_id=f"{result.security_sk:064x}",
                as_of=date(2026, 7, 22),
                generation="e22-prior",
                narrative_premium=abs(result.narrative_premium) + 0.3,
                fit_context_hash=baseline_context,
            )
            for result in baseline
        }

        results = build_narrative_premiums(rows, previous_premiums=previous)

        self.assertTrue(all(result.is_converging for result in results))
        self.assertTrue(all(
            result.evidence_pack["attribution"]["previous_premium"] is not None
            for result in results
        ))
        self.assertTrue(all(result.model_version == "e22_v4" for result in results))

    def test_nonpositive_beta_is_withheld_instead_of_inverting_states(self):
        intensities = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0]
        rows = [
            observation(index + 1, 4.5 - index, intensity)
            for index, intensity in enumerate(intensities)
        ]

        results = build_narrative_premiums(rows)

        self.assertTrue(all(result.coverage_status == "WITHHELD" for result in results))
        self.assertTrue(all(result.divergence_state is None for result in results))
        self.assertTrue(all(
            "attribution:nonpositive_beta" in result.coverage_reasons
            for result in results
        ))

    def test_zero_anchor_variance_is_withheld(self):
        results = build_narrative_premiums([
            observation(index, 0.5, 10.0 * index)
            for index in range(1, 9)
        ])

        self.assertTrue(all(result.coverage_status == "WITHHELD" for result in results))
        self.assertTrue(all(
            "attribution:zero_anchor_variance" in result.coverage_reasons
            for result in results
        ))

    def test_heterogeneous_component_masks_are_withheld(self):
        rows = [
            observation(index, index / 4, 10.0 * index)
            for index in range(1, 8)
        ] + [
            observation(
                8,
                2.0,
                80.0,
                component_mask=("sentiment_strength", "hype_density"),
            )
        ]

        results = build_narrative_premiums(rows)

        self.assertTrue(all(result.coverage_status == "WITHHELD" for result in results))
        self.assertTrue(all(
            "attribution:heterogeneous_component_mask" in result.coverage_reasons
            for result in results
        ))

    def test_convergence_is_null_when_fit_context_changes(self):
        rows = [observation(index, index / 3, 10.0 * index) for index in range(1, 9)]
        baseline = build_narrative_premiums(rows)
        previous = {
            result.security_sk: PreviousPremiumState(
                decision_id=f"{result.security_sk:064x}",
                as_of=date(2026, 7, 22),
                generation="e22-prior",
                narrative_premium=abs(result.narrative_premium) + 1.0,
                fit_context_hash="different-context",
            )
            for result in baseline
        }

        results = build_narrative_premiums(rows, previous_premiums=previous)

        self.assertTrue(all(result.is_converging is None for result in results))


class NarrativePremiumArtifactTests(unittest.TestCase):
    def test_notebook_is_canonical_hash_pinned_and_append_only(self):
        notebook = notebook_code("nb_12_narrative_premium")
        parameter_cells = [
            code
            for marker, code in notebook_cells("nb_12_narrative_premium")
            if marker == "PARAMETERS CELL"
        ]

        self.assertEqual(parameter_cells, ['as_of_date = ""'])
        for value in [
            'MODEL_VERSION = "e22_v4"',
            'ENGINE_LAKEHOUSE_PATH = "Files/config/e22/09e9532dd031ecb45e8e3591986164d763a4ebbec3da43246c8ca8040aaa02ea.py"',
            'ENGINE_SHA256 = "09e9532dd031ecb45e8e3591986164d763a4ebbec3da43246c8ca8040aaa02ea"',
            "_portable_snapshot_fingerprint",
            "build_narrative_premiums",
            "PreviousPremiumState",
            'F.col("status") == F.lit("completed")',
            "CREATE TABLE IF NOT EXISTS fact_narrative_premium",
            "CREATE TABLE IF NOT EXISTS decision_log",
            "CREATE TABLE IF NOT EXISTS fact_narrative_premium_evidence",
            "CREATE TABLE IF NOT EXISTS narrative_premium_snapshot_manifest",
            ".whenNotMatchedInsertAll()",
            "E22 immutable replay conflict",
            "reconciliation_violations",
            "mssparkutils.notebook.exit(run_summary_json)",
            "upstream_manifest_changed",
            'F.col("model_version") != F.lit(MODEL_VERSION)',
            'DROP TABLE IF EXISTS {obsolete_projection}',
        ]:
            with self.subTest(value=value):
                self.assertIn(value, notebook)

        self.assertNotIn("whenMatchedUpdateAll()\n    .whenNotMatchedInsertAll()\n    .execute()\n)\n(\n    DeltaTable.forName(spark, \"decision_log\")", notebook)
        self.assertIn('F.col("knowledge_date") > F.lit(parsed_as_of_date)', notebook)
        self.assertIn('F.col("status") == F.lit("completed")', notebook)
        self.assertNotIn('.unionByName(_manifest_frame("completed"', notebook)
        self.assertIn('condition="t.status <> \'completed\'"', notebook)

    def test_notebook_platform_and_engine_artifacts_exist(self):
        for path in [
            ROOT / "engine" / "narrative_premium.py",
            ROOT / "fabric" / "nb_12_narrative_premium.Notebook" / ".platform",
            ROOT / "fabric" / "nb_12_narrative_premium.Notebook" / "notebook-content.py",
            ROOT / "fabric" / "warehouse" / "metrics" / "17_narrative_premium.sql",
            ROOT / "fabric" / "warehouse" / "metrics" / "18_promote_narrative_premium_snapshot.sql",
            ROOT / "scripts" / "deploy_e22_fabric.ps1",
            ROOT / "scripts" / "deploy_e22_warehouse.py",
            ROOT / "scripts" / "run_e22_pipeline.ps1",
        ]:
            self.assertTrue(path.exists(), path)

    def test_warehouse_and_daily_feature_contracts_preserve_e22_coverage(self):
        metrics_notebook = notebook_code("nb_04_metrics")
        base_metrics = (
            ROOT / "fabric" / "warehouse" / "metrics" / "04_base_metrics.sql"
        ).read_text(encoding="utf-8")
        shared_promotion = (
            ROOT / "fabric" / "warehouse" / "05_promote_lakehouse_snapshot.sql"
        ).read_text(encoding="utf-8")
        premium_sql = (
            ROOT / "fabric" / "warehouse" / "metrics" / "17_narrative_premium.sql"
        ).read_text(encoding="utf-8")
        promotion_sql = (
            ROOT / "fabric" / "warehouse" / "metrics" / "18_promote_narrative_premium_snapshot.sql"
        ).read_text(encoding="utf-8")

        for value in [
            'spark.table("narrative_premium_snapshot_manifest")',
            'F.col("status") == F.lit("completed")',
            'priority_as_of_date = ""',
            "parsed_priority_as_of_date = date.fromisoformat(priority_as_of_date)",
            "priority_processing_dates",
            "remaining_limit = _MAX_MARKET_SNAPSHOT_DATES_PER_RUN - int(priority_is_stale)",
            "premium_feature_differences",
            "deleted_premium_feature_dates",
            "narrative_premium_coverage_status",
            "narrative_premium_coverage_reasons_json",
            "narrative_decision_id",
            "anchor_support_z",
            "narrative_is_converging",
            "invalid_narrative_premium_contract",
            "LENGTH(narrative_decision_id) <> 64",
            'F.col("narrative_premium").isNull().alias("narrative_premium")',
        ]:
            self.assertIn(value, metrics_notebook)
        for value in [
            "narrative_premium_coverage_status",
            "narrative_premium_coverage_reasons_json",
            "narrative_decision_id",
            "anchor_support_z",
            "narrative_is_converging",
        ]:
            self.assertIn(value, base_metrics)
            self.assertIn(value, shared_promotion)
        for value in [
            "CREATE TABLE dbo.fact_narrative_premium",
            "CREATE TABLE dbo.fact_narrative_premium_evidence",
            "CREATE TABLE dbo.decision_log",
            "CREATE OR ALTER VIEW dbo.v_narrative_premium",
        ]:
            self.assertIn(value, premium_sql)
        for value in [
            "CREATE OR ALTER PROCEDURE dbo.usp_promote_narrative_premium_snapshot",
            "@emit_result BIT = 1",
            "IF @emit_result = 1",
            "BEGIN TRANSACTION",
            "ROLLBACK TRANSACTION",
            "E22 Warehouse decision log immutable conflict",
            "E22 Warehouse evidence immutable conflict",
            "E22 Lakehouse evidence hash does not reconcile",
            "E22 Warehouse evidence hash does not reconcile",
            "E22 Lakehouse snapshot fingerprint does not reconcile",
            "E22 Lakehouse fact and decision payloads do not reconcile",
            "CREATE OR ALTER PROCEDURE dbo.usp_promote_e22_release",
            "@emit_result = 0",
            "SELECT @premium_row_count = COUNT_BIG(*)",
            "E22 release daily features do not reconcile to premium facts",
            "JSON_VALUE(f.evidence_pack_json, '$.narrative.evidence_document_hash') IS NULL",
            "d.security_sk <> f.security_sk",
            "d.event_date <> f.event_date",
            "STRING_ESCAPE(e.document_id, 'json')",
            "WITHIN GROUP (ORDER BY e.evidence_ordinal)",
            "CONCAT_WS(",
            "CHAR(31)",
            "HASHBYTES(",
            "NOT EXISTS",
            "attribution reconciliation failed",
        ]:
            self.assertIn(value, promotion_sql)
        self.assertNotIn("INSERT INTO #premium_result", promotion_sql)

    def test_deployment_runners_cover_engine_notebooks_and_promotions(self):
        fabric_runner = (ROOT / "scripts" / "deploy_e22_fabric.ps1").read_text(encoding="utf-8")
        warehouse_runner = (ROOT / "scripts" / "deploy_e22_warehouse.py").read_text(encoding="utf-8")
        pipeline_runner = (ROOT / "scripts" / "run_e22_pipeline.ps1").read_text(encoding="utf-8")

        for value in [
            "Files/config/e22/09e9532dd031ecb45e8e3591986164d763a4ebbec3da43246c8ca8040aaa02ea.py",
            "Files/config/e20/84641443bde957496881c8cce27b4c8a0dda7f2b5b94eca79b4fdd6213a9a14b.py",
            'Deploy-Notebook -DisplayName "nb_09_fundamental_anchor"',
            'Deploy-Notebook -DisplayName "nb_12_narrative_premium"',
            'Deploy-Notebook -DisplayName "nb_04_metrics"',
            "Engine hash mismatch for",
            "Content-addressed target mismatch",
            "Remove-ObsoleteEngineFiles",
            'Where-Object { $_.Name -in @(".platform", "notebook-content.py") }',
        ]:
            self.assertIn(value, fabric_runner)
        for value in [
            '"17_narrative_premium.sql"',
            '"18_promote_narrative_premium_snapshot.sql"',
            '"14_fundamental_anchor.sql"',
            "usp_promote_narrative_premium_snapshot",
            '"--gold-promotion-run-id"',
            "usp_promote_e22_release",
            "PREPRODUCTION_DERIVED_RESET_SQL",
            "model_version <> 'e20_v2'",
            "model_version <> 'e22_v4'",
        ]:
            self.assertIn(value, warehouse_runner)
        for value in [
            "Assert-NoActiveJobs",
            'Invoke-Notebook "nb_09_fundamental_anchor"',
            'Invoke-Notebook "nb_11_narrative_intensity"',
            'Invoke-Notebook "nb_12_narrative_premium"',
            'Invoke-Notebook "nb_04_metrics"',
            "priority_as_of_date = $AsOfDate",
            "--gold-promotion-run-id",
            "/suspend?api-version=2023-11-01",
        ]:
            self.assertIn(value, pipeline_runner)


if __name__ == "__main__":
    unittest.main()