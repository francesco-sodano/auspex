from dataclasses import fields
from datetime import date, datetime, timezone
from pathlib import Path
import unittest

from engine.thesis import (
    LEG_WEIGHTS,
    MIN_THEME_COHORT,
    MODEL_VERSION,
    WEIGHT_VERSION,
    OpportunityObservation,
    score_theme,
)


ROOT = Path(__file__).resolve().parents[1]


def observation(security_sk: int, **overrides) -> OpportunityObservation:
    values = {
        "theme_id": "ai-infrastructure",
        "security_sk": security_sk,
        "date_sk": 20260723,
        "as_of": date(2026, 7, 23),
        "candidate_source": "TRS",
        "candidate_snapshot_id": "batch-20260703",
        "candidate_snapshot_ingest_ts": datetime(2026, 7, 3, 12, tzinfo=timezone.utc),
        "membership_weight": 0.01 + security_sk / 10000,
        "news_volume_z_30d": security_sk / 20,
        "insider_net_buy_ratio_90d": security_sk / 50,
        "insider_cluster_buy_30d": security_sk % 3,
        "inst_net_flow_qoq": float(security_sk * 1000),
        "inst_new_initiations": security_sk % 4,
        "contract_award_usd_trailing_90d": float(security_sk * 10000),
        "activist_13d_flag": security_sk % 2 == 0,
        "profit_margin": 0.10 + security_sk / 1000,
        "rev_growth_yoy": 0.08 + security_sk / 1000,
        "fcf_yield": 0.02 + security_sk / 10000,
        "net_debt_to_ebitda": 2.0 - security_sk / 100,
        "fundamental_anchor_z": security_sk / 10,
        "news_count_30d": 10 + security_sk,
        "institutional_holder_count_120d": 20 + security_sk,
        "max_knowledge_date": date(2026, 7, 23),
    }
    values.update(overrides)
    return OpportunityObservation(**values)


class OpportunityScoreEngineTests(unittest.TestCase):
    def test_active_versions_and_balanced_weights_are_fixed(self):
        self.assertEqual(MODEL_VERSION, "e6b_v1")
        self.assertEqual(WEIGHT_VERSION, "e6b_balanced_v1")
        self.assertEqual(MIN_THEME_COHORT, 8)
        self.assertAlmostEqual(sum(LEG_WEIGHTS.values()), 1.0, places=12)
        self.assertEqual(
            set(LEG_WEIGHTS),
            {
                "thesis_linkage",
                "attention_acceleration",
                "smart_money",
                "fundamental_health",
                "valuation_brake",
                "crowding_positioning",
            },
        )

    def test_valuation_brake_demotes_expensive_anchor(self):
        observations = [observation(index) for index in range(1, 9)]
        observations[0] = observation(1, fundamental_anchor_z=2.0)
        observations[1] = observation(2, fundamental_anchor_z=-2.0)

        by_security = {row.security_sk: row for row in score_theme(observations, LEG_WEIGHTS)}

        self.assertLess(by_security[1].valuation_brake_z, by_security[2].valuation_brake_z)
        self.assertLess(
            by_security[1].valuation_brake_contribution,
            by_security[2].valuation_brake_contribution,
        )

    def test_missing_components_are_neutral_imputed_and_mark_partial(self):
        observations = [observation(index) for index in range(1, 9)]
        observations[0] = observation(
            1,
            fundamental_anchor_z=None,
            contract_award_usd_trailing_90d=None,
        )

        result = {row.security_sk: row for row in score_theme(observations, LEG_WEIGHTS)}[1]

        self.assertEqual(result.coverage_status, "PARTIAL")
        self.assertIsNotNone(result.opportunity_score)
        self.assertIn("missing:fundamental_anchor_z", result.coverage_reasons)
        self.assertIn("missing:contract_award_usd_trailing_90d", result.coverage_reasons)

    def test_small_theme_cohort_is_withheld(self):
        results = score_theme([observation(index) for index in range(1, 8)], LEG_WEIGHTS)

        self.assertTrue(results)
        self.assertTrue(all(row.coverage_status == "WITHHELD" for row in results))
        self.assertTrue(all(row.opportunity_score is None for row in results))
        self.assertTrue(all("theme_cohort_below_minimum" in row.coverage_reasons for row in results))

    def test_replay_is_order_independent_and_all_ties_are_neutral(self):
        observations = [
            observation(
                index,
                membership_weight=0.01,
                news_volume_z_30d=0.0,
                insider_net_buy_ratio_90d=0.0,
                insider_cluster_buy_30d=0,
                inst_net_flow_qoq=0.0,
                inst_new_initiations=0,
                contract_award_usd_trailing_90d=0.0,
                activist_13d_flag=False,
                profit_margin=0.1,
                rev_growth_yoy=0.1,
                fcf_yield=0.02,
                net_debt_to_ebitda=1.0,
                fundamental_anchor_z=0.0,
                news_count_30d=10,
                institutional_holder_count_120d=20,
            )
            for index in range(1, 9)
        ]

        first = score_theme(observations, LEG_WEIGHTS)
        replay = score_theme(list(reversed(observations)), LEG_WEIGHTS)

        self.assertEqual(first, replay)
        self.assertTrue(all(row.opportunity_score == 50.0 for row in first))

    def test_non_neutral_ties_share_the_first_percentile_rank(self):
        observations = [observation(index) for index in range(1, 9)]
        shared_fields = {
            field.name: getattr(observations[0], field.name)
            for field in fields(OpportunityObservation)
            if field.name not in {"security_sk"}
        }
        observations[1] = OpportunityObservation(security_sk=2, **shared_fields)

        by_security = {row.security_sk: row for row in score_theme(observations, LEG_WEIGHTS)}

        self.assertEqual(by_security[1].opportunity_score, by_security[2].opportunity_score)

    def test_pit_violation_is_rejected(self):
        observations = [observation(index) for index in range(1, 9)]
        observations[0] = observation(1, max_knowledge_date=date(2026, 7, 24))

        with self.assertRaisesRegex(ValueError, "knowledge_date exceeds as_of"):
            score_theme(observations, LEG_WEIGHTS)

    def test_runtime_weights_are_applied_and_hashed(self):
        observations = [observation(index) for index in range(1, 9)]
        thesis_only = {leg_name: 0.0 for leg_name in LEG_WEIGHTS}
        thesis_only["thesis_linkage"] = 1.0

        balanced = score_theme(observations, LEG_WEIGHTS)
        reweighted = score_theme(observations, thesis_only)

        self.assertNotEqual(
            balanced[0].cohort_snapshot_hash,
            reweighted[0].cohort_snapshot_hash,
        )
        self.assertTrue(all(row.smart_money_contribution == 0.0 for row in reweighted))

    def test_candidate_snapshot_identity_is_hashed(self):
        observations = [observation(index) for index in range(1, 9)]
        revised = list(observations)
        revised[0] = observation(1, candidate_snapshot_id="batch-replacement")

        initial = score_theme(observations, LEG_WEIGHTS)
        replacement = score_theme(revised, LEG_WEIGHTS)

        self.assertNotEqual(
            initial[0].cohort_snapshot_hash,
            replacement[0].cohort_snapshot_hash,
        )

    def test_invalid_runtime_weights_are_rejected(self):
        invalid = dict(LEG_WEIGHTS)
        invalid["valuation_brake"] = 0.0

        with self.assertRaisesRegex(ValueError, "sum to 1.0"):
            score_theme([observation(index) for index in range(1, 9)], invalid)

    def test_e22_premium_is_not_an_engine_ranking_input(self):
        field_names = {field.name for field in fields(OpportunityObservation)}

        self.assertNotIn("narrative_premium", field_names)
        self.assertNotIn("divergence_state", field_names)


class OpportunityScoreArtifactTests(unittest.TestCase):
    def test_documented_artifacts_exist(self):
        for path in [
            ROOT / "engine" / "thesis.py",
            ROOT / "fabric" / "warehouse" / "metrics" / "12b_opportunity_legs.sql",
            ROOT / "fabric" / "warehouse" / "metrics" / "13_opportunity_score.sql",
            ROOT / "scripts" / "deploy_e14_e6b_warehouse.py",
        ]:
            self.assertTrue(path.exists(), path)

    def test_notebook_and_warehouse_preserve_theme_grain_and_e22_boundary(self):
        notebook = (ROOT / "fabric" / "nb_04_metrics.Notebook" / "notebook-content.py").read_text(encoding="utf-8")
        legs_sql = (ROOT / "fabric" / "warehouse" / "metrics" / "12b_opportunity_legs.sql").read_text(encoding="utf-8")
        score_sql = (ROOT / "fabric" / "warehouse" / "metrics" / "13_opportunity_score.sql").read_text(encoding="utf-8")
        promotion_sql = (ROOT / "fabric" / "warehouse" / "05_promote_lakehouse_snapshot.sql").read_text(encoding="utf-8")

        for value in [
            "fact_theme_opportunity_score",
            "opportunity_score_snapshot_manifest",
            "latest_theme_snapshot_keys",
            "snapshot_batch_id",
            "snapshot_ingest_ts",
            "theme_snapshot_provenance",
            "missing_theme_snapshot_provenance",
            "theme_source_freshness",
            "score_stale_dates",
            'F.lit("TRS").alias("candidate_source")',
            "opportunity_active_weights",
            "score_theme(",
            'F.lit("THEME_CONTEXT_REQUIRED")',
            "UPDATE security_daily_features",
            "score_status = 'THEME_CONTEXT_REQUIRED'",
            "v_security_score_attribution",
            "invalid_theme_score_contract",
            "active_score_manifest_orphans",
            '"|".join(score_ids)',
        ]:
            self.assertIn(value, notebook)
        self.assertIn('condition="t.snapshot_batch_id IS NULL AND t.snapshot_ingest_ts IS NULL"', notebook)
        self.assertIn('condition="t.snapshot_ingest_ts IS NULL"', notebook)
        self.assertIn('condition="t.snapshot_batch_id IS NULL"', notebook)
        self.assertIn('"AND t.snapshot_batch_id = s.snapshot_batch_id"', notebook)
        self.assertIn('"AND t.snapshot_ingest_ts = s.snapshot_ingest_ts"', notebook)
        self.assertNotIn("NOT(t.snapshot_batch_id <=> s.snapshot_batch_id)", notebook)
        self.assertIn(
            'spark.table("security_daily_features").alias("f"),',
            notebook,
        )
        self.assertIn('        "left",\n    )\n    .select(', notebook)
        self.assertIn("LEFT JOIN security_daily_features f", notebook)
        self.assertIn("theme_score_fact_count", notebook)
        self.assertIn("invalid_score_projection_count", notebook)
        self.assertIn("thesis_linkage_contribution IS NULL", notebook)
        self.assertIn("institutional_holder_count_120d,\n           activist_13d_flag", notebook)
        self.assertNotIn("narrative_premium=candidate", notebook)

        for value in [
            "CREATE TABLE dbo.fact_theme_opportunity_score",
            "CREATE TABLE dbo.opportunity_score_snapshot_manifest",
            "CREATE OR ALTER VIEW dbo.v_opportunity_legs",
        ]:
            self.assertIn(value, legs_sql)
        for value in [
            "CREATE OR ALTER VIEW dbo.v_opportunity_score",
            "CREATE OR ALTER VIEW dbo.v_security_score_attribution",
            "LEFT JOIN dbo.security_daily_features f",
            "f.narrative_premium",
            "e6b_balanced_v1",
        ]:
            self.assertIn(value, score_sql)
        self.assertNotIn("narrative_premium_contribution", score_sql)

        for value in [
            "DELETE FROM dbo.fact_theme_opportunity_score",
            "INSERT INTO dbo.fact_theme_opportunity_score",
            "Warehouse Opportunity Score contract validation failed",
            "Warehouse Opportunity Score manifest reconciliation failed",
            "Warehouse Opportunity Score fact has no completed manifest",
            "WITHIN GROUP (ORDER BY score_id)",
            "thesis_linkage_contribution IS NULL",
            "Warehouse Opportunity Score serving projection lost score facts",
            "Warehouse Opportunity Score attribution projection lost score facts",
        ]:
            self.assertIn(value, promotion_sql)
        self.assertNotIn(
            '.merge(\n        score_frame.alias("s"),\n        "t.score_id = s.score_id",\n    )\n    .whenMatchedUpdateAll()',
            notebook,
        )

    def test_deployment_paths_pin_engine_and_scope_derived_cleanup(self):
        fabric_deploy = (ROOT / "scripts" / "deploy_e22_fabric.ps1").read_text(encoding="utf-8")
        warehouse_deploy = (ROOT / "scripts" / "deploy_e14_e6b_warehouse.py").read_text(encoding="utf-8")
        base_metrics_sql = (ROOT / "fabric" / "warehouse" / "metrics" / "04_base_metrics.sql").read_text(encoding="utf-8")
        e8_notebook = (ROOT / "fabric" / "nb_05_alpha_vantage_to_gold.Notebook" / "notebook-content.py").read_text(encoding="utf-8")

        self.assertIn(
            "Files/config/e14/c2e46ed74b73c478528b4b39177990e988f9477dbd1be91c9d756eb5b844adab.py",
            fabric_deploy,
        )
        self.assertIn("e14 = $opportunityEngine", fabric_deploy)
        self.assertIn('Deploy-Notebook -DisplayName "nb_05_alpha_vantage_to_gold"', fabric_deploy)
        self.assertIn("PREPRODUCTION_DERIVED_RESET_SQL", warehouse_deploy)
        self.assertIn('"01_dims.sql"', warehouse_deploy)
        self.assertIn('"14_fundamental_anchor.sql"', warehouse_deploy)
        self.assertIn("fact_theme_opportunity_score", warehouse_deploy)
        self.assertIn("opportunity_score_snapshot_manifest", warehouse_deploy)
        for table in [
            "fact_narrative_features", "fact_narrative_intensity",
            "narrative_snapshot_manifest", "fact_narrative_premium",
            "fact_narrative_premium_evidence", "narrative_premium_snapshot_manifest",
            "decision_log", "e22_release_audit",
        ]:
            self.assertIn(f"DELETE FROM dbo.{table};", warehouse_deploy)
        self.assertNotIn("fact_market_daily", warehouse_deploy)
        self.assertNotIn("WHEN MATCHED THEN UPDATE SET\n    metric_group", base_metrics_sql)
        self.assertIn('Window.partitionBy("theme_id").orderBy', e8_notebook)
        self.assertIn("latest_theme_batches", e8_notebook)
        self.assertIn('.saveAsTable("fact_theme_membership")', e8_notebook)


if __name__ == "__main__":
    unittest.main()