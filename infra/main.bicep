// main.bicep — Auspex platform infrastructure
// Scope: subscription (declares all resource groups and composes modules)
//
// Usage:
//   az deployment sub create \
//     --location switzerlandnorth \
//     --template-file infra/main.bicep \
//     --parameters @infra/params/dev.json
//
// Deployment order enforced by explicit dependsOn / output references:
//   1.  Resource groups (all five, in parallel)
//   1b. Network-VNet — VNet + subnets deployed immediately after RGs so subnet IDs
//       exist before Function Apps configure VNet integration.
//   2.  Monitor (Log Analytics + App Insights) — outputs App Insights connection string
//   3.  Ingestion Function App + Static Web App (in parallel, after monitor + network-vnet)
//   3b. Web API Function App (after Static Web App so its hostname is in CORS)
//   4.  Key Vault — uses principal IDs for RBAC; stores App Insights connection string as secret
//   5.  Cosmos DB — uses principal IDs for data-plane RBAC
//   6.  Fabric Capacity — Bicep-managed; uses ingest func principal ID for Contributor RBAC
//   7.  AI Search — uses web API func principal ID for Search Index Data Reader RBAC
//   8.  Azure OpenAI — no cross-dependencies
//  10.  Network — private DNS zones + private endpoints; deployed last because private
//       endpoints require resource IDs from modules above. VNet ID flows from step 1b.
//
// Circular-dependency avoidance:
//   Cosmos endpoint / KV name / Fabric capacity name: computed as deterministic local
//   variables so Function Apps don't need to wait for those module outputs.

targetScope = 'subscription'

@description('Environment name')
@allowed(['dev', 'prod'])
param env string

@description('Primary region for all resources that support Switzerland North')
param location string = 'switzerlandnorth'

@description('UPN of the Fabric capacity administrator')
param fabricAdminUpn string

@description('Log Analytics retention in days (30 for dev, 90 for prod)')
param logRetentionDays int = 30

@description('Email address for operational alerts')
@minLength(3)
param alertEmailAddress string

@secure()
@description('SEC EDGAR user agent with an operator-monitored contact address')
@minLength(3)
param edgarUserAgent string

@secure()
@description('Alpha Vantage API key for enabled price, fundamental, FX, news, and ETF sources')
@minLength(1)
param alphaVantageApiKey string

@secure()
@description('Finnhub API key for the enabled company-news source')
@minLength(1)
param finnhubApiKey string

@description('Fabric workspace GUID for OneLake bronze writes')
@minLength(36)
param onelakeWorkspaceId string

@description('Fabric Lakehouse name or item GUID for OneLake bronze writes')
@minLength(1)
param onelakeLakehouseName string

@description('Fabric Warehouse SQL endpoint used by daily promotion')
@minLength(1)
param fabricWarehouseServer string

@description('Fabric Warehouse database used by daily promotion')
param fabricWarehouseDatabase string = 'auspex_gold'

@description('Application client ID for the Microsoft personal-account SWA auth registration')
@minLength(1)
param microsoftAuthClientId string

@description('Git repository URL used by Static Web Apps')
@minLength(1)
param repositoryUrl string

@description('Git branch used by Static Web Apps')
@minLength(1)
param repositoryBranch string = 'main'

@secure()
@description('Client secret for the Microsoft personal-account SWA auth registration')
@minLength(1)
param microsoftAuthClientSecret string

// ---------------------------------------------------------------------------
// Deterministic resource names (no module output needed)
// ---------------------------------------------------------------------------

var cosmosAccountName = 'auspex-${env}-cosmos'
// Cosmos DB endpoint follows the predictable pattern:
var cosmosEndpoint = 'https://${cosmosAccountName}.documents.azure.com:443/'

// Fabric capacity name — alphanumeric only, no hyphens
var fabricCapacityName = 'auspex${env}fab'

var kvName = 'auspex-${env}-kv'
var aiSearchEndpoint = 'https://auspex-${env}-search.search.windows.net'
var azureOpenAiEndpoint = 'https://auspex-${env}-openai.openai.azure.com/'

// ---------------------------------------------------------------------------
// Resource groups
// ---------------------------------------------------------------------------

resource rgShared 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'auspex-${env}-shared'
  location: location
}

resource rgIngest 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'auspex-${env}-ingest'
  location: location
}

resource rgData 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'auspex-${env}-data'
  location: location
}

resource rgAi 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'auspex-${env}-ai'
  location: location
}

resource rgWeb 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'auspex-${env}-web'
  location: location
}

// ---------------------------------------------------------------------------
// Step 1b: VNet + subnets (shared RG) — must exist before Function Apps
// ---------------------------------------------------------------------------

module networkVnet 'modules/network-vnet.bicep' = {
  name: 'networkVnet'
  scope: rgShared
  params: {
    env: env
    location: location
  }
}

// ---------------------------------------------------------------------------
// Step 2: Observability (shared RG)
// ---------------------------------------------------------------------------

module monitor 'modules/monitor.bicep' = {
  name: 'monitor'
  scope: rgShared
  params: {
    env: env
    location: location
    retentionDays: logRetentionDays
    alertEmailAddress: alertEmailAddress
  }
}

// ---------------------------------------------------------------------------
// Step 3a: Ingestion Function App (ingest RG)
// Deployed before Key Vault and Cosmos so their RBAC assignments can reference
// this app's system-assigned managed identity principal ID.
// ---------------------------------------------------------------------------

module ingestFunc 'modules/functionapp.bicep' = {
  name: 'ingestFunc'
  scope: rgIngest
  params: {
    appName: 'auspex-${env}-func'
    location: location
    keyVaultName: kvName
    isIngestion: true
    cosmosEndpoint: cosmosEndpoint
    fabricCapacityName: fabricCapacityName
    fabricCapacityResourceGroup: rgData.name
    fabricSubscriptionId: subscription().subscriptionId
    onelakeWorkspaceId: onelakeWorkspaceId
    onelakeLakehouseName: onelakeLakehouseName
    fabricWarehouseServer: fabricWarehouseServer
    fabricWarehouseDatabase: fabricWarehouseDatabase
    aiSearchEndpoint: aiSearchEndpoint
    azureOpenAiEndpoint: azureOpenAiEndpoint
    vnetIntegrationSubnetId: networkVnet.outputs.ingestSubnetId
    logAnalyticsWorkspaceId: monitor.outputs.workspaceId
  }
}

// ---------------------------------------------------------------------------
// Step 3b: Web API Function App (web RG)
// ---------------------------------------------------------------------------

module webApiFunc 'modules/functionapp.bicep' = {
  name: 'webApiFunc'
  scope: rgWeb
  params: {
    appName: 'auspex-${env}-wapi'
    location: location
    keyVaultName: kvName
    isIngestion: false
    cosmosEndpoint: cosmosEndpoint
    aiSearchEndpoint: aiSearchEndpoint
    azureOpenAiEndpoint: azureOpenAiEndpoint
    vnetIntegrationSubnetId: networkVnet.outputs.wapiSubnetId
    logAnalyticsWorkspaceId: monitor.outputs.workspaceId
  }
}

// ---------------------------------------------------------------------------
// Step 3: Key Vault (shared RG)
// Needs both Function App principal IDs for RBAC assignments.
// Stores the App Insights connection string as a secret.
// ---------------------------------------------------------------------------

module keyVault 'modules/keyvault.bicep' = {
  name: 'keyVault'
  scope: rgShared
  params: {
    env: env
    location: location
    ingestFuncPrincipalId: ingestFunc.outputs.principalId
    webApiFuncPrincipalId: webApiFunc.outputs.principalId
    appInsightsConnectionString: monitor.outputs.appInsightsConnectionString
    logAnalyticsWorkspaceId: monitor.outputs.workspaceId
    edgarUserAgent: edgarUserAgent
    alphaVantageApiKey: alphaVantageApiKey
    finnhubApiKey: finnhubApiKey
  }
}

// ---------------------------------------------------------------------------
// Step 4: Cosmos DB (shared RG)
// Needs both Function App principal IDs for data-plane RBAC.
// ---------------------------------------------------------------------------

module cosmos 'modules/cosmos.bicep' = {
  name: 'cosmos'
  scope: rgShared
  params: {
    env: env
    location: location
    ingestFuncPrincipalId: ingestFunc.outputs.principalId
    webApiFuncPrincipalId: webApiFunc.outputs.principalId
    logAnalyticsWorkspaceId: monitor.outputs.workspaceId
  }
}

// ---------------------------------------------------------------------------
// Step 5: Fabric Capacity (data RG)
// Bicep owns the Azure Fabric capacity. Fabric workspace/lakehouse/items remain
// portal/Fabric Git managed because they are not ARM/Bicep resources.
// ---------------------------------------------------------------------------

module fabric 'modules/fabric.bicep' = {
  name: 'fabric'
  scope: rgData
  params: {
    env: env
    location: location
    fabricAdminUpn: fabricAdminUpn
    ingestFuncPrincipalId: ingestFunc.outputs.principalId
  }
}

// ---------------------------------------------------------------------------
// Step 6: AI Search (ai RG)
// Needs web API func principal ID for Search Index Data Reader RBAC.
// ---------------------------------------------------------------------------

module aiSearch 'modules/aisearch.bicep' = {
  name: 'aiSearch'
  scope: rgAi
  params: {
    env: env
    location: location
    webApiFuncPrincipalId: webApiFunc.outputs.principalId
    ingestFuncPrincipalId: ingestFunc.outputs.principalId
    logAnalyticsWorkspaceId: monitor.outputs.workspaceId
  }
}

// ---------------------------------------------------------------------------
// Step 7: Azure OpenAI (ai RG)
// No cross-dependencies on other module outputs.
// ---------------------------------------------------------------------------

module openAi 'modules/openai.bicep' = {
  name: 'openAi'
  scope: rgAi
  params: {
    env: env
    location: location
    logAnalyticsWorkspaceId: monitor.outputs.workspaceId
    ingestFuncPrincipalId: ingestFunc.outputs.principalId
    webApiFuncPrincipalId: webApiFunc.outputs.principalId
    searchPrincipalId: aiSearch.outputs.principalId
  }
}

// ---------------------------------------------------------------------------
// Step 3a: Static Web App (web RG)
// Location: westeurope — Switzerland North not supported for SWA.
// ---------------------------------------------------------------------------

module staticWebApp 'modules/staticwebapp.bicep' = {
  name: 'staticWebApp'
  scope: rgWeb
  params: {
    env: env
    swaLocation: 'westeurope'
    microsoftAuthClientId: microsoftAuthClientId
    microsoftAuthClientSecret: microsoftAuthClientSecret
    repositoryUrl: repositoryUrl
    branch: repositoryBranch
    webApiResourceId: webApiFunc.outputs.functionAppId
    webApiName: webApiFunc.outputs.functionAppName
    webApiLocation: location
  }
}

// ---------------------------------------------------------------------------
// Step 10: Network — private DNS zones + private endpoints (shared RG)
// Deployed last: requires resource IDs from Cosmos DB, Key Vault, and both
// Function App storage accounts. VNet ID comes from step 1b.
// ---------------------------------------------------------------------------

module network 'modules/network.bicep' = {
  name: 'network'
  scope: rgShared
  params: {
    env: env
    location: location
    vnetId: networkVnet.outputs.vnetId
    cosmosAccountId: cosmos.outputs.accountId
    kvId: keyVault.outputs.keyVaultId
    storageFuncId: ingestFunc.outputs.storageAccountId
    storageWapiId: webApiFunc.outputs.storageAccountId
  }
}

// ---------------------------------------------------------------------------
// Resource locks — CanNotDelete on all RGs in prod
// Must use modules: subscription-scope Bicep cannot deploy RG-scoped resources inline (BCP139).
// ---------------------------------------------------------------------------

module rgSharedLock 'modules/lock.bicep' = if (env == 'prod') {
  name: 'rgSharedLock'
  scope: rgShared
  params: { rgName: rgShared.name }
}

module rgIngestLock 'modules/lock.bicep' = if (env == 'prod') {
  name: 'rgIngestLock'
  scope: rgIngest
  params: { rgName: rgIngest.name }
}

module rgDataLock 'modules/lock.bicep' = if (env == 'prod') {
  name: 'rgDataLock'
  scope: rgData
  params: { rgName: rgData.name }
}

module rgAiLock 'modules/lock.bicep' = if (env == 'prod') {
  name: 'rgAiLock'
  scope: rgAi
  params: { rgName: rgAi.name }
}

module rgWebLock 'modules/lock.bicep' = if (env == 'prod') {
  name: 'rgWebLock'
  scope: rgWeb
  params: { rgName: rgWeb.name }
}

// ---------------------------------------------------------------------------
// Outputs — useful for CI/CD and post-deploy verification
// ---------------------------------------------------------------------------

output keyVaultName string = keyVault.outputs.keyVaultName
output keyVaultUri string = keyVault.outputs.keyVaultUri
output cosmosEndpoint string = cosmos.outputs.endpoint
output appInsightsConnectionString string = monitor.outputs.appInsightsConnectionString
output ingestFuncName string = ingestFunc.outputs.functionAppName
output webApiFuncName string = webApiFunc.outputs.functionAppName
output fabricCapacityName string = fabric.outputs.capacityName
output searchEndpoint string = aiSearch.outputs.searchEndpoint
output openAiEndpoint string = openAi.outputs.openAiEndpoint
output swaHostname string = staticWebApp.outputs.defaultHostname

 
