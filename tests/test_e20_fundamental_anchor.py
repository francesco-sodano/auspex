from datetime import date, timedelta
import hashlib
from pathlib import Path
import re
import unittest

from engine.fundamental_anchor import AnchorObservation, build_anchors
from tests.fabric_notebook import notebook_code


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = ROOT / "fabric" / "warehouse"


def observation(
    security_sk: int,
    ev_sales: float | None,
    *,
    sector: str = "Technology",
    ev_ebitda: float | None = None,
    p_fcf: float | None = None,
    growth: float | None = 0.1,
    gross_margin: float | None = 0.5,
    profit_margin: float | None = 0.2,
    leverage: float | None = 1.0,
    fcf_yield: float | None = 0.04,
    cash_burn: bool | None = False,
    knowledge_date: date = date(2026, 7, 20),
) -> AnchorObservation:
    return AnchorObservation(
        security_sk=security_sk,
        as_of=date(2026, 7, 21),
        sector=sector,
        ev_sales=ev_sales,
        ev_ebitda=ev_ebitda,
        p_fcf=p_fcf,
        rev_growth_yoy=growth,
        gross_margin=gross_margin,
        profit_margin=profit_margin,
        net_debt_to_ebitda=leverage,
        fcf_yield=fcf_yield,
        cash_burn_flag=cash_burn,
        event_date=date(2026, 6, 30),
        knowledge_date=knowledge_date,
    )


class FundamentalAnchorEngineTests(unittest.TestCase):
    def test_future_knowledge_is_excluded(self):
        rows = [
            observation(1, 4.0),
            observation(2, 8.0, knowledge_date=date(2026, 7, 22)),
        ]

        results = build_anchors(rows)

        self.assertEqual([result.security_sk for result in results], [1])

    def test_sparse_sector_uses_percentile_fallback_with_correct_sign(self):
        results = build_anchors([
            observation(1, 2.0),
            observation(2, 4.0),
            observation(3, 8.0),
        ])
        by_security = {result.security_sk: result for result in results}

        self.assertTrue(all(result.anchor_method == "percentile" for result in results))
        self.assertTrue(all(result.n_peers == 3 for result in results))
        self.assertLess(by_security[1].fundamental_anchor_z, 0)
        self.assertAlmostEqual(by_security[2].fundamental_anchor_z, 0.0, places=7)
        self.assertGreater(by_security[3].fundamental_anchor_z, 0)
        self.assertAlmostEqual(by_security[2].expected_ev_sales, 4.0, places=7)
        self.assertAlmostEqual(by_security[2].residual_evs, 0.0, places=7)

    def test_singleton_sector_is_unanchorable_not_neutral(self):
        result = build_anchors([observation(1, 4.0)])[0]

        self.assertEqual(result.anchor_method, "unanchorable")
        self.assertEqual(result.n_peers, 1)
        self.assertIsNone(result.anchor_residual)
        self.assertIsNone(result.fundamental_anchor_z)
        self.assertIn("insufficient_peers", result.imputed_flags)

    def test_tied_percentile_peers_receive_the_same_neutral_score(self):
        results = build_anchors([observation(1, 4.0), observation(2, 4.0)])

        self.assertTrue(all(result.anchor_method == "percentile" for result in results))
        self.assertTrue(all(result.anchor_residual == 0 for result in results))
        self.assertTrue(all(result.fundamental_anchor_z == 0 for result in results))

    def test_negative_secondary_denominators_are_excluded(self):
        result = build_anchors([
            observation(1, 4.0, ev_ebitda=-2.0, p_fcf=-10.0),
            observation(2, 5.0),
        ])[0]

        self.assertIsNone(result.residual_evebitda)
        self.assertIsNone(result.residual_pfcf)
        self.assertIsNotNone(result.residual_evs)

    def test_missing_primary_anchor_is_unanchorable(self):
        result = build_anchors([
            observation(1, None, ev_ebitda=10.0, p_fcf=20.0),
        ])[0]

        self.assertEqual(result.anchor_method, "unanchorable")
        self.assertIsNone(result.anchor_residual)
        self.assertIsNone(result.fundamental_anchor_z)

    def test_huber_regression_resists_outlier_and_is_deterministic(self):
        rows = []
        for index in range(16):
            growth = index / 20
            fair_multiple = 2.0 + 4.0 * growth
            observed = fair_multiple * (8.0 if index == 15 else 1.0)
            rows.append(observation(
                index + 1,
                observed,
                growth=growth,
                gross_margin=0.30 + (index % 3) / 20,
                profit_margin=0.05 + ((index * 3) % 7) / 50,
                leverage=0.5 + ((index * index) % 11) / 5,
                fcf_yield=0.01 + ((index * 5) % 13) / 200,
                cash_burn=index % 4 == 0,
            ))

        first = build_anchors(rows)
        second = build_anchors(rows)
        reversed_order = build_anchors(list(reversed(rows)))
        outlier = next(result for result in first if result.security_sk == 16)

        self.assertEqual(first, second)
        self.assertEqual(first, reversed_order)
        self.assertTrue(all(result.anchor_method == "regression" for result in first))
        self.assertIsNotNone(outlier.r2_sector)
        self.assertGreater(outlier.r2_sector, 0.5)
        self.assertGreater(outlier.fundamental_anchor_z, 1.0)
        self.assertLess(outlier.expected_ev_sales, outlier.ev_sales)

    def test_primary_regression_method_is_not_downgraded_by_sparse_secondary(self):
        rows = [
            observation(
                index + 1,
                2.0 + index / 2,
                ev_ebitda=10.0 if index == 0 else None,
                growth=index / 25,
                gross_margin=0.30 + (index % 3) / 20,
                profit_margin=0.05 + ((index * 3) % 7) / 50,
                leverage=0.5 + ((index * index) % 11) / 5,
                fcf_yield=0.01 + ((index * 5) % 13) / 200,
                cash_burn=index % 4 == 0,
            )
            for index in range(16)
        ]

        results = build_anchors(rows)

        self.assertTrue(all(result.anchor_method == "regression" for result in results))
        self.assertTrue(all(result.r2_sector is not None for result in results))
        self.assertIsNotNone(results[0].residual_evebitda)
        self.assertLess(max(abs(result.anchor_residual) for result in results), 10)

    def test_saturated_eight_peer_regression_falls_back_to_percentile(self):
        rows = [
            observation(
                index + 1,
                2.0 + index,
                growth=index / 10,
                gross_margin=0.2 + index / 100,
                profit_margin=0.05 + index / 100,
                leverage=0.5 + index / 10,
                fcf_yield=0.01 + index / 100,
                cash_burn=index % 2 == 0,
            )
            for index in range(8)
        ]

        results = build_anchors(rows)

        self.assertTrue(all(result.anchor_method == "percentile" for result in results))
        self.assertTrue(all(result.r2_sector is None for result in results))


class FundamentalAnchorContractTests(unittest.TestCase):
    def test_fundamentals_preserve_shares_outstanding(self):
        notebook = notebook_code("nb_05_alpha_vantage_to_gold")
        facts = (WAREHOUSE / "04_e8_facts.sql").read_text(encoding="utf-8")
        promotion = (WAREHOUSE / "05_promote_lakehouse_snapshot.sql").read_text(encoding="utf-8")

        self.assertIn('F.element_at("parsed_payload", "SharesOutstanding")', notebook)
        self.assertIn("shares_outstanding", notebook)
        self.assertIn("shares_outstanding", facts)
        self.assertIn("shares_outstanding", promotion)
        self.assertIn('F.col("short_term_debt").isNotNull() & F.col("long_term_debt").isNotNull()', notebook)
        self.assertNotIn('F.coalesce(F.col("short_term_debt"), F.lit(0))', notebook)
        self.assertIn('F.element_at("statement", "reportedCurrency")', notebook)
        self.assertIn("FUNDAMENTALS_STATEMENT_CURRENCY_MISMATCH", notebook)
        self.assertIn("FUNDAMENTALS_OVERVIEW_CURRENCY_MISMATCH", notebook)
        self.assertIn('F.coalesce(F.col("b.fetched_at"), F.col("c.fetched_at"))', notebook)
        self.assertIn('F.col("operating_cashflow") - F.abs(F.col("capital_expenditures"))', notebook)
        self.assertIn('(F.col("market_cap") > 0)', notebook)
        self.assertIn("e8_date_candidates", notebook)
        self.assertIn("e8_date_keyed_fact_tables", notebook)
        self.assertIn('_merge_all("dim_date", e8_date_df', notebook)
        self.assertIn('spark.table("silver_prices")', notebook)
        self.assertIn("date_orphans", notebook)
        self.assertRegex(
            facts,
            re.compile(
                r"ADD shares_outstanding DECIMAL\(20,4\) NULL;.*?\nGO\n\s*IF EXISTS",
                re.DOTALL,
            ),
        )

    def test_anchor_notebook_is_pit_safe_and_materializes_model_contract(self):
        notebook = notebook_code("nb_09_fundamental_anchor")

        for value in [
            "fact_fundamental_anchor",
            "v_fundamental_anchor",
            "MIN_PEERS = 8",
            "MIN_RESIDUAL_DF = 5",
            "max_anchor_dates = 7",
            "max_anchor_dates must be between 1 and 366",
            "requested_anchor_dates",
            "F.sequence(",
            'F.col("f.knowledge_date") <= F.col("d.as_of")',
            'F.col("f.event_date") <= F.col("d.as_of")',
            'F.col("f.fundamentals_kind") == "OVERVIEW_SNAPSHOT"',
            'F.col("p.knowledge_date") <= F.col("d.as_of")',
            'F.col("s.valid_from") <= F.col("d.as_of")',
            'F.col("d.as_of") < F.col("s.valid_to")',
            "sector_overlap_count",
            "overview_snapshot",
            "statement_revision_window",
            "statement_quarter_number",
            'F.col("ttm_quarters") == 4',
            'F.col("ttm_currency_count") == 1',
            'F.countDistinct("statement_currency")',
            'F.sum(F.abs(F.col("capital_expenditures")))',
            'F.col("ttm_operating_cashflow") - F.col("ttm_capex_outflow")',
            'F.col("total_debt").isNotNull()',
            'F.col("cash_and_equivalents").isNotNull()',
            "currency_coherent",
            'model_panel = panel.filter(F.col("overview_knowledge_date").isNotNull())',
            'F.lit(11).cast(IntegerType())',
            "e20_fundamental_anchor",
            "anchor_method",
            "model_version",
            "e20_v2",
            "fundamental_anchor_snapshot_manifest",
            "E20 persisted snapshot validation failed",
            'DeltaTable.forName(spark, "fact_fundamental_anchor").delete()',
            'DeltaTable.forName(spark, "fundamental_anchor_snapshot_manifest").delete()',
            "E20 validation failed",
        ]:
            self.assertIn(value, notebook)

        engine = (ROOT / "engine" / "fundamental_anchor.py").read_text(encoding="utf-8")
        resource = (
            ROOT / "fabric" / "nb_09_fundamental_anchor.Notebook"
            / "builtin" / "fundamental_anchor.py"
        ).read_text(encoding="utf-8")
        self.assertEqual(resource.splitlines(), engine.splitlines())
        self.assertEqual(
            (
                ROOT / "fabric" / "nb_09_fundamental_anchor.Notebook"
                / "builtin" / "fundamental_anchor.py"
            ).read_bytes(),
            (ROOT / "engine" / "fundamental_anchor.py").read_bytes(),
        )
        self.assertIn("_huber_fit", resource)
        self.assertIn('anchor_method="unanchorable"', resource)
        expected_hash = hashlib.sha256(engine.encode("utf-8")).hexdigest()
        self.assertIn(f'ENGINE_SHA256 = "{expected_hash}"', notebook)
        self.assertIn('ENGINE_LAKEHOUSE_PATH = "Files/config/e20/84641443bde957496881c8cce27b4c8a0dda7f2b5b94eca79b4fdd6213a9a14b.py"', notebook)
        self.assertIn("E20 engine resource hash mismatch", notebook)
        self.assertIn("'e20_fundamental_anchor' AS source_id", notebook)
        self.assertIn("'derived_model' AS source_type", notebook)
        self.assertIn("processed_date_sks", notebook)
        self.assertIn('dates.select("date_sk")', notebook)
        self.assertIn("whenNotMatchedBySourceDelete", notebook)
        self.assertNotIn("target.delete(", notebook)
        self.assertIn("a.model_version = 'e20_v2'", notebook)
        self.assertIn("PARTITION BY a.security_sk", notebook)
        self.assertIn("a.anchor_row_number = 1", notebook)
        self.assertNotIn("SELECT a.*, s.ticker", notebook)
        deployment_script = (ROOT / "scripts" / "deploy_e20_engine.ps1").read_text(encoding="utf-8")
        self.assertIn('-replace "`r`n", "`n"', deployment_script)
        self.assertIn("GetByteArrayAsync", deployment_script)
        self.assertIn("E20 engine hash mismatch", deployment_script)
        self.assertIn('foreach ($directory in @("Files/config", "Files/config/e20"))', deployment_script)

    def test_warehouse_and_metrics_consume_anchor_without_premature_score(self):
        anchor_sql = (WAREHOUSE / "metrics" / "14_fundamental_anchor.sql").read_text(encoding="utf-8")
        promotion = (WAREHOUSE / "05_promote_lakehouse_snapshot.sql").read_text(encoding="utf-8")
        metrics = notebook_code("nb_04_metrics")

        self.assertIn("dbo.fact_fundamental_anchor", anchor_sql)
        self.assertIn("dbo.v_fundamental_anchor", anchor_sql)
        self.assertIn("model_version", anchor_sql)
        self.assertIn("a.model_version = 'e20_v2'", anchor_sql)
        self.assertIn("fact_fundamental_anchor", promotion)
        self.assertIn("Warehouse fundamental-anchor PIT/model validation failed", promotion)
        self.assertIn("model_version <> 'e20_v2'", promotion)
        self.assertIn("uses_forward <> 0", promotion)
        self.assertIn("fact_fundamental_anchor", metrics)
        self.assertIn("anchor_feature_differences", metrics)
        self.assertIn("deleted_anchor_feature_dates", metrics)
        self.assertIn('F.col("f.fundamental_anchor_z").isNotNull()', metrics)
        self.assertNotIn('.withColumn("fundamental_anchor_z", F.lit(None)', metrics)
        self.assertIn('alias("fundamental_anchor")', metrics)
        self.assertIn("THEME_CONTEXT_REQUIRED", metrics)
        self.assertIn("fact_theme_opportunity_score", metrics)
        self.assertIn('fundamental_anchor_z=candidate.fundamental_anchor_z', metrics)

        reset = (
            ROOT / "fabric" / "notebooks" / "nb_reset_three_year_baseline.ipynb"
        ).read_text(encoding="utf-8")
        self.assertIn("fact_fundamental_anchor", reset)
        self.assertIn("v_fundamental_anchor", reset)
        self.assertIn('anchor_notebook = \\"nb_09_fundamental_anchor\\"', reset)
        self.assertIn('anchor_batch_days = 31', reset)
        self.assertIn('metrics_max_runs = 130', reset)
        self.assertIn('\\"max_anchor_dates\\": anchor_batch_days', reset)
        self.assertIn('while anchor_start <= anchor_end', reset)
        self.assertIn('rebuild_log[\\"anchor\\"]', reset)
        self.assertLess(
            reset.index('anchor_result = mssparkutils.notebook.run('),
            reset.index('metrics_result = mssparkutils.notebook.run('),
        )


if __name__ == "__main__":
    unittest.main()