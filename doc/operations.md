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

### 6. Create the Bronze Lakehouse in Fabric

- Fabric portal → `auspex-dev` workspace → **+ New item** → **Lakehouse**
- Name: `auspex_bronze`

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
