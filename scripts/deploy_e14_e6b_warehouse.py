"""Deploy and promote E14/E6b Fabric Warehouse contracts with Entra authentication."""

from argparse import ArgumentParser
import json
import os
from pathlib import Path
import re

from mssql_python import connect


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SERVER = os.environ.get("FABRIC_WAREHOUSE_SERVER", "")
SQL_FILES = [
    ROOT / "fabric" / "warehouse" / "01_dims.sql",
    ROOT / "fabric" / "warehouse" / "04_e8_facts.sql",
    ROOT / "fabric" / "warehouse" / "metrics" / "04_base_metrics.sql",
    ROOT / "fabric" / "warehouse" / "metrics" / "12b_opportunity_legs.sql",
    ROOT / "fabric" / "warehouse" / "metrics" / "13_opportunity_score.sql",
    ROOT / "fabric" / "warehouse" / "metrics" / "14_fundamental_anchor.sql",
]
GOLD_PROMOTION_SQL = ROOT / "fabric" / "warehouse" / "05_promote_lakehouse_snapshot.sql"
PREPRODUCTION_DERIVED_RESET_SQL = """
IF OBJECT_ID('dbo.fact_narrative_features', 'U') IS NOT NULL
    DELETE FROM dbo.fact_narrative_features;
IF OBJECT_ID('dbo.fact_narrative_intensity', 'U') IS NOT NULL
    DELETE FROM dbo.fact_narrative_intensity;
IF OBJECT_ID('dbo.narrative_snapshot_manifest', 'U') IS NOT NULL
    DELETE FROM dbo.narrative_snapshot_manifest;
IF OBJECT_ID('dbo.fact_narrative_premium', 'U') IS NOT NULL
    DELETE FROM dbo.fact_narrative_premium;
IF OBJECT_ID('dbo.fact_narrative_premium_evidence', 'U') IS NOT NULL
    DELETE FROM dbo.fact_narrative_premium_evidence;
IF OBJECT_ID('dbo.narrative_premium_snapshot_manifest', 'U') IS NOT NULL
    DELETE FROM dbo.narrative_premium_snapshot_manifest;
IF OBJECT_ID('dbo.decision_log', 'U') IS NOT NULL
    DELETE FROM dbo.decision_log;
IF OBJECT_ID('dbo.e22_release_audit', 'U') IS NOT NULL
    DELETE FROM dbo.e22_release_audit;
IF OBJECT_ID('dbo.fact_theme_opportunity_score', 'U') IS NOT NULL
    DELETE FROM dbo.fact_theme_opportunity_score
    WHERE model_version <> 'e6b_v1' OR weight_version <> 'e6b_balanced_v1';
IF OBJECT_ID('dbo.opportunity_score_snapshot_manifest', 'U') IS NOT NULL
    DELETE FROM dbo.opportunity_score_snapshot_manifest
    WHERE model_version <> 'e6b_v1' OR weight_version <> 'e6b_balanced_v1';
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
    parser.add_argument("--deploy-only", action="store_true")
    parser.add_argument("--skip-deploy", action="store_true")
    parser.add_argument("--promotion-run-id")
    args = parser.parse_args()
    if not args.server:
        parser.error("--server or FABRIC_WAREHOUSE_SERVER is required")
    if args.deploy_only and args.skip_deploy:
        parser.error("--deploy-only and --skip-deploy cannot be combined")
    if args.deploy_only and args.promotion_run_id:
        parser.error("--promotion-run-id cannot be combined with --deploy-only")
    if not args.deploy_only and not args.promotion_run_id:
        parser.error("--promotion-run-id is required unless --deploy-only is used")

    connection_string = (
        f"Server={args.server};Database={args.database};"
        "Authentication=ActiveDirectoryDefault;Encrypt=yes;TrustServerCertificate=no;"
    )
    connection = connect(connection_string)
    connection.autocommit = True
    try:
        cursor = connection.cursor()
        deployment_files = [*SQL_FILES, GOLD_PROMOTION_SQL]
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
        cursor.execute(
            "EXEC dbo.usp_promote_lakehouse_gold @promotion_run_id = ?",
            (args.promotion_run_id,),
        )
        cursor.execute(
            """
            SELECT promotion_run_id, source_row_count, target_row_count, status
            FROM dbo.gold_promotion_audit
            WHERE promotion_run_id = ?
            """,
            (args.promotion_run_id,),
        )
        promotion_row = cursor.fetchone()
        if promotion_row is None:
            raise RuntimeError("E14/E6b Gold promotion did not write its audit row")
        promotion_columns = [description[0] for description in cursor.description]
        print(json.dumps(
            dict(zip(promotion_columns, promotion_row)),
            default=str,
            sort_keys=True,
        ))
    finally:
        connection.close()


if __name__ == "__main__":
    main()