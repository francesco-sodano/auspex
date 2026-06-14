// functionapp.bicep — Reusable module for Azure Functions (Flex Consumption plan)
// Used for both the ingestion Function App (auspex-{env}-func) and
// the web API Function App (auspex-{env}-wapi).
//
// Region: Switzerland North — Flex Consumption is supported.
//
// Storage Account for the Functions host is created in the same resource group.
// All secrets are wired as Key Vault references — no plaintext values in app settings.

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

var storageAccountName = replace('${appName}st', '-', '')

// Flex Consumption plan
resource hostingPlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: '${appName}-plan'
  location: location
  kind: 'functionapp'
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true // Linux
  }
}

// Storage account for the Functions host (blobs, queues, tables)
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
  }
}

// Base app settings — common to both ingestion and web API
var baseAppSettings = [
  {
    name: 'FUNCTIONS_WORKER_RUNTIME'
    value: 'python'
  }
  {
    name: 'PYTHON_VERSION'
    value: '3.11'
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
]

// Additional settings for the ingestion Function App only
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
] : []

var appSettings = concat(baseAppSettings, ingestionExtraSettings)

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
      linuxFxVersion: 'Python|3.11'
      http20Enabled: true
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
    }
    httpsOnly: true
    publicNetworkAccess: 'Enabled'
  }
}

// Grant the Function App's managed identity Storage Blob Data Owner on its own
// storage account so Flex Consumption can manage its host storage without shared keys.
var storageBlobDataOwnerRoleId = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'

resource storageRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, functionApp.id, storageBlobDataOwnerRoleId)
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataOwnerRoleId)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Also grant Storage Queue Data Contributor and Table Data Contributor
// for Functions runtime internal queues and tables.
var storageQueueContributorRoleId = '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
var storageTableContributorRoleId = '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'

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

@description('Function App resource name')
output functionAppName string = functionApp.name

@description('System-assigned managed identity principal ID')
output principalId string = functionApp.identity.principalId

@description('Function App resource ID')
output functionAppId string = functionApp.id

@description('Storage account name for the Functions host')
output storageAccountName string = storageAccount.name
