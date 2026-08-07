# Auspex

Auspex is an Azure-native personal financial research assistant. It ingests public market, regulatory, macroeconomic, fund, and contract data on a daily batch cadence; builds point-in-time features in Microsoft Fabric; ranks stocks and ETFs; maintains an owner-isolated portfolio ledger; and explains advisory buy, hold, and sell suggestions with evidence.

Auspex never places trades, connects to a bank, or moves money. Its outputs are research aids, not financial advice.

This repository is a deployable MVP, not a production-approved financial service. The current engineering and legal-control assessment is in [doc/compliance-mvp.md](doc/compliance-mvp.md). It identifies controls present in the code, obligations owned by each deploying organization, and production gates. It is not a certification or legal opinion.

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

All Azure resources are deployed in Switzerland North where supported. Azure Static Web Apps is deployed in West Europe because Switzerland North is not available for that service. See [doc/arc42-auspex.md](doc/arc42-auspex.md) for the full architecture, exact six-leg Opportunity Score method, classification provenance, recommendation policy, audit controls, and known model limitations.

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
- Alpha Vantage and Finnhub API keys, plus an SEC user agent containing an operator-monitored contact address

FMP is disabled and its key is optional. Yahoo price fallback is unofficial and disabled.

## New Subscription Installation

The installation has two phases because Fabric capacity is an Azure resource while Fabric workspace items are tenant resources.

### 1. Bootstrap Fabric capacity

Sign in to the target subscription, register the Fabric resource provider if required, and create the data resource group and F2 capacity:

```powershell
az login
az account set --subscription <subscription-id>
az provider register --namespace Microsoft.Fabric --wait
az deployment sub create `
  --name auspex-dev-fabric-bootstrap `
  --location switzerlandnorth `
  --template-file infra/bootstrap-fabric.bicep `
  --parameters env=dev fabricAdminUpn=admin@example.com
```

The bootstrap is idempotent. The full deployment later adopts the same deterministic resource names and grants the ingestion identity capacity RBAC.

### 2. Create Fabric tenant items

1. Create a Fabric workspace and record its workspace ID.
2. Assign the workspace to `auspexdevfab` or `auspexprodfab`.
3. Create a Lakehouse named `auspex_bronze` and record its item ID.
4. Create a Warehouse named `auspex_gold` and record its SQL endpoint and database name.
5. Make the GitHub deployment identity Member or Admin in the workspace.

The checked-in Fabric definitions use public placeholder bindings. `scripts/deploy_fabric_items.py` injects workspace, Lakehouse, and SEC contact values during deployment.

### 3. Create application identity

Create a Microsoft identity platform app registration that accepts personal Microsoft accounts. Create a client secret and configure the Static Web Apps authentication redirect URI for the deployed hostname. The client ID and secret are deployment inputs; no identity binding is tracked in Git.

### 4. Configure GitHub environments

Create `dev` and/or `prod` GitHub environments. Configure workload identity federation for the repository and environment. The deployment principal needs subscription-scope resource deployment and role-assignment permission, and Fabric workspace access.

Environment variables:

`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `FABRIC_ADMIN_UPN`, `FABRIC_WORKSPACE_ID`, `FABRIC_LAKEHOUSE_ID`, `FABRIC_WAREHOUSE_SERVER`, `FABRIC_WAREHOUSE_DATABASE`, `ALERT_EMAIL_ADDRESS`, `EDGAR_USER_AGENT`, and `MICROSOFT_AUTH_CLIENT_ID`.

Environment secrets:

`MICROSOFT_AUTH_CLIENT_SECRET`, `ALPHAVANTAGE_API_KEY`, and `FINNHUB_API_KEY`.

The deployment writes enabled-source credentials into the private Key Vault through secure Bicep parameters. Source registry rows are idempotently initialized from `connectors/shared/sources_seed.json` on first connector execution.

### 5. Deploy

Run the manual `Deploy` workflow and choose the target environment. It performs the supported installation sequence:

1. infrastructure and managed identities;
2. Function packaging and deployment;
3. Fabric workspace access, notebook/ontology deployment, graph activation, and pipeline deployment;
4. complete Warehouse schema deployment;
5. Cosmos data-plane RBAC narrowing;
6. frontend lint, build, and Static Web Apps deployment;
7. Function and web endpoint verification;
8. Fabric capacity suspension, including failure paths.

Release-era E7/E14/E20/E21/E22 deployers are not part of the installation contract. The supported deployers are `deploy_fabric_items.py`, `deploy_fabric_pipeline.py`, `deploy_warehouse_schema.py`, and the GitHub workflow.

## Local Configuration

For local CLI deployment, create the ignored file `infra/params/dev.local.json` or `infra/params/prod.local.json`. Supply at least:

```json
{
  "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
  "contentVersion": "1.0.0.0",
  "parameters": {
    "fabricAdminUpn": { "value": "admin@example.com" },
    "alertEmailAddress": { "value": "operations@example.com" },
    "edgarUserAgent": { "value": "Auspex/1.0 operations@example.com" },
    "alphaVantageApiKey": { "value": "<alpha-vantage-key>" },
    "finnhubApiKey": { "value": "<finnhub-key>" },
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

For a direct full infrastructure deployment after the Fabric tenant items exist:

```powershell
az deployment sub create `
  --location switzerlandnorth `
  --template-file infra/main.bicep `
  --parameters @infra/params/dev.json `
  --parameters @infra/params/dev.local.json
```

The GitHub workflow remains the supported end-to-end path because it also packages code, deploys Fabric and Warehouse artifacts, narrows Cosmos roles, and publishes the web application.

## Daily Build

The ingestion Function starts at 01:00 UTC and retries a failed UTC-dated instance at 04:00 and 07:00. The Durable workflow:

1. Resumes Fabric capacity.
2. Runs only sources due for the date; required connector failure stops publication while the optional SEC/LLM classifier reports degraded coverage.
3. Runs the ordered core notebooks through Fabric's managed-identity Job Scheduler API.
4. Scores and publishes immutable E21 narrative features in bounded pages.
5. Runs the ordered narrative-premium, metric, and serving notebooks through the Job Scheduler API.
6. Promotes E21, E22, Gold, and portfolio snapshots to Warehouse.
7. Synchronizes Cosmos serving projections and Azure AI Search evidence.
8. Emits completion or failure telemetry and explicitly suspends capacity on both paths.

Application Insights alerts cover build failure, missing completion by 05:00 UTC, and capacity running longer than four hours.

Each timer attempt receives a unique UTC run namespace so the 04:00 and 07:00
recovery windows cannot collide with earlier Cosmos run-log entries. Bronze batch
IDs remain deterministic, allowing later attempts to skip already-landed pages
without rewriting data. Date-driven connectors terminate covered windows before
pagination or provider calls, SEC Company Facts `404` responses are retained as
explicit sparse coverage, and completed activity results are replayed from the run
log if Durable redelivers an activity.

## Validate

```powershell
python -m unittest discover -s tests -q
az bicep build --file infra/main.bicep --stdout *> $null
az bicep build --file infra/bootstrap-fabric.bicep --stdout *> $null
npm ci --prefix web
npm run lint --prefix web
npm run build --prefix web
npm audit --prefix web --omit=dev
```

The CI workflow runs the same contracts on pull requests and pushes.

After the first deployment, invoke or wait for one daily build and verify a completed run, zero required-source failures, completed E21/E22 manifests, reconciled Warehouse promotion, synchronized serving projections, and current portfolio/theme coverage before admitting pilot users.

## Correctness And Security

- Every analytical fact carries `event_date` and `knowledge_date`; queries enforce `knowledge_date <= as_of`.
- Bronze identity includes source, deterministic window, and schema version; Delta and Warehouse replays converge.
- Watermarks advance only after successful bronze writes.
- Every portfolio row carries `owner_user_sk`; API repositories require owner scope for every read and mutation.
- Ledger schema v5 stores a parent event and linked categorized cost rows atomically in one Cosmos partition.
- Secrets are Key Vault references and Azure access uses managed identity or federated workload identity.
- Recommendations are deterministic policy outputs; model-generated text may explain evidence but cannot create trades or alter policy decisions.
- Generated explanations and discussion turns are visibly disclosed and machine-readable in the DOM; the deterministic recommendation itself is not mislabeled as generated content.
- Personalized suggestions are an MVP feature, not a completed MiFID II or FinSA suitability journey. A regulated or commercial deployment must satisfy the gates in the compliance assessment.

## License

MIT. See [LICENSE](LICENSE).
