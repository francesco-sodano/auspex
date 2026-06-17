// functionapp.bicep — Reusable module for Azure Functions (Consumption Y1 plan)
// Used for both the ingestion Function App (auspex-{env}-func) and
// the web API Function App (auspex-{env}-wapi).
//
// Storage uses managed identity (no shared key). Policy WEBSITE_RUN_FROM_PACKAGE
// deployment via portal blob upload does not require shared key access.

@description('Function App resource name (e.g. auspex-prod-func)')
param appName string

@description('Azure region')
param location string

@description('Key Vault name (used to build KV reference strings)')
param keyVaultName string

@description('Application Insights connection string Key Vault secret name')
param appInsightsKvSecretName string = 'APPLICATIONINSIGHTS-CONNECTION-STRING'

@description('Set to true for the ingestion Function App — adds API key KV references.')
param isIngestion bool

@description('Cosmos DB endpoint (used as an app setting on both apps)')
param cosmosEndpoint string

@description('Fabric capacity name (used in the scheduler; ingestion only)')
param fabricCapacityName string = ''

@description('Fabric workspace GUID for OneLake bronze writes (ingestion only)')
param onelakeWorkspaceId string = ''

@description('Fabric Lakehouse name for bronze layer (ingestion only)')
param onelakeLakehouseName string = 'auspex_bronze'

@description('Subnet resource ID for VNet integration')
param vnetIntegrationSubnetId string

@description('Log Analytics workspace resource ID for diagnostic settings')
param logAnalyticsWorkspaceId string

var storageAccountName = take(replace('${appName}st', '-', ''), 24)

// ---------------------------------------------------------------------------
// Storage account for the Functions host (keyless — MI only)
// ---------------------------------------------------------------------------

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
    // Shared key disabled — matches StorageAccount_DisableLocalAuth_Modify policy.
    // Function App runtime uses MI via AzureWebJobsStorage__accountName + credential=managedidentity.
    allowSharedKeyAccess: false
    // Trusted service bypass allows the Functions runtime and Azure Monitor to reach storage.
    // Private endpoint (created in network.bicep) handles data-plane access from within the VNet.
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices, Logging, Metrics'
    }
  }
}

// ---------------------------------------------------------------------------
// Consumption hosting plan
// ---------------------------------------------------------------------------

resource hostingPlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: '${appName}-plan'
  location: location
  kind: 'functionapp'
  sku: {
    name: 'Y1'
    tier: 'Dynamic'
  }
  properties: {
    reserved: true // Linux
  }
}

// ---------------------------------------------------------------------------
// App settings
// ---------------------------------------------------------------------------

var baseAppSettings = [
  {
    name: 'FUNCTIONS_WORKER_RUNTIME'
    value: 'python'
  }
  {
    name: 'FUNCTIONS_EXTENSION_VERSION'
    value: '~4'
  }
  {
    name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
    value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=${appInsightsKvSecretName})'
  }
  {
    name: 'COSMOS_ENDPOINT'
    value: cosmosEndpoint
  }
  {
    name: 'AzureWebJobsStorage__accountName'
    value: storageAccountName
  }
  {
    name: 'AzureWebJobsStorage__credential'
    value: 'managedidentity'
  }
]

var ingestionExtraSettings = isIngestion ? [
  {
    name: 'EDGAR_USER_AGENT'
    value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=EDGAR-USER-AGENT)'
  }
  {
    name: 'ALPHAVANTAGE_API_KEY'
    value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=ALPHAVANTAGE-API-KEY)'
  }
  {
    name: 'FMP_API_KEY'
    value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=FMP-API-KEY)'
  }
  {
    name: 'FINNHUB_API_KEY'
    value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=FINNHUB-API-KEY)'
  }
  {
    name: 'FABRIC_CAPACITY_NAME'
    value: fabricCapacityName
  }
  {
    name: 'ONELAKE_WORKSPACE_ID'
    value: onelakeWorkspaceId
  }
  {
    name: 'ONELAKE_LAKEHOUSE_NAME'
    value: onelakeLakehouseName
  }
] : []

var appSettings = concat(baseAppSettings, ingestionExtraSettings)

// ---------------------------------------------------------------------------
// Function App (Consumption Y1, Linux, Python 3.12)
// ---------------------------------------------------------------------------

resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: appName
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: hostingPlan.id
    siteConfig: {
      appSettings: appSettings
      linuxFxVersion: 'Python|3.12'
      http20Enabled: true
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
    }
    httpsOnly: true
    publicNetworkAccess: 'Enabled'
    // Regional VNet integration — all outbound traffic routes through the VNet,
    // enabling the Function App to reach private endpoints for Cosmos DB and Key Vault.
    virtualNetworkSubnetId: vnetIntegrationSubnetId
    vnetRouteAllEnabled: true
  }
}

// ---------------------------------------------------------------------------
// Diagnostic settings — stream FunctionAppLogs to Log Analytics
// Fixes FunctionApps_DiagnosticSetting_Audit compliance finding.
// ---------------------------------------------------------------------------

resource diagnosticSettings 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-${appName}'
  scope: functionApp
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        category: 'FunctionAppLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// RBAC — Function App MI needs access to its own storage (keyless)
// ---------------------------------------------------------------------------

var storageBlobDataOwnerRoleId = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
var storageQueueContributorRoleId = '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
var storageTableContributorRoleId = '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'

resource storageRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, functionApp.id, storageBlobDataOwnerRoleId)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataOwnerRoleId)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource queueRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, functionApp.id, storageQueueContributorRoleId)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageQueueContributorRoleId)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource tableRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, functionApp.id, storageTableContributorRoleId)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageTableContributorRoleId)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------

@description('Function App resource name')
output functionAppName string = functionApp.name

@description('System-assigned managed identity principal ID')
output principalId string = functionApp.identity.principalId

@description('Function App resource ID')
output functionAppId string = functionApp.id

@description('Storage account name for the Functions host')
output storageAccountName string = storageAccount.name

@description('Storage account resource ID (used by network module for private endpoint)')
output storageAccountId string = storageAccount.id
