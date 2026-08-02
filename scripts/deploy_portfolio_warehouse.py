"""Deploy and promote the owner-scoped portfolio Warehouse snapshot."""

from argparse import ArgumentParser
import json
import os
from pathlib import Path
import re

from mssql_python import connect


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SERVER = os.environ.get("FABRIC_WAREHOUSE_SERVER", "")
SQL_FILES = [
    ROOT / "fabric" / "warehouse" / "07_portfolio_facts.sql",
    ROOT / "fabric" / "warehouse" / "08_portfolio_views.sql",
    ROOT / "fabric" / "warehouse" / "09_promote_portfolio_snapshot.sql",
]
ACCOUNTING_COLUMNS = {
    "gross_amount",
    "source_currency",
    "source_amount",
    "fx_rate_to_settlement",
    "linked_transaction_id",
    "cost_category",
    "affects_cash",
}


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
    parser.add_argument("--skip-deploy", action="store_true")
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
        if not args.skip_deploy:
            for sql_file in SQL_FILES:
                for batch in sql_batches(sql_file.read_text(encoding="utf-8")):
                    cursor.execute(batch)

        cursor.execute("EXEC dbo.usp_promote_portfolio_snapshot")
        cursor.execute("""
            SELECT
                (SELECT COUNT_BIG(*) FROM dbo.fact_portfolio_transaction),
                (SELECT COUNT_BIG(*) FROM dbo.fact_portfolio_position),
                (SELECT COUNT_BIG(*) FROM dbo.fact_portfolio_valuation),
                @@TRANCOUNT
        """)
        transactions, positions, valuations, open_transactions = cursor.fetchone()
        cursor.execute("""
            SELECT name
            FROM sys.columns
            WHERE object_id = OBJECT_ID('dbo.fact_portfolio_transaction')
        """)
        columns = {row[0] for row in cursor.fetchall()}
        missing_columns = sorted(ACCOUNTING_COLUMNS - columns)
        result = {
            "status": "promoted",
            "deployed": not args.skip_deploy,
            "transactions": int(transactions),
            "positions": int(positions),
            "valuations": int(valuations),
            "open_transactions": int(open_transactions),
            "missing_accounting_columns": missing_columns,
        }
        print(json.dumps(result, sort_keys=True))
        if open_transactions != 0 or missing_columns:
            raise RuntimeError("Portfolio Warehouse validation failed")
    finally:
        connection.close()


if __name__ == "__main__":
    main()