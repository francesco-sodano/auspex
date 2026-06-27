# Auspex — Operations & Deployment Guide

All manual steps required to provision and operate Auspex. No secrets are stored here — secret values are always set interactively or via CI and never committed.

---

## Subscription constraints

This subscription is governed by the **MCAPSGovDeployPolicies** initiative assigned at the tenant root management group. Two policies affect storage:

| Policy | Effect | Impact |
|--------|--------|--------|
| `StorageAccount_DisableLocalAuth_Modify` | Modify | Silently reverts `allowSharedKeyAccess` to `false` on every storage account. Cannot be exempted without tenant-level access. |
| `StorageAccount_BlobAnonymousAccess_Modify` | Modify | Silently reverts `allowBlobPublicAccess` to `false`. |

In addition, the `frsodano@microsoft.com` guest account is subject to **Microsoft corporate Conditional Access** that blocks Azure Storage data plane calls from local machines and Cloud Shell. Only the Azure Portal (server-side) bypasses this.

Consequences for deployment:
- `func azure functionapp publish` fails — it requires the Functions Core Tools/Kudu publish path that expects shared-key host storage behavior blocked by this subscription.
- Azure CLI storage data plane commands (`az storage container create`, `az storage blob upload`) fail from both local machine and Cloud Shell under the Microsoft guest account.
- The working connector deployment path is a prebuilt Linux-compatible zip deployed with `az functionapp deployment source config-zip`.

---

## Prerequisites

### Azure / GitHub access

- Azure subscription with Owner or Contributor + User Access Administrator
- Microsoft Fabric enabled on the tenant (requires tenant admin)
- GitHub repository: `francesco-sodano/auspex` with push access
- `az login` completed with an account that has subscription access

### Required tools

| Tool | Version | Install |
|------|---------|---------|
| Git | any | https://git-scm.com |
| Python | 3.12 | https://www.python.org/downloads/ |
| Azure CLI | 2.60+ | `winget install Microsoft.AzureCLI` |
| PowerShell | 7+ | `winget install Microsoft.PowerShell` |
| Bicep CLI | latest | `az bicep install` (bundled with Azure CLI) |

> Azure Functions Core Tools (`func`) is **not used** for deployment — see the Subscription constraints section above.

Verify installs:

```powershell
az --version
python --version    # expect 3.12.x
```

---

## E1 — Infrastructure

### 1. Deploy Bicep

Bicep is scoped to the subscription (creates all resource groups):

```powershell
az deployment sub create `
  --location switzerlandnorth `
  --template-file infra/main.bicep `
  --parameters @infra/params/dev.json
```

> Repeat with `infra/params/prod.json` for production.

### 2. Verify Fabric capacity from Bicep, then create Fabric workspace (manual)

Fabric capacity is an Azure resource and is provisioned by Bicep as `auspexdevfab` for dev or `auspexprodfab` for prod (SKU F2, Switzerland North). Fabric workspace, Lakehouse, Warehouse, notebooks, and pipelines are Fabric items, not ARM/Bicep resources, so they are created/synced through the Fabric portal and Fabric Git integration.

- Azure portal → confirm the Microsoft Fabric capacity exists in `auspex-dev-data` or `auspex-prod-data`
- Fabric portal → create workspace `auspex-dev` or `auspex-prod`
- Assign the workspace to the Bicep-created capacity

> The capacity name must match `FABRIC_CAPACITY_NAME` in the ingestion Function App. Bicep derives that setting as `auspex${env}fab`, so do not use hyphens. Fabric capacity is billed by the hour when running. The capacity scheduler Function handles resume/suspend automatically once the scheduler work is implemented.

### 3. Connect Fabric workspace to GitHub (one-time)

- Fabric portal (`https://app.fabric.microsoft.com`) → `auspex-dev` workspace
- Workspace settings → **Git integration** → Connect to GitHub
  - Organisation: `francesco-sodano`
  - Repository: `auspex`
  - Branch: `main`
  - Folder: `/fabric`

---

## E2 — Control Plane (Cosmos DB)

No manual steps — Cosmos DB and all containers are provisioned by Bicep.

**Cosmos sources registry** is seeded by a script at E3 test time (see step 12).

---

## E3 — Connectors

### 4. Grant yourself Key Vault Secrets Officer

Required to set API key secrets. Run once per environment.

```powershell
$myId = az ad signed-in-user show --query id -o tsv
az role assignment create `
  --role "Key Vault Secrets Officer" `
  --assignee-object-id $myId `
  --assignee-principal-type User `
  --scope $(az keyvault show --name auspex-dev-kv --resource-group auspex-dev-shared --query id -o tsv)
```

> Use `--assignee-object-id` (not `--assignee email`) — guest accounts cannot be resolved by UPN.

### 5. Set Key Vault secrets

Set these manually — never commit values. Replace `<value>` with the actual key.

```powershell
# Required for E3 (connectors)
az keyvault secret set --vault-name auspex-dev-kv --name "EDGAR-USER-AGENT"     --value "Auspex/1.0 <contact-email>"
az keyvault secret set --vault-name auspex-dev-kv --name "ALPHAVANTAGE-API-KEY" --value "<alphavantage-free-key>"

# Required for E8 (remaining connectors)
az keyvault secret set --vault-name auspex-dev-kv --name "FINNHUB-API-KEY" --value "<finnhub-key>"
az keyvault secret set --vault-name auspex-dev-kv --name "FMP-API-KEY"     --value "<key>"
```

Free account sign-ups:
- Alpha Vantage: https://www.alphavantage.co/support/#api-key (free tier: 25 req/day, 5 req/min — sufficient for MVP daily runs)
- Finnhub: https://finnhub.io (free tier covers `/quote` but not `/stock/candle`; needed for E8 news connector only)
- FMP (Financial Modeling Prep): https://site.financialmodelingprep.com/developer/docs

> `prices_eod` uses Alpha Vantage (`TIME_SERIES_DAILY`) rather than Finnhub — Finnhub's free tier does not include historical candle data.

### 6. Create the Bronze Lakehouse in Fabric

- Fabric portal → `auspex-dev` workspace → **+ New item** → **Lakehouse**
- Name: `auspex_bronze` — bronze Files layer (`Files/bronze/...`)

> The E4 silver notebooks attach `auspex_bronze` as the default lakehouse (to read `Files/bronze/`) and write silver Delta tables to the same lakehouse's **Tables** section. A separate silver lakehouse can be introduced later, but the notebooks must first be updated to use explicit ABFS paths.

### 7. Get the Fabric workspace GUID

From the workspace URL:
```
https://app.fabric.microsoft.com/groups/<WORKSPACE-GUID>/...
```

### 8. Add the ingestion Function App MI to the Fabric workspace

- `auspex-dev` workspace → settings (gear icon) → **Manage access**
- **+ Add people or groups** → search `auspex-dev-func` → role: **Contributor** → **Add**

### 9. Configure OneLake GUIDs in the Function App

No Bicep re-run is required for this step. After the Fabric workspace and Lakehouse exist, set the OneLake GUIDs directly on the ingestion Function App:

- Azure portal → `auspex-dev-func` → **Settings → Environment variables** → **App settings**
- Set `ONELAKE_WORKSPACE_ID` = the Fabric workspace GUID from step 7
- Set `ONELAKE_LAKEHOUSE_NAME` = the Fabric Lakehouse item GUID for `auspex_bronze`
- **Apply → Confirm**

The setting name is `ONELAKE_LAKEHOUSE_NAME` for compatibility with the connector code, but the value must be the Lakehouse item GUID. Do not use the friendly Lakehouse name.

### 10. Build the deployment package (Linux-compatible)

The connector Functions App runs on Linux. Packages must use manylinux wheels, not Windows `.pyd` binaries.

```powershell
cd D:\Projects\auspex\connectors

# Remove any previously built (Windows) packages
Remove-Item -Recurse -Force .python_packages -ErrorAction SilentlyContinue

# Install Linux x86_64 compatible packages
pip install `
  --platform manylinux2014_x86_64 `
  --python-version 312 `
  --implementation cp `
  --only-binary :all: `
  --target .python_packages/lib/site-packages `
  -r requirements.txt

# Verify no Windows .pyd files — output should be empty
Get-ChildItem -Recurse -Filter "*.pyd" .python_packages | Select-Object Name

# Create the zip (exclude local.settings.json)
$items = Get-ChildItem -Force | Where-Object { $_.Name -ne 'local.settings.json' }
Compress-Archive -Path $items.FullName -DestinationPath ..\function-deploy.zip -Force
Write-Host "Zip size: $([math]::Round((Get-Item ..\function-deploy.zip).Length/1MB, 2)) MB"
```

### 11. Deploy the Function package

Deploy the package with Azure CLI zip deployment. Do **not** set `WEBSITE_RUN_FROM_PACKAGE` for this Flex Consumption app; the app uses its configured deployment storage container.

```powershell
# Ensure any previous run-from-package setting is absent.
az functionapp config appsettings delete `
  --subscription 3d043edc-4478-4153-a922-ec782b4b97fe `
  --resource-group auspex-dev-ingest `
  --name auspex-dev-func `
  --setting-names WEBSITE_RUN_FROM_PACKAGE

az functionapp deployment source config-zip `
  --subscription 3d043edc-4478-4153-a922-ec782b4b97fe `
  --resource-group auspex-dev-ingest `
  --name auspex-dev-func `
  --src D:\Projects\auspex\function-deploy.zip

az functionapp restart `
  --subscription 3d043edc-4478-4153-a922-ec782b4b97fe `
  --resource-group auspex-dev-ingest `
  --name auspex-dev-func
```

**Verify functions loaded:**

```powershell
Start-Sleep -Seconds 30
az functionapp function list --name auspex-dev-func --resource-group auspex-dev-ingest --query "[].name" -o tsv
```

Expected output: `auspex-dev-func/sec_form4_run`, `auspex-dev-func/prices_eod_run`, and `auspex-dev-func/run_connector`.

> GitHub Actions automation is deferred to E10 and is not used for the current E1-E4 deployment path.

### 12. Grant yourself Cosmos DB data-plane access

Cosmos DB uses its own native RBAC separate from Azure RBAC. The Bicep assigns the built-in Data Contributor role to the Function App MI only — grant it to your identity for local scripts:

```powershell
$myId = az ad signed-in-user show --query id -o tsv
az cosmosdb sql role assignment create `
  --account-name auspex-dev-cosmos `
  --resource-group auspex-dev-shared `
  --role-definition-id "00000000-0000-0000-0000-000000000002" `
  --principal-id $myId `
  --scope "/subscriptions/3d043edc-4478-4153-a922-ec782b4b97fe/resourceGroups/auspex-dev-shared/providers/Microsoft.DocumentDB/databaseAccounts/auspex-dev-cosmos"
```

> Role definition `00000000-0000-0000-0000-000000000002` is the Cosmos DB Built-in Data Contributor. Use `--assignee-object-id` equivalent (`--principal-id`) — no UPN lookup needed here.

### 13. Seed the Cosmos sources registry

```powershell
cd D:\Projects\auspex\connectors
$env:COSMOS_ENDPOINT = "https://auspex-dev-cosmos.documents.azure.com:443/"
python -m scripts.seed_sources
```

### 14. Verify end-to-end (trigger a connector run)

```powershell
# Get the default function key
$key = az functionapp keys list `
  --name auspex-dev-func `
  --resource-group auspex-dev-ingest `
  --query "functionKeys.default" -o tsv

# Trigger sec_form4
Invoke-RestMethod `
  -Uri "https://auspex-dev-func.azurewebsites.net/api/run" `
  -Method POST `
  -Headers @{"x-functions-key" = $key; "Content-Type" = "application/json"} `
  -Body '{"source_id":"sec_form4"}'
```

Check the Fabric portal → `auspex_bronze` lakehouse → Files → `bronze/sec_form4/` for the NDJSON output.

```powershell
# Trigger prices_eod for a small synchronous chunk. Run nb_01_form4_to_silver
# first to seed Files/config/prices_universe.json. Full-universe HTTP calls can
# exceed the Function HTTP request lifetime even when the Alpha Vantage plan has
# no daily limit, so use symbol_offset/symbol_limit chunks until E10 automation
# moves this to an async/orchestrated path.
Invoke-RestMethod `
  -Uri "https://auspex-dev-func.azurewebsites.net/api/run" `
  -Method POST `
  -Headers @{"x-functions-key" = $key; "Content-Type" = "application/json"} `
  -Body '{"source_id":"prices_eod","symbol_offset":0,"symbol_limit":200}'
```

Repeat chunked `prices_eod` calls by increasing `symbol_offset` until the whole
universe has landed. With `AV_RPM=75`, a chunk of 200 symbols takes roughly
3 minutes plus overhead and stays inside the synchronous HTTP path. Example
offsets for an 803-symbol universe: `0`, `200`, `400`, `600`, `800`.

> `symbol_offset` / `symbol_limit` require the latest Function App package. If a
> full `prices_eod` call returns a generic server error after several minutes,
> redeploy the connector package and use chunked calls rather than rerunning the
> full universe synchronously.

### 14a. Manual E1-E4 smoke checklist

Use this checklist before considering E1-E4 ready for further automation:

- **Azure substrate:** `az deployment sub create` from step 1 completes successfully.
- **Fabric resources:** Bicep-created capacity is named `auspexdevfab`; workspace `auspex-dev` exists; Lakehouse `auspex_bronze` exists.
- **Workspace access:** `auspex-dev-func` managed identity is Contributor on the Fabric workspace.
- **Function App settings:** `auspex-dev-func` has `ONELAKE_WORKSPACE_ID` set to the Fabric workspace GUID and `ONELAKE_LAKEHOUSE_NAME` set to the `auspex_bronze` Lakehouse item GUID.
- **Function package:** `az functionapp function list --name auspex-dev-func --resource-group auspex-dev-ingest --query "[].name" -o tsv` lists `sec_form4_run`, `prices_eod_run`, and `run_connector`.
- **Control plane:** `python -m scripts.seed_sources` completes and the Cosmos `sources` container contains `sec_form4` and `prices_eod` with `enabled=true`.
- **Bronze Form 4:** `POST /api/run` with `{"source_id":"sec_form4"}` returns `status=ok` or `status=empty`; the bronze path appears under `Files/bronze/sec_form4/{yyyy}/{mm}/{dd}/` using the batch partition date.
- **Silver Form 4:** run `nb_00_entity_resolution`, then `nb_01_form4_to_silver`; verify `security_master`, `dim_security`, `silver_insider_txn.security_sk`, and `Files/config/prices_universe.json` exist.
- **Bronze prices:** `POST /api/run` with `{"source_id":"prices_eod"}` returns `status=ok` or `status=empty`; the bronze path appears under `Files/bronze/prices_eod/{yyyy}/{mm}/{dd}/`.
- **Silver prices:** run `nb_02_prices_to_silver`; verify `silver_prices.security_sk` has rows for the requested date window and unresolved symbols are in `silver_security_quarantine`.
- **PIT sanity:** the SQL checks in step 18 return zero future `knowledge_date` violations.

---

## E4 — Silver Transforms + Entity Resolution

Current status: this guide validates the E4 reference path for the implemented sources (`sec_form4`, `prices_eod`): `security_master`, canonical `dim_security`, `silver_insider_txn`, `silver_prices`, PIT sanity, and replay-safe quarantine. Exact CIK/ticker resolution is implemented now; ISIN/fuzzy fallback is reserved for later sources that provide those identifiers.

Three PySpark notebooks transform raw bronze NDJSON into cleaned, deduplicated silver Delta tables.  
They all use the `auspex_bronze` lakehouse as their default (reads `Files/bronze/…`, writes Delta tables to `Tables/`).

---

### 15. Create each notebook in Fabric

Fabric notebooks are created in the portal and code is pasted cell by cell. The source files are in `fabric/notebooks/`. Repeat the following for each of the three notebooks.

**Open the source file** in VS Code or any editor:
```
fabric/notebooks/nb_00_entity_resolution.py
fabric/notebooks/nb_01_form4_to_silver.py
fabric/notebooks/nb_02_prices_to_silver.py
```

**Create the notebook in Fabric:**

1. Go to `https://app.fabric.microsoft.com` → `auspex-dev` workspace.
2. Click **+ New item** → select **Notebook**.
3. In the notebook header, click the default name (`Notebook 1`) and rename it to match the file name (e.g., `nb_00_entity_resolution`).

**Add the lakehouse:**

4. In the left-hand **Explorer** panel, click **Add lakehouse**.
5. Select **Existing lakehouse** → choose `auspex_bronze` → click **Add**.
6. Confirm `auspex_bronze` appears under **Lakehouses** with a house icon. It is now the default — `Files/` and `Tables/` relative paths in code resolve to it.

**Paste the code (one cell per section):**

The `.py` files use `# COMMAND ----------` as a cell separator. Each block of code between two separators becomes one notebook cell.

7. In the notebook, click on the first empty code cell.
8. Open the source `.py` file and copy everything from the top up to (but not including) the first `# COMMAND ----------` line. Paste it into the first cell.
9. Click **+ Code** below the cell to add a new cell. Copy the next block (between the first and second `# COMMAND ----------` lines) and paste it.
10. Repeat for every block. The number of cells equals the number of `# COMMAND ----------` separators plus one.

> **Tip:** the `# COMMAND ----------` lines themselves are not pasted — they are the separator, not code.

**Save:**

11. Press **Ctrl+S** or click the save icon. The notebook is saved to the workspace.

---

### 16. Add notebook parameters

Each notebook reads optional pipeline parameters via `mssparkutils.widgets.get()` with a fallback default. You can set them directly in the notebook for a manual run, or leave the defaults.

The notebooks read parameters via `mssparkutils.widgets.get()` with hardcoded fallback defaults, so **no configuration is required for a manual run** — just run them as-is.

If you want to change the date window, user-agent, or EDGAR throttle for a manual run, edit the values directly in the first cell of the notebook (the `_widget(...)` fallback values):

**`nb_00_entity_resolution`** — edit the fallback in the first cell:
```python
EDGAR_USER_AGENT = _widget("edgar_user_agent", "Auspex/1.0 auspex@auspex.ai")
```

`nb_00` fetches SEC `company_tickers.json`, seeds `security_master`, maintains `dim_security`, and creates/upgrades the replay-safe quarantine tables.

**`nb_01_form4_to_silver`** — edit the fallbacks in the first cell:
```python
from_date        = _widget("from_date", "2026-06-10")   # change as needed
to_date          = _widget("to_date",   "2026-06-17")
EDGAR_USER_AGENT = _widget("edgar_user_agent", "Auspex/1.0 auspex@auspex.ai")
EDGAR_REQUESTS_PER_MINUTE = int(_widget("edgar_requests_per_minute", "450"))
_MAX_WORKERS = max(1, int(_widget("max_workers", "5")))
```

`EDGAR_REQUESTS_PER_MINUTE` is the aggregate request cap for the whole notebook process. It is not multiplied by `_MAX_WORKERS`; workers only overlap network wait time. EDGAR's fair-use ceiling is 10 requests/second (600/minute); the default 450/minute keeps a 25% buffer. This is separate from Alpha Vantage's `AV_RPM` limit.

**`nb_02_prices_to_silver`** — edit the fallbacks in the first cell:
```python
from_date = _widget("from_date", "2026-06-10")
to_date   = _widget("to_date",   "2026-06-17")
```

> **Pipeline integration (later):** when the Fabric Data Factory pipeline calls these notebooks, it passes values via **Base parameters** in the Notebook activity. To allow the pipeline to override a variable, mark the first cell as a parameter cell: hover over it → click **`···`** (More commands) → **Toggle parameter cell**. A grey **Parameters** badge appears on the cell. The pipeline then injects its values at runtime, overriding the fallback defaults.

---

### 17. Run notebooks in order

Run the notebooks one at a time from the Fabric portal. Click **Run all** in the toolbar of each notebook.

**Order is mandatory:**

```
1. nb_00_entity_resolution   — seeds security_master; creates quarantine tables
2. nb_01_form4_to_silver     — bronze sec_form4 → entity-resolved silver_insider_txn
3. nb_02_prices_to_silver    — bronze prices_eod → entity-resolved silver_prices
```

`nb_01` is the slowest — each new Form 4 accession may require 2–3 EDGAR HTTP calls to discover and fetch the full filing XML (transaction amounts are not in the search index). With the default aggregate cap of 450 requests/minute and 5 workers, a 7-day backfill can take **30–60 minutes** depending on filing count, XML discovery path, and SEC response latency. Subsequent daily runs are much faster — already-processed accession numbers are skipped via `done_set`, so only that day's new filings are fetched.

`nb_00` creates/upgrades `dim_security`, `silver_security_quarantine`, `silver_dq_quarantine`, and `silver_parse_errors`. If you are upgrading an existing E4-lite lakehouse, run `nb_00` first so later notebooks can add `security_sk` and merge quarantine rows by `natural_key`.

`nb_01` also writes `Files/config/prices_universe.json` to the lakehouse at the end of each run — a JSON file containing the distinct resolved `issuer_ticker` values from the full `silver_insider_txn` table. The `prices_eod` connector reads this file to know which symbols to fetch, so **nb_01 must complete before triggering the prices_eod connector**.

When upgrading from the earlier E4-lite notebooks, rerun `nb_01` for the desired Form 4 window. It skips already resolved filings but reprocesses legacy rows where `security_sk` is missing. Rerun `nb_02` after the next `prices_eod` bronze pull; it backfills `security_sk` for resolvable legacy `silver_prices` rows.

Watch the cell outputs for progress messages like:
```
Fetched 10823 tickers from SEC
security_master: 10823 rows
dim_security current rows: 10823
Merged 4231 rows into silver_insider_txn
Merged 21 rows into silver_prices
```

If a cell raises an error, read the traceback. Common issues:
- **Table not found** — run `nb_00` first; it creates the control tables.
- **No such file or directory** (`Files/bronze/…`) — the bronze lakehouse is not attached as default, or the date range has no data yet.
- **HTTP 430 `TooManyRequestsForCapacity`** — Fabric could not schedule the Spark job because the capacity is saturated. Open Fabric → workspace `auspex-dev` → **Monitoring hub** or **Workspace settings → Job management**; cancel stale/running Spark jobs, wait a few minutes for capacity to free, then rerun only one notebook at a time. This is Fabric capacity contention, not an EDGAR request-rate problem.
- **403 from EDGAR** — rate limit; reduce `edgar_requests_per_minute` or the date window. Do not raise `_MAX_WORKERS` to solve this; the cap is aggregate across workers.

---

### 18. Verify silver output

Open the `auspex_bronze` SQL analytics endpoint (Fabric portal → `auspex_bronze` → **SQL analytics endpoint** tab) and run:

```sql
-- Row counts
SELECT 'security_master'          AS tbl, COUNT(*) AS rows FROM security_master
UNION ALL
SELECT 'dim_security_current',             COUNT(*)        FROM dim_security WHERE is_current = 1
UNION ALL
SELECT 'silver_insider_txn',               COUNT(*)        FROM silver_insider_txn
UNION ALL
SELECT 'silver_prices',                    COUNT(*)        FROM silver_prices
UNION ALL
SELECT 'silver_security_quarantine',       COUNT(*)        FROM silver_security_quarantine
UNION ALL
SELECT 'silver_dq_quarantine',             COUNT(*)        FROM silver_dq_quarantine;

-- Quarantine breakdown — should be dominated by NO_NONDERIVATIVE_TXNS (options-only filings, normal)
SELECT reason, COUNT(*) AS n
FROM silver_security_quarantine
GROUP BY reason
ORDER BY n DESC;

-- PIT sanity: no knowledge_date in the future
SELECT 'silver_insider_txn' AS tbl, COUNT(*) AS violations
FROM silver_insider_txn
WHERE knowledge_date > CAST(GETDATE() AS DATE)
UNION ALL
SELECT 'silver_prices', COUNT(*)
FROM silver_prices
WHERE knowledge_date > CAST(GETDATE() AS DATE);

-- Entity-resolution sanity: silver rows must be resolved to dim_security
SELECT 'silver_insider_txn' AS tbl, COUNT(*) AS unresolved_rows
FROM silver_insider_txn
WHERE security_sk IS NULL
UNION ALL
SELECT 'silver_prices', COUNT(*)
FROM silver_prices
WHERE security_sk IS NULL;

-- Replay sanity: quarantine natural keys must be unique
SELECT natural_key, COUNT(*) AS duplicate_count
FROM silver_security_quarantine
GROUP BY natural_key
HAVING COUNT(*) > 1
UNION ALL
SELECT natural_key, COUNT(*)
FROM silver_dq_quarantine
GROUP BY natural_key
HAVING COUNT(*) > 1;

-- Sample insider buys
SELECT TOP 10
    security_sk, accession_no, issuer_ticker, issuer_name,
    reporter_name, txn_code, is_buy,
    shares, price, value_usd, event_date, knowledge_date
FROM silver_insider_txn
WHERE is_buy = 1
ORDER BY event_date DESC;

-- Sample prices
SELECT TOP 10 security_sk, symbol, date, close, volume, knowledge_date
FROM silver_prices
ORDER BY date DESC;
```

Expected state after a successful E4 run:

| Table | Expected rows |
|---|---|
| `security_master` | ~10 000 (all SEC-listed tickers) |
| `dim_security` | ~10 000 current rows, with SCD2-ready inactive rows accumulating over time |
| `silver_insider_txn` | Several thousand (7-day backfill of Form 4 filings) |
| `silver_prices` | One row per resolved security per trading day in the window (driven by the prices universe) |
| `silver_security_quarantine` | Some rows, mostly `NO_NONDERIVATIVE_TXNS`; any `SECURITY_UNRESOLVED` rows need review before gold |
| `silver_dq_quarantine` | Usually 0; non-zero rows indicate invalid source price/date records |

Validated dev run (2026-06-27):

| Check | Result |
|---|---:|
| `security_master` | 10433 rows |
| `dim_security` current rows | 10433 rows |
| `sec_form4` bronze records | 2915 rows |
| `silver_insider_txn` merged rows | 4614 rows |
| `silver_security_quarantine` `NO_NONDERIVATIVE_TXNS` | 644 rows |
| `silver_security_quarantine` `SECURITY_UNRESOLVED` | 47 rows |
| `prices_eod` bronze rows across chunks | 3996 rows |
| `silver_prices` merged rows | 3996 rows |
| unresolved `security_sk` in silver | 0 rows |
| future `knowledge_date` violations | 0 rows |
| duplicate quarantine natural keys / duplicate price keys | 0 rows |

Conclusion: E4 is operationally validated for the currently implemented E3 sources (`sec_form4`, `prices_eod`). Later E8 sources must extend E4 with their own source-specific silver parsers, DQ checks, and entity-resolution rules.

---

## E5 — Gold Star Schema

Current status: this guide validates the first E5 gold path for the implemented E4 sources (`sec_form4`, `prices_eod`). The notebook creates the gold table contract, loads dimensions/facts for current data, and creates empty forward-compatible fact tables for later E8 sources.

### 19. Create the silver-to-gold notebook in Fabric

Create or sync the notebook:

```
fabric/notebooks/nb_03_silver_to_gold.py
```

Attach it to the same `auspex_bronze` Lakehouse used by E4. It reads E4 Delta tables and writes gold Delta tables in the Lakehouse.

Optional Warehouse promotion DDL lives in:

```
fabric/warehouse/01_dims.sql
fabric/warehouse/02_facts.sql
fabric/warehouse/03_fx.sql
```

These SQL files define the Fabric Warehouse contract for the same gold tables. The E5 notebook is the current manual validation path; Warehouse promotion is part of the later Fabric Git/deployment pipeline.

### 20. Run E5 after E4 validation

Run `nb_03_silver_to_gold` after all E4 notebooks have completed and E4 SQL checks have passed.

Expected outputs include row counts for:

```
dim_security
dim_date
dim_source
dim_entity
fact_market_daily
fact_insider_txn
fact_institutional_holding
fact_ownership_event
fact_news_sentiment
fact_contract_award
fact_macro
fact_fx_rate
```

For the current E3/E4 source set, only `fact_market_daily` and `fact_insider_txn` should be populated. The other fact tables should exist and usually have 0 rows until E8 sources are implemented.

### 21. Verify gold output

Open the `auspex_bronze` SQL analytics endpoint and run:

```sql
-- Core row counts
SELECT 'dim_security' AS tbl, COUNT(*) AS rows FROM dim_security
UNION ALL
SELECT 'dim_date', COUNT(*) FROM dim_date
UNION ALL
SELECT 'dim_source', COUNT(*) FROM dim_source
UNION ALL
SELECT 'dim_entity', COUNT(*) FROM dim_entity
UNION ALL
SELECT 'fact_market_daily', COUNT(*) FROM fact_market_daily
UNION ALL
SELECT 'fact_insider_txn', COUNT(*) FROM fact_insider_txn;

-- FK sanity: no market facts without dim_security
SELECT COUNT(*) AS orphan_market_rows
FROM fact_market_daily f
LEFT JOIN dim_security s ON f.security_sk = s.security_sk
WHERE s.security_sk IS NULL;

-- FK sanity: no insider facts without dim_security
SELECT COUNT(*) AS orphan_insider_rows
FROM fact_insider_txn f
LEFT JOIN dim_security s ON f.security_sk = s.security_sk
WHERE s.security_sk IS NULL;

-- PIT sanity: fact PIT columns must be populated
SELECT 'fact_market_daily' AS tbl, COUNT(*) AS missing_pit_rows
FROM fact_market_daily
WHERE event_date IS NULL OR knowledge_date IS NULL
UNION ALL
SELECT 'fact_insider_txn', COUNT(*)
FROM fact_insider_txn
WHERE event_date IS NULL OR knowledge_date IS NULL;

-- Idempotency sanity: no duplicate market grain
SELECT security_sk, date_sk, COUNT(*) AS duplicate_count
FROM fact_market_daily
GROUP BY security_sk, date_sk
HAVING COUNT(*) > 1;

-- Idempotency sanity: no duplicate insider transaction grain
SELECT accession_no, line_no, COUNT(*) AS duplicate_count
FROM fact_insider_txn
GROUP BY accession_no, line_no
HAVING COUNT(*) > 1;
```

Expected results:

| Check | Expected |
|---|---|
| `fact_market_daily` rows | equals `silver_prices` rows for the current E4 run |
| `fact_insider_txn` rows | equals `silver_insider_txn` rows for the current E4 run |
| orphan market rows | 0 |
| orphan insider rows | 0 |
| missing PIT rows | 0 |
| duplicate market grain | no rows |
| duplicate insider grain | no rows |

Replay check: rerun `nb_03_silver_to_gold` on the same E4 data and rerun the duplicate-grain SQL. Row counts should converge and duplicate checks should still return no rows.

Validated dev run (2026-06-27):

| Check | Result |
|---|---:|
| `dim_security` | 10433 rows |
| `dim_date` | 64 rows |
| `dim_source` | 2 rows |
| `dim_entity` | 2014 rows |
| `fact_market_daily` | 3996 rows |
| `fact_insider_txn` | 4608 rows |
| future E8 fact tables | 0 rows each |
| orphan market rows | 0 rows |
| orphan insider rows | 0 rows |
| missing PIT rows | 0 rows |
| duplicate `fact_market_daily` grain | no rows |
| duplicate `fact_insider_txn` grain | no rows |
| duplicate `dim_entity.entity_natural_id` | no rows |

Conclusion: E5 is operationally validated for the currently implemented E4 source set (`sec_form4`, `prices_eod`). Later E8 sources must populate the empty forward-compatible fact tables through source-specific silver/gold loaders.

---

## E6 — Metric Layer + Feature Contract

Current status: E6a is implemented for the currently available E5 facts. The notebook computes PIT-safe price/risk context from `fact_market_daily`, Form 4 smart-money metrics from `fact_insider_txn`, seeds `metric_weights`, and publishes the stable `v_security_daily_features` contract. In the Lakehouse, the `v_*` serving projections are materialized as Delta tables so the Fabric SQL endpoint can query them. The Warehouse SQL files define the promoted Warehouse objects as true SQL views. The final six-leg `opportunity_score` remains gated by design until E8 data sources and the E14 valuation brake are complete; rows are marked with `score_status = 'INCOMPLETE_E6A_WAITING_E8_E14'`.

Artifacts:

```text
fabric/notebooks/nb_04_metrics.py
fabric/warehouse/metrics/04_base_metrics.sql
fabric/warehouse/metrics/12b_opportunity_legs.sql
fabric/warehouse/metrics/13_opportunity_score.sql
tests/test_e6_metric_contract.py
```

Run order:

1. Complete and validate E5 (`nb_03_silver_to_gold`).
2. Run `nb_04_metrics` attached to the same Lakehouse.
3. Rerun `nb_04_metrics` once on the same E5 data to verify replay convergence.
4. Run the SQL checks below in the Lakehouse SQL endpoint or Warehouse mirror.

Expected notebook behavior:

| Output | Expected |
|---|---|
| `metric_weights` | 6 active `e6a_v1` weights, sum = 1.000000 |
| `security_daily_features` | one row per `(security_sk, as_of)` snapshot that is PIT-valid for known market data |
| `v_market_momentum` | materialized as a Lakehouse Delta table |
| `v_market_risk` | materialized as a Lakehouse Delta table |
| `v_risk_adjusted` | materialized as a Lakehouse Delta table |
| `v_smart_money` | materialized as a Lakehouse Delta table |
| `v_opportunity_legs` | materialized as a Lakehouse Delta table with NULL E8/E14 legs |
| `v_opportunity_score` | materialized as a Lakehouse Delta table with NULL `opportunity_score` during E6a |
| `v_security_daily_features` | materialized as a Lakehouse Delta table; stable API/agent feature contract |

Validation SQL:

```sql
-- E6 feature grain must be replay-safe.
SELECT security_sk, date_sk, COUNT(*) AS duplicate_count
FROM security_daily_features
GROUP BY security_sk, date_sk
HAVING COUNT(*) > 1;

-- E6 must produce usable rows. Historical backfills may collapse many price
-- event dates into the first date Auspex knew them, so do not expect this count
-- to equal fact_market_daily row count.
SELECT COUNT(*) AS feature_rows
FROM security_daily_features;

-- PIT sanity: no feature row can expose knowledge after its as-of date.
SELECT COUNT(*) AS future_knowledge_rows
FROM security_daily_features
WHERE as_of IS NULL OR max_knowledge_date IS NULL OR max_knowledge_date > as_of;

-- Score range sanity for the provisional composite metric.
SELECT COUNT(*) AS invalid_composite_scores
FROM security_daily_features
WHERE composite_growth_score < 0 OR composite_growth_score > 100;

-- E6a gate sanity: final Opportunity Score must not be published before E8/E14.
SELECT COUNT(*) AS prematurely_published_scores
FROM security_daily_features
WHERE opportunity_score IS NOT NULL
   OR score_status <> 'INCOMPLETE_E6A_WAITING_E8_E14';

-- Weight sanity: active composite weights sum to one.
SELECT ROUND(SUM(weight), 6) AS active_weight_sum
FROM metric_weights
WHERE metric_group = 'composite_growth_score'
  AND is_active = true
  AND version = 'e6a_v1';

-- Contract smoke: the serving view should expose only PIT-valid rows.
SELECT COUNT(*) AS serving_rows
FROM v_security_daily_features;
```

Expected results:

| Check | Expected |
|---|---|
| duplicate feature grain | no rows |
| feature rows | greater than 0 |
| future knowledge rows | 0 |
| invalid composite scores | 0 |
| prematurely published scores | 0 |
| active weight sum | 1.000000 |
| serving rows | equals `security_daily_features` rows when all PIT checks pass |

Replay check: rerun `nb_04_metrics` on the same E5 data and rerun the duplicate-grain and PIT checks. Row counts should converge and duplicate checks should still return no rows.

Validated dev run (2026-06-27):

| Check | Result |
|---|---:|
| `security_daily_features` | 801 rows |
| `v_security_daily_features` | 801 rows |
| missing/future PIT rows | 0 rows |
| duplicate feature grain | 0 rows |
| invalid composite/opportunity scores | 0 rows |
| active `e6a_v1` weight sum | 1.000000 |
| Lakehouse SQL endpoint `v_security_daily_features` query | returns 801 rows |

Conclusion: E6a is operationally validated for the current E5 fact set. The final six-leg `opportunity_score` remains intentionally unpublished until E8/E14 provide the missing source and valuation-brake legs.

---

## Future CI/CD — GitHub Actions (OIDC, E10)

GitHub Actions are **not used for the current E1-E4 deployment path**. The supported deployment path is the manual/local procedure above. The workflows in `.github/workflows/` are future E10 automation and should remain disabled until the manual deployment and smoke checks are stable.

When E10 enables CI/CD, the workflows authenticate to Azure via OIDC (no stored credentials). Set these GitHub Actions secrets in the `dev` environment:

| Secret | Value |
|--------|-------|
| `AZURE_CLIENT_ID` | App registration client ID (federated credential) |
| `AZURE_TENANT_ID` | Azure AD tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Azure subscription ID |

Setup steps:
1. Create an app registration in Entra ID
2. Add a federated credential: entity type `Environment`, environment name `dev`, repo `francesco-sodano/auspex`
3. Assign `Contributor` on all `auspex-dev-*` resource groups
4. Assign `Storage Blob Data Owner` on `auspexdevfuncst` (for bearer-token blob upload during deployment)
5. Add the three secrets to the GitHub `dev` environment

The deployment workflow must:
- Build Python packages on a Linux runner (correct platform — no manylinux targeting needed)
- Deploy the package with `az functionapp deployment source config-zip`
- Verify `WEBSITE_RUN_FROM_PACKAGE` is absent
- Verify `run_connector`, `sec_form4_run`, and `prices_eod_run` are indexed after deployment

---

## Storage account names (derived from Bicep)

| Function App | Storage account |
|---|---|
| `auspex-dev-func` | `auspexdevfuncst` |
| `auspex-dev-wapi` | `auspexdevwapist` |
| `auspex-prod-func` | `auspexprodfu` *(truncated to 24 chars)* |
| `auspex-prod-wapi` | `auspexprodwa` *(truncated to 24 chars)* |

> The name formula is `take(replace('${appName}st', '-', ''), 24)`.
