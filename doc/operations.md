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
- `func azure functionapp publish` and Kudu zip deploy both fail — they require `AzureWebJobsStorage` as a shared-key connection string so Kudu can upload the built squashfs artifact to blob storage.
- Azure CLI storage data plane commands (`az storage container create`, `az storage blob upload`) fail from both local machine and Cloud Shell under the Microsoft guest account.
- The workaround is **portal-based blob upload + `WEBSITE_RUN_FROM_PACKAGE`**, which bypasses Kudu entirely.

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

### 2. Create Fabric capacity (manual — cannot be automated via Bicep)

Fabric capacity must be provisioned from the Azure portal after a tenant admin enables Fabric:

- Azure portal → **Create a resource** → search **Microsoft Fabric**
- Name: `auspex-dev-fabric` / SKU: F2 (minimum) / Region: Switzerland North
- Assign yourself as capacity admin

> Fabric capacity is billed by the hour when running. The capacity scheduler Function (Timer trigger) handles resume/suspend automatically once E3 is complete.

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
  --scope $(az keyvault show --name auspex-dev-kv --resource-group auspex-dev-ingest --query id -o tsv)
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

### 6. Create the Bronze and Silver Lakehouses in Fabric

- Fabric portal → `auspex-dev` workspace → **+ New item** → **Lakehouse**
- Name: `auspex_bronze` — bronze Files layer (`Files/bronze/...`)
- Repeat: **+ New item** → **Lakehouse** → Name: `auspex_silver` — silver Delta tables (E4+)

> The E4 silver notebooks attach `auspex_bronze` as the default lakehouse (to read `Files/bronze/`) but write silver Delta tables to the **default Tables section**. If you want a separate silver lakehouse, update the notebook `saveAsTable` calls to use explicit ABFS paths.

### 7. Get the Fabric workspace GUID

From the workspace URL:
```
https://app.fabric.microsoft.com/groups/<WORKSPACE-GUID>/...
```

### 8. Add the ingestion Function App MI to the Fabric workspace

- `auspex-dev` workspace → settings (gear icon) → **Manage access**
- **+ Add people or groups** → search `auspex-dev-func` → role: **Contributor** → **Add**

### 9. Set ONELAKE_WORKSPACE_ID in the Function App

- Azure portal → `auspex-dev-func` → **Settings → Environment variables**
- Set `ONELAKE_WORKSPACE_ID` = `<WORKSPACE-GUID>` from step 7
- **Apply → Confirm**

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

### 11. Upload zip and set WEBSITE_RUN_FROM_PACKAGE

Kudu-based deployment is blocked by the subscription policy (see Subscription constraints). The workaround is to upload the package directly to blob storage via the portal and point the runtime at it.

**Upload via portal** (portal runs server-side — bypasses local Conditional Access):

1. Portal → Storage accounts → **`auspexdevfuncst`** → Data storage → **Containers**
2. **+ Container** → name: `deployments`, access: Private → **Create**
3. Click into `deployments` → **Upload** → select `function-deploy.zip` → **Upload**
4. Click `function-deploy.zip` → **Generate SAS** tab
   - Signing method: **User delegation key** (auto-selected when shared key is disabled)
   - Permissions: **Read**
   - Expiry: **+6 days** (maximum for user delegation SAS)
5. Click **Generate SAS token and URL** → copy the **Blob SAS URL**

**Set the app setting** (use ARM REST API — the `&` in SAS URLs breaks `az functionapp config appsettings set` on Windows):

```powershell
$token = az account get-access-token --resource https://management.azure.com/ --query accessToken -o tsv
$sasUrl = '<paste Blob SAS URL here>'

$getUri = "https://management.azure.com/subscriptions/3d043edc-4478-4153-a922-ec782b4b97fe/resourceGroups/auspex-dev-ingest/providers/Microsoft.Web/sites/auspex-dev-func/config/appsettings/list?api-version=2023-12-01"
$current = Invoke-RestMethod -Uri $getUri -Method POST -Headers @{Authorization = "Bearer $token"} -ContentType "application/json"

$settings = $current.properties
$settings | Add-Member -NotePropertyName "WEBSITE_RUN_FROM_PACKAGE" -NotePropertyValue $sasUrl -Force

$body = @{ properties = $settings } | ConvertTo-Json -Depth 10
$putUri = "https://management.azure.com/subscriptions/3d043edc-4478-4153-a922-ec782b4b97fe/resourceGroups/auspex-dev-ingest/providers/Microsoft.Web/sites/auspex-dev-func/config/appsettings?api-version=2023-12-01"
Invoke-RestMethod -Uri $putUri -Method PUT -Headers @{Authorization = "Bearer $token"} -Body $body -ContentType "application/json" | Out-Null

az functionapp restart --name auspex-dev-func --resource-group auspex-dev-ingest
```

**Verify functions loaded:**

```powershell
Start-Sleep -Seconds 30
az functionapp function list --name auspex-dev-func --resource-group auspex-dev-ingest --query "[].name" -o tsv
```

Expected output: `auspex-dev-func/sec_form4_run` and `auspex-dev-func/prices_eod_run`.

> The SAS URL expires in 6 days. On each redeployment repeat steps 10–11. For CI/CD, GitHub Actions generates a fresh SAS using OIDC bearer-token blob writes (no shared key needed) and updates the setting automatically.

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
  -Uri "https://auspex-dev-func.azurewebsites.net/api/sec_form4/run" `
  -Method POST `
  -Headers @{"x-functions-key" = $key}
```

Check the Fabric portal → `auspex_bronze` lakehouse → Files → `bronze/sec_form4/` for the NDJSON output.

---

## E4 — Silver Transforms + Entity Resolution

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

Each notebook reads optional pipeline parameters via `dbutils.widgets.get()` with a fallback default. You can set them directly in the notebook for a manual run, or leave the defaults.

**For `nb_00_entity_resolution`** — one parameter:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `edgar_user_agent` | `Auspex/1.0 auspex-bot@example.com` | SEC-required User-Agent header. Replace with your actual contact email. |

To override for a manual run, add this as the **very first cell** (before all other cells) and mark it as a parameter cell:

```python
edgar_user_agent = "Auspex/1.0 fsodano79@gmail.com"
```

Then right-click the cell → **Toggle parameter cell** (Fabric adds a `# Parameters` marker). When run from a pipeline, the pipeline overrides this value; when run manually, this value is used.

**For `nb_01_form4_to_silver`** — three parameters:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `from_date` | 7 days ago | Start of the bronze window to process (`YYYY-MM-DD`) |
| `to_date` | today | End of the window |
| `edgar_user_agent` | `Auspex/1.0 auspex-bot@example.com` | As above |

First cell (mark as parameter cell):
```python
from_date        = "2026-06-08"   # adjust as needed
to_date          = "2026-06-15"
edgar_user_agent = "Auspex/1.0 fsodano79@gmail.com"
```

**For `nb_02_prices_to_silver`** — two parameters:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `from_date` | 7 days ago | Start of the bronze window |
| `to_date` | today | End of the window |

First cell (mark as parameter cell):
```python
from_date = "2026-06-08"
to_date   = "2026-06-15"
```

---

### 17. Run notebooks in order

Run the notebooks one at a time from the Fabric portal. Click **Run all** in the toolbar of each notebook.

**Order is mandatory:**

```
1. nb_00_entity_resolution   — seeds security_master; creates quarantine tables
2. nb_01_form4_to_silver     — bronze sec_form4 → silver_insider_txn
3. nb_02_prices_to_silver    — bronze prices_eod → silver_prices
```

`nb_01` is the slowest — it makes one EDGAR HTTP call per new Form 4 accession number to fetch the full filing XML (transaction amounts are not in the search index). For a 7-day backfill expect 3–5 minutes. Subsequent daily runs only fetch new filings; already-processed accession numbers are skipped automatically.

Watch the cell outputs for progress messages like:
```
Fetched 10823 tickers from SEC
security_master: 10823 rows
Merged 4231 rows into silver_insider_txn
Merged 21 rows into silver_prices
```

If a cell raises an error, read the traceback. Common issues:
- **Table not found** — run `nb_00` first; it creates the control tables.
- **No such file or directory** (`Files/bronze/…`) — the bronze lakehouse is not attached as default, or the date range has no data yet.
- **403 from EDGAR** — rate limit; reduce the window or add a longer sleep.

---

### 18. Verify silver output

Open the `auspex_bronze` SQL analytics endpoint (Fabric portal → `auspex_bronze` → **SQL analytics endpoint** tab) and run:

```sql
-- Row counts
SELECT 'security_master'          AS tbl, COUNT(*) AS rows FROM security_master
UNION ALL
SELECT 'silver_insider_txn',               COUNT(*)        FROM silver_insider_txn
UNION ALL
SELECT 'silver_prices',                    COUNT(*)        FROM silver_prices;

-- Quarantine breakdown — should be dominated by NO_NONDERIVATIVE_TXNS (options-only filings, normal)
SELECT reason, COUNT(*) AS n
FROM silver_security_quarantine
GROUP BY reason
ORDER BY n DESC;

-- PIT sanity: no knowledge_date in the future
SELECT COUNT(*) AS violations
FROM silver_insider_txn
WHERE knowledge_date > CAST(GETDATE() AS DATE);

-- Sample insider buys
SELECT TOP 10
    accession_no, issuer_ticker, issuer_name,
    reporter_name, txn_code, is_buy,
    shares, price, value_usd, event_date, knowledge_date
FROM silver_insider_txn
WHERE is_buy = 1
ORDER BY event_date DESC;

-- Sample prices
SELECT TOP 10 symbol, date, close, volume, knowledge_date
FROM silver_prices
ORDER BY date DESC;
```

Expected state after a successful E4 run:

| Table | Expected rows |
|---|---|
| `security_master` | ~10 000 (all SEC-listed tickers) |
| `silver_insider_txn` | Several thousand (7-day backfill of Form 4 filings) |
| `silver_prices` | 3 rows × days (AAPL, MSFT, NVDA for the test window) |
| `silver_security_quarantine` | Some rows, mostly `NO_NONDERIVATIVE_TXNS` (derivative-only filings — expected) |

---

## CI/CD — GitHub Actions (OIDC)

The workflows in `.github/workflows/` authenticate to Azure via OIDC (no stored credentials). Set these GitHub Actions secrets in the `dev` environment:

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
- Upload the zip to `auspexdevfuncst/deployments/` using `az storage blob upload --auth-mode login` (bearer token, no shared key)
- Generate a user-delegation SAS (`az storage blob generate-sas --as-user --auth-mode login`, max 6 days)
- Update `WEBSITE_RUN_FROM_PACKAGE` via ARM REST API (avoid `&` shell-parsing issues)

---

## Storage account names (derived from Bicep)

| Function App | Storage account |
|---|---|
| `auspex-dev-func` | `auspexdevfuncst` |
| `auspex-dev-wapi` | `auspexdevwapist` |
| `auspex-prod-func` | `auspexprodfu` *(truncated to 24 chars)* |
| `auspex-prod-wapi` | `auspexprodwa` *(truncated to 24 chars)* |

> The name formula is `take(replace('${appName}st', '-', ''), 24)`.
