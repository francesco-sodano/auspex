# Auspex

Auspex is an Azure-native personal financial research assistant. It ingests public market, regulatory, macroeconomic, fund, and contract data on a daily batch cadence; builds point-in-time features in Microsoft Fabric; ranks stocks and ETFs; maintains an owner-isolated portfolio ledger; and explains advisory buy, hold, and sell suggestions with evidence.

Auspex never places trades, connects to a bank, or moves money. Its outputs are research aids, not financial advice.

## Architecture

```text
Azure Functions connectors -> OneLake bronze NDJSON
                          -> Fabric silver Delta tables
                          -> Fabric gold tables and Warehouse views
                          -> Azure AI Search + Azure OpenAI evidence
                          -> Python Functions API -> React Static Web App

Cosmos DB -> source registry, watermarks, run logs, caches, and owner-scoped ledger
Durable Functions -> daily connector/Fabric/AI/Warehouse orchestration and capacity guard
```

All Azure resources are deployed in Switzerland North where supported. Azure Static Web Apps is deployed in West Europe because Switzerland North is not available for that service. See [doc/arc42-auspex.md](doc/arc42-auspex.md) for the full architecture.

## Repository

| Path | Purpose |
| --- | --- |
| `infra/` | Subscription-scope Bicep and reusable modules |
| `connectors/` | Python Azure Functions ingestion and daily Durable orchestration |
| `fabric/` | Fabric Git items, PySpark notebooks, pipelines, and Warehouse SQL |
| `api/` | Owner-isolated Python Functions web API |
| `web/` | React and TypeScript application |
| `search/`, `agent/`, `engine/` | Evidence indexing, grounded explanations, and deterministic scoring |
| `scripts/` | Repeatable Fabric, Warehouse, RBAC, and recovery tools |
| `tests/` | Unit, contract, PIT, idempotency, isolation, and deployment tests |

## Prerequisites

- Azure subscription with permission to deploy subscription-scope Bicep and role assignments
- Microsoft Fabric tenant with an F2-capable region and permission to create a workspace
- Microsoft Entra app registration supporting personal Microsoft accounts for Static Web Apps authentication
- Azure CLI with Bicep, Azure Functions Core Tools, Python 3.12, Node.js 22, and PowerShell 7
- GitHub environment using workload identity federation when deploying through Actions

External source credentials are optional per connector, but enabled sources must have their Key Vault secret populated. SEC access requires a descriptive user agent containing a monitored contact address.

## Prepare Fabric

Fabric workspaces are not ARM resources, so create these items before the application deployment:

1. Create a Fabric workspace and record its workspace ID.
2. Create a Lakehouse named `auspex_bronze` and record its item ID.
3. Create a Warehouse named `auspex_gold` and record its SQL endpoint and database name.
4. Ensure the deployment identity is Member or Admin in the workspace. Deployment grants the ingestion Function managed identity the Contributor role.

The checked-in Fabric definitions use public placeholder bindings. `scripts/deploy_fabric_items.py` injects workspace, Lakehouse, and SEC contact values during deployment.

## Configure

For local CLI deployment, create the ignored file `infra/params/dev.local.json` or `infra/params/prod.local.json`. Supply at least:

```json
{
  "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
  "contentVersion": "1.0.0.0",
  "parameters": {
    "fabricAdminUpn": { "value": "admin@example.com" },
    "alertEmailAddress": { "value": "operations@example.com" },
    "onelakeWorkspaceId": { "value": "<workspace-guid>" },
    "onelakeLakehouseName": { "value": "<lakehouse-item-guid>" },
    "fabricWarehouseServer": { "value": "<warehouse-sql-endpoint>" },
    "fabricWarehouseDatabase": { "value": "auspex_gold" },
    "microsoftAuthClientId": { "value": "<entra-client-id>" },
    "microsoftAuthClientSecret": { "value": "<entra-client-secret>" },
    "repositoryUrl": { "value": "https://github.com/<owner>/<repository>" }
  }
}
```

Local Function examples are in `connectors/local.settings.example.json` and `api/local.settings.example.json`. Never commit local settings, parameter overrides, deployment state, tokens, or Fabric IDs.

For `.github/workflows/deploy.yml`, configure GitHub environment variables:

`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `FABRIC_ADMIN_UPN`, `FABRIC_WORKSPACE_ID`, `FABRIC_LAKEHOUSE_ID`, `FABRIC_WAREHOUSE_SERVER`, `FABRIC_WAREHOUSE_DATABASE`, `ALERT_EMAIL_ADDRESS`, `EDGAR_USER_AGENT`, and `MICROSOFT_AUTH_CLIENT_ID`.

Configure `MICROSOFT_AUTH_CLIENT_SECRET` as an environment secret.

## Deploy

The recommended path is the manual `Deploy` GitHub Actions workflow. It performs infrastructure deployment, Function packaging, Fabric workspace access, capacity resume/suspend, Fabric item and pipeline deployment, Warehouse schema deployment, Cosmos RBAC narrowing, web deployment, and endpoint verification.

For a direct infrastructure deployment:

```powershell
az deployment sub create `
  --location switzerlandnorth `
  --template-file infra/main.bicep `
  --parameters @infra/params/dev.json `
  --parameters @infra/params/dev.local.json
```

Then use the scripts invoked by `.github/workflows/deploy.yml` in the same order. All operational scripts require an explicit environment or explicit resource identifiers.

## Daily Build

The ingestion Function starts at 01:00 UTC. The Durable workflow:

1. Resumes Fabric capacity.
2. Runs only sources due for the date; required connector failure stops publication.
3. Runs the core Fabric pipeline through evidence and fundamental projections.
4. Scores and publishes immutable E21 narrative features in bounded pages.
5. Runs narrative premium and metric publication.
6. Promotes E21, E22, Gold, and portfolio snapshots to Warehouse.
7. Synchronizes Cosmos serving projections and Azure AI Search evidence.
8. Emits completion or failure telemetry and suspends capacity in `finally`.

Application Insights alerts cover build failure, missing completion by 05:00 UTC, and capacity running longer than four hours.

## Validate

```powershell
python -m unittest discover -s tests -q
az bicep build --file infra/main.bicep --stdout *> $null
npm ci --prefix web
npm run lint --prefix web
npm run build --prefix web
npm audit --prefix web --omit=dev
```

The CI workflow runs the same contracts on pull requests and pushes.

## Correctness And Security

- Every analytical fact carries `event_date` and `knowledge_date`; queries enforce `knowledge_date <= as_of`.
- Bronze identity includes source, deterministic window, and schema version; Delta and Warehouse replays converge.
- Watermarks advance only after successful bronze writes.
- Every portfolio row carries `owner_user_sk`; API repositories require owner scope for every read and mutation.
- Ledger schema v5 stores a parent event and linked categorized cost rows atomically in one Cosmos partition.
- Secrets are Key Vault references and Azure access uses managed identity or federated workload identity.
- Recommendations are deterministic policy outputs; model-generated text may explain evidence but cannot create trades or alter policy decisions.

## License

MIT. See [LICENSE](LICENSE).
