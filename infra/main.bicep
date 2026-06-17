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
//   3.  Ingestion Function App + Web API Function App (in parallel, after monitor + network-vnet)
//   4.  Key Vault — uses principal IDs for RBAC; stores App Insights connection string as secret
//   5.  Cosmos DB — uses principal IDs for data-plane RBAC
//   6.  Fabric Capacity — uses ingest func principal ID for Contributor RBAC (manual step)
//   7.  AI Search — uses web API func principal ID for Search Index Data Reader RBAC
//   8.  Azure OpenAI — no cross-dependencies
//   9.  Static Web App — no cross-dependencies
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

// fabricAdminUpn removed — Fabric capacity must be provisioned manually once
// Microsoft Fabric is enabled for the tenant (admin.microsoft.com → Settings →
// Org settings → Microsoft Fabric). See infra/modules/fabric.bicep for the
// Bicep definition to use when ready.

@description('Log Analytics retention in days (30 for dev, 90 for prod)')
param logRetentionDays int = 30

// ---------------------------------------------------------------------------
// Deterministic resource names (no module output needed)
// ---------------------------------------------------------------------------

var cosmosAccountName = 'auspex-${env}-cosmos'
// Cosmos DB endpoint follows the predictable pattern:
var cosmosEndpoint = 'https://${cosmosAccountName}.documents.azure.com:443/'

// Fabric capacity name — alphanumeric only, no hyphens
var fabricCapacityName = 'auspex${env}fab'

var kvName = 'auspex-${env}-kv'

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
  dependsOn: [monitor, networkVnet]
  params: {
    appName: 'auspex-${env}-func'
    location: location
    keyVaultName: kvName
    isIngestion: true
    cosmosEndpoint: cosmosEndpoint
    fabricCapacityName: fabricCapacityName
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
  dependsOn: [monitor, networkVnet]
  params: {
    appName: 'auspex-${env}-wapi'
    location: location
    keyVaultName: kvName
    isIngestion: false
    cosmosEndpoint: cosmosEndpoint
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
  }
}

// ---------------------------------------------------------------------------
// Step 5: Fabric Capacity (data RG) — MANUAL STEP
// Fabric capacity must be provisioned manually via the Azure portal once
// Microsoft Fabric is enabled for the tenant. The Bicep module is in
// infra/modules/fabric.bicep and can be re-added to this file when ready.
// The auspex-{env}-data resource group is created above and will hold it.
// ---------------------------------------------------------------------------

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
  }
}

// ---------------------------------------------------------------------------
// Step 8: Static Web App (web RG)
// Location: westeurope — Switzerland North not supported for SWA.
// No cross-dependencies on other module outputs.
// ---------------------------------------------------------------------------

module staticWebApp 'modules/staticwebapp.bicep' = {
  name: 'staticWebApp'
  scope: rgWeb
  params: {
    env: env
    swaLocation: 'westeurope'
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
// Outputs — useful for CI/CD and post-deploy verification
// ---------------------------------------------------------------------------

output keyVaultName string = keyVault.outputs.keyVaultName
output keyVaultUri string = keyVault.outputs.keyVaultUri
output cosmosEndpoint string = cosmos.outputs.endpoint
output appInsightsConnectionString string = monitor.outputs.appInsightsConnectionString
output ingestFuncName string = ingestFunc.outputs.functionAppName
output webApiFuncName string = webApiFunc.outputs.functionAppName
output searchEndpoint string = aiSearch.outputs.searchEndpoint
output openAiEndpoint string = openAi.outputs.openAiEndpoint
output swaHostname string = staticWebApp.outputs.defaultHostname

 
