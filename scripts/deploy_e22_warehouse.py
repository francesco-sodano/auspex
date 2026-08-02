"""Deploy and promote E22 Fabric Warehouse contracts with Entra authentication."""

from argparse import ArgumentParser
from pathlib import Path
import json
import os
import re

from mssql_python import connect


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SERVER = os.environ.get("FABRIC_WAREHOUSE_SERVER", "")
SQL_FILES = [
    ROOT / "fabric" / "warehouse" / "04_e8_facts.sql",
    ROOT / "fabric" / "warehouse" / "metrics" / "14_fundamental_anchor.sql",
    ROOT / "fabric" / "warehouse" / "metrics" / "04_base_metrics.sql",
    ROOT / "fabric" / "warehouse" / "metrics" / "12b_opportunity_legs.sql",
    ROOT / "fabric" / "warehouse" / "metrics" / "13_opportunity_score.sql",
    ROOT / "fabric" / "warehouse" / "metrics" / "17_narrative_premium.sql",
    ROOT / "fabric" / "warehouse" / "metrics" / "18_promote_narrative_premium_snapshot.sql",
]
GOLD_PROMOTION_SQL = ROOT / "fabric" / "warehouse" / "05_promote_lakehouse_snapshot.sql"
PREPRODUCTION_DERIVED_RESET_SQL = """
IF OBJECT_ID('dbo.fact_fundamental_anchor', 'U') IS NOT NULL
    DELETE FROM dbo.fact_fundamental_anchor WHERE model_version <> 'e20_v2';
IF OBJECT_ID('dbo.fact_narrative_premium', 'U') IS NOT NULL
    DELETE FROM dbo.fact_narrative_premium WHERE model_version <> 'e22_v4';
IF OBJECT_ID('dbo.decision_log', 'U') IS NOT NULL
    DELETE FROM dbo.decision_log
    WHERE decision_type = 'NARRATIVE_PREMIUM' AND model_version <> 'e22_v4';
IF OBJECT_ID('dbo.fact_narrative_premium_evidence', 'U') IS NOT NULL
    DELETE FROM dbo.fact_narrative_premium_evidence WHERE model_version <> 'e22_v4';
IF OBJECT_ID('dbo.fact_theme_opportunity_score', 'U') IS NOT NULL
    DELETE FROM dbo.fact_theme_opportunity_score
    WHERE model_version <> 'e6b_v1' OR weight_version <> 'e6b_balanced_v1';
IF OBJECT_ID('dbo.opportunity_score_snapshot_manifest', 'U') IS NOT NULL
    DELETE FROM dbo.opportunity_score_snapshot_manifest
    WHERE model_version <> 'e6b_v1' OR weight_version <> 'e6b_balanced_v1';
IF OBJECT_ID('dbo.e22_release_audit', 'U') IS NOT NULL
    DELETE FROM dbo.e22_release_audit;
IF OBJECT_ID('dbo.gold_promotion_audit', 'U') IS NOT NULL
    DELETE FROM dbo.gold_promotion_audit;
"""


def sql_batches(text: str) -> list[str]:
    return [
        batch.strip()
        for batch in re.split(r"(?im)^\s*GO\s*(?:--.*)?$", text)
        if batch.strip()
    ]


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--database", default="auspex_gold")
    parser.add_argument("--as-of")
    parser.add_argument("--skip-deploy", action="store_true")
    parser.add_argument("--deploy-only", action="store_true")
    parser.add_argument("--gold-promotion-run-id")
    args = parser.parse_args()
    if not args.server:
        parser.error("--server or FABRIC_WAREHOUSE_SERVER is required")
    if args.deploy_only and args.skip_deploy:
        parser.error("--deploy-only and --skip-deploy cannot be combined")
    if not args.deploy_only and not args.as_of:
        parser.error("--as-of is required unless --deploy-only is used")
    if args.deploy_only and args.gold_promotion_run_id:
        parser.error("--gold-promotion-run-id cannot be combined with --deploy-only")

    connection_string = (
        f"Server={args.server};Database={args.database};"
        "Authentication=ActiveDirectoryDefault;Encrypt=yes;TrustServerCertificate=no;"
    )
    connection = connect(connection_string)
    connection.autocommit = True
    try:
        cursor = connection.cursor()
        deployment_files = [
            *SQL_FILES,
            *([GOLD_PROMOTION_SQL] if args.gold_promotion_run_id else []),
        ]
        if not args.skip_deploy:
            for sql_file in deployment_files:
                for batch in sql_batches(sql_file.read_text(encoding="utf-8")):
                    cursor.execute(batch)
        if args.deploy_only:
            print(json.dumps({
                "status": "deployed",
                "files": [str(path) for path in deployment_files],
            }))
            return

        cursor.execute(PREPRODUCTION_DERIVED_RESET_SQL)

        if args.gold_promotion_run_id:
            cursor.execute(
                """
                SELECT TOP 1 fingerprint
                FROM auspex_bronze.dbo.narrative_premium_snapshot_manifest
                WHERE status = 'completed' AND as_of_date = ?
                ORDER BY completed_at DESC, generation DESC
                """,
                (args.as_of,),
            )
            fingerprint_row = cursor.fetchone()
            if fingerprint_row is None:
                raise RuntimeError("No completed E22 manifest exists for release")
            cursor.execute(
                """
                EXEC dbo.usp_promote_e22_release
                    @as_of_date = ?,
                    @release_run_id = ?,
                    @expected_fingerprint = ?
                """,
                (args.as_of, args.gold_promotion_run_id, fingerprint_row[0]),
            )
        else:
            cursor.execute(
                "EXEC dbo.usp_promote_narrative_premium_snapshot @as_of_date = ?",
                (args.as_of,),
            )
        promotion_row = cursor.fetchone()
        promotion_columns = [description[0] for description in cursor.description]
        result = dict(zip(promotion_columns, promotion_row))
        print(json.dumps(result, default=str, sort_keys=True))
    finally:
        connection.close()


if __name__ == "__main__":
    main()