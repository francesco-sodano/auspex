import argparse
import json
import os
import re
from pathlib import Path

from mssql_python import connect


ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = ROOT / "fabric" / "warehouse"
SQL_FILES = [
    WAREHOUSE / "01_dims.sql",
    WAREHOUSE / "02_facts.sql",
    WAREHOUSE / "03_fx.sql",
    WAREHOUSE / "04_e8_facts.sql",
    WAREHOUSE / "metrics" / "04_base_metrics.sql",
    WAREHOUSE / "metrics" / "14_fundamental_anchor.sql",
    WAREHOUSE / "metrics" / "15_narrative_features.sql",
    WAREHOUSE / "metrics" / "16_promote_narrative_snapshot.sql",
    WAREHOUSE / "metrics" / "17_narrative_premium.sql",
    WAREHOUSE / "metrics" / "18_promote_narrative_premium_snapshot.sql",
    WAREHOUSE / "metrics" / "12b_opportunity_legs.sql",
    WAREHOUSE / "metrics" / "13_opportunity_score.sql",
    WAREHOUSE / "metrics" / "20_financing_risk.sql",
    WAREHOUSE / "metrics" / "21_opportunity_diagnostics.sql",
    WAREHOUSE / "metrics" / "19_metric_metadata.sql",
    WAREHOUSE / "05_promote_lakehouse_snapshot.sql",
    WAREHOUSE / "06_portfolio_dims.sql",
    WAREHOUSE / "07_portfolio_facts.sql",
    WAREHOUSE / "08_portfolio_views.sql",
    WAREHOUSE / "09_promote_portfolio_snapshot.sql",
]


def sql_batches(text):
    return [
        batch.strip()
        for batch in re.split(r"(?im)^\s*GO\s*(?:--.*)?$", text)
        if batch.strip()
    ]


def main():
    parser = argparse.ArgumentParser(description="Deploy the complete Auspex Warehouse schema")
    parser.add_argument("--server", default=os.environ.get("FABRIC_WAREHOUSE_SERVER", ""))
    parser.add_argument("--database", default=os.environ.get("FABRIC_WAREHOUSE_DATABASE", "auspex_gold"))
    args = parser.parse_args()
    if not args.server:
        parser.error("--server or FABRIC_WAREHOUSE_SERVER is required")
    connection = connect(
        f"Server={args.server};Database={args.database};"
        "Authentication=ActiveDirectoryDefault;Encrypt=yes;TrustServerCertificate=no;"
    )
    connection.autocommit = True
    try:
        cursor = connection.cursor()
        batches = 0
        for sql_file in SQL_FILES:
            for batch in sql_batches(sql_file.read_text(encoding="utf-8")):
                cursor.execute(batch)
                batches += 1
        cursor.execute("SELECT @@TRANCOUNT")
        open_transactions = int(cursor.fetchone()[0])
        if open_transactions:
            raise RuntimeError(f"Warehouse schema deployment left {open_transactions} transactions open")
        print(json.dumps({"status": "deployed", "files": len(SQL_FILES), "batches": batches}))
    finally:
        connection.close()


if __name__ == "__main__":
    main()