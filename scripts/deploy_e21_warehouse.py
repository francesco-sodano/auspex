"""Deploy and execute the E21 Fabric Warehouse contract with Entra authentication."""

from argparse import ArgumentParser
from pathlib import Path
import json
import os
import re

from mssql_python import connect


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SERVER = os.environ.get("FABRIC_WAREHOUSE_SERVER", "")
SQL_FILES = [
    ROOT / "fabric" / "warehouse" / "metrics" / "04_base_metrics.sql",
    ROOT / "fabric" / "warehouse" / "metrics" / "15_narrative_features.sql",
    ROOT / "fabric" / "warehouse" / "metrics" / "16_promote_narrative_snapshot.sql",
]
GOLD_PROMOTION_SQL = ROOT / "fabric" / "warehouse" / "05_promote_lakehouse_snapshot.sql"


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
            print(json.dumps({"status": "deployed", "files": [str(path) for path in deployment_files]}))
            return
        cursor.execute("EXEC dbo.usp_promote_narrative_snapshot @as_of_date = ?", (args.as_of,))
        cursor.execute("""
            SELECT
                (SELECT COUNT_BIG(*) FROM dbo.fact_narrative_features) AS feature_count,
                (SELECT COUNT_BIG(*) FROM dbo.fact_narrative_intensity) AS intensity_count,
                (SELECT COUNT_BIG(*) FROM dbo.fact_narrative_intensity WHERE coverage_status = 'PARTIAL') AS partial_count,
                (SELECT COUNT_BIG(*) FROM dbo.fact_narrative_intensity WHERE coverage_status = 'WITHHELD') AS withheld_count,
                (SELECT COUNT_BIG(*) FROM dbo.fact_narrative_intensity WHERE coverage_status = 'READY') AS ready_count,
                (SELECT COUNT_BIG(*) FROM dbo.fact_narrative_intensity WHERE narrative_intensity IS NOT NULL) AS scored_count
        """)
        row = cursor.fetchone()
        columns = [description[0] for description in cursor.description]
        result = dict(zip(columns, row))
        if args.gold_promotion_run_id:
            cursor.execute(
                "EXEC dbo.usp_promote_lakehouse_gold @promotion_run_id = ?",
                (args.gold_promotion_run_id,),
            )
            cursor.execute("""
                SELECT source_row_count, target_row_count, status
                FROM dbo.gold_promotion_audit
                WHERE promotion_run_id = ?
            """, (args.gold_promotion_run_id,))
            audit_row = cursor.fetchone()
            audit_columns = [description[0] for description in cursor.description]
            result["gold_promotion_run_id"] = args.gold_promotion_run_id
            result["gold_promotion"] = dict(zip(audit_columns, audit_row))
        print(json.dumps(result, sort_keys=True))
    finally:
        connection.close()


if __name__ == "__main__":
    main()