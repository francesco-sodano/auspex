import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "connectors"))

from shared.daily_build import FabricDailyBuildClient


SCHEMA_REFRESH_NOTEBOOKS = [
    {
        "notebook": "nb_13_source_history_to_silver",
        "parameters": {"end_date": "@pipeline().parameters.as_of_date"},
    },
    {
        "notebook": "nb_05_alpha_vantage_to_gold",
        "parameters": {"to_date": "@pipeline().parameters.as_of_date"},
    },
    {
        "notebook": "nb_09_fundamental_anchor",
        "parameters": {
            "from_date": "@pipeline().parameters.as_of_date",
            "to_date": "@pipeline().parameters.as_of_date",
            "max_anchor_dates": "1",
        },
    },
    {
        "notebook": "nb_04_metrics",
        "parameters": {"priority_as_of_date": "@pipeline().parameters.as_of_date"},
    },
]


def refresh_schema(client, as_of_date, poll_seconds=10):
    results = []
    for notebook in SCHEMA_REFRESH_NOTEBOOKS:
        job = client.start_notebook(as_of_date, notebook)
        while True:
            status = client.get_notebook_status(job["job_id"], job["notebook_id"])
            normalized = str(status.get("status") or "").lower()
            if normalized == "completed":
                results.append({**job, "status": "Completed"})
                break
            if normalized in {"cancelled", "deduped", "failed"}:
                reason = status.get("failure_reason") or normalized
                raise RuntimeError(f"{job['notebook_name']} failed: {reason}")
            time.sleep(poll_seconds)
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Refresh rebuildable Fabric schemas before Warehouse DDL deployment"
    )
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--subscription-id", required=True)
    parser.add_argument("--capacity-resource-group", required=True)
    parser.add_argument("--capacity-name", required=True)
    parser.add_argument("--as-of-date", default=date.today().isoformat())
    parser.add_argument("--poll-seconds", type=int, default=10)
    args = parser.parse_args()
    client = FabricDailyBuildClient(
        subscription_id=args.subscription_id,
        capacity_resource_group=args.capacity_resource_group,
        capacity_name=args.capacity_name,
        workspace_id=args.workspace_id,
    )
    print(json.dumps({
        "status": "completed",
        "as_of_date": args.as_of_date,
        "notebooks": refresh_schema(client, args.as_of_date, args.poll_seconds),
    }))


if __name__ == "__main__":
    main()