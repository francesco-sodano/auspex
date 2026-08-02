#!/usr/bin/env python3
"""Seed the Cosmos DB sources registry with all v1 source definitions.

Usage:
    COSMOS_ENDPOINT=https://<account>.documents.azure.com:443/ python -m connectors.scripts.seed_sources

Requires DefaultAzureCredential — run `az login` locally or use managed identity in Azure.
The upsert is idempotent: safe to run multiple times.
"""
import json
import os
import sys
from pathlib import Path

from azure.cosmos import CosmosClient
from azure.identity import DefaultAzureCredential


def main() -> None:
    endpoint = os.environ.get("COSMOS_ENDPOINT")
    if not endpoint:
        print("ERROR: COSMOS_ENDPOINT environment variable is required.", file=sys.stderr)
        sys.exit(1)

    seed_file = Path(__file__).parent.parent / "shared" / "sources_seed.json"
    sources = json.loads(seed_file.read_text(encoding="utf-8"))

    container = (
        CosmosClient(endpoint, DefaultAzureCredential())
        .get_database_client("auspex")
        .get_container_client("sources")
    )

    for source in sources:
        container.upsert_item(source)
        status = "enabled" if source["enabled"] else "disabled"
        print(f"  seeded {source['source_id']} ({status})")

    print(f"\nDone — {len(sources)} sources seeded into {endpoint}")


if __name__ == "__main__":
    main()
