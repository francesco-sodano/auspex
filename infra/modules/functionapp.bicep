// functionapp.bicep — Reusable module for Azure Functions (Flex Consumption plan)
// Used for both the ingestion Function App (auspex-{env}-func) and
// the web API Function App (auspex-{env}-wapi).
//
// Storage uses managed identity (no shared key). Deployment packages are uploaded
// to the 'deployments' blob container; the Function App reads them via its
// system-assigned MI (SystemAssignedIdentity auth — no shared key required).

@description('Function App resource name (e.g. auspex-prod-func)')
@minLength(3)
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

@description('Fabric capacity resource group (used in the scheduler; ingestion only)')
param fabricCapacityResourceGroup string = ''

@description('Azure subscription ID containing the Fabric capacity (ingestion only)')
param fabricSubscriptionId string = ''

@description('Fabric workspace GUID for OneLake bronze writes (ingestion only)')
param onelakeWorkspaceId string = ''

@description('Fabric Lakehouse name or item GUID for OneLake bronze writes (ingestion only)')
param onelakeLakehouseName string = 'auspex_bronze'

@description('Fabric Warehouse SQL endpoint (ingestion daily promotion only)')
param fabricWarehouseServer string = ''

@description('Fabric Warehouse database (ingestion daily promotion only)')
param fabricWarehouseDatabase string = 'auspex_gold'

@description('Azure AI Search endpoint used by E7')
param aiSearchEndpoint string

@description('Azure OpenAI endpoint used by E7 ingestion')
param azureOpenAiEndpoint string

@description('Alpha Vantage request cap in requests per minute (ingestion only)')
param alphaVantageRequestsPerMinute string = '75'

@description('Finnhub company-news maximum lookback in days for the free tier (ingestion only)')
param finnhubMaxLookbackDays string = '365'

@description('Allowed browser origins for HTTP-triggered functions')
@minLength(1)
param allowedOrigins array = [
  'https://portal.azure.com'
]

@description('Subnet resource ID for VNet integration')
param vnetIntegrationSubnetId string

@description('Log Analytics workspace resource ID for diagnostic settings')
param logAnalyticsWorkspaceId string

var storageAccountName = take(replace('${appName}st', '-', ''), 24)
var deploymentContainerName = 'deployments'

// ---------------------------------------------------------------------------
// Storage account for the Functions host (keyless — MI only)
// ---------------------------------------------------------------------------

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  #disable-next-line BCP334 // appName is a valid Function App name; changing this expression would replace host storage.
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

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' existing = {
  parent: storageAccount
  name: 'default'
}

resource queueService 'Microsoft.Storage/storageAccounts/queueServices@2023-05-01' existing = {
  parent: storageAccount
  name: 'default'
}

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' existing = {
  parent: storageAccount
  name: 'default'
}

resource storageAccountDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-${storageAccountName}'
  scope: storageAccount
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    metrics: [
      {
        category: 'Transaction'
        enabled: true
      }
    ]
  }
}

resource blobDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-${storageAccountName}-blob'
  scope: blobService
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        category: 'StorageRead'
        enabled: true
      }
      {
        category: 'StorageWrite'
        enabled: true
      }
      {
        category: 'StorageDelete'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'Transaction'
        enabled: true
      }
    ]
  }
}

resource queueDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-${storageAccountName}-queue'
  scope: queueService
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        category: 'StorageRead'
        enabled: true
      }
      {
        category: 'StorageWrite'
        enabled: true
      }
      {
        category: 'StorageDelete'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'Transaction'
        enabled: true
      }
    ]
  }
}

resource tableDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-${storageAccountName}-table'
  scope: tableService
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        category: 'StorageRead'
        enabled: true
      }
      {
        category: 'StorageWrite'
        enabled: true
      }
      {
        category: 'StorageDelete'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'Transaction'
        enabled: true
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Deployment blob container — Flex Consumption reads the code package from here.
// SystemAssignedIdentity auth means no shared key needed for deployment.
// ---------------------------------------------------------------------------

resource deploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: deploymentContainerName
  properties: {
    publicAccess: 'None'
  }
}

// ---------------------------------------------------------------------------
// Flex Consumption hosting plan
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// App settings
// ---------------------------------------------------------------------------

var baseAppSettings = [
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
    name: 'AV_RPM'
    value: alphaVantageRequestsPerMinute
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
    name: 'FINNHUB_MAX_LOOKBACK_DAYS'
    value: finnhubMaxLookbackDays
  }
  {
    name: 'ONELAKE_WORKSPACE_ID'
    value: onelakeWorkspaceId
  }
  {
    name: 'ONELAKE_LAKEHOUSE_NAME'
    value: onelakeLakehouseName
  }
  {
    name: 'FABRIC_CAPACITY_NAME'
    value: fabricCapacityName
  }
  {
    name: 'FABRIC_CAPACITY_RESOURCE_GROUP'
    value: fabricCapacityResourceGroup
  }
  {
    name: 'AZURE_SUBSCRIPTION_ID'
    value: fabricSubscriptionId
  }
  {
    name: 'FABRIC_WORKSPACE_ID'
    value: onelakeWorkspaceId
  }
  {
    name: 'FABRIC_DAILY_PIPELINE_NAME'
    value: 'auspex_daily_build'
  }
  {
    name: 'FABRIC_DAILY_PUBLISH_PIPELINE_NAME'
    value: 'auspex_daily_publish'
  }
  {
    name: 'FABRIC_WAREHOUSE_SERVER'
    value: fabricWarehouseServer
  }
  {
    name: 'FABRIC_WAREHOUSE_DATABASE'
    value: fabricWarehouseDatabase
  }
  {
    name: 'DAILY_BUILD_SCHEDULE'
    value: '0 0 1 * * *'
  }
  {
    name: 'DAILY_BUILD_POLL_SECONDS'
    value: '30'
  }
  {
    name: 'DAILY_BUILD_NARRATIVE_PAGE_SIZE'
    value: '20'
  }
  {
    name: 'DAILY_BUILD_NARRATIVE_MAX_WORKERS'
    value: '2'
  }
  {
    name: 'INGESTION_UNIVERSE_CONTAINER'
    value: 'ingestion_universe'
  }
  {
    name: 'AI_SEARCH_ENDPOINT'
    value: aiSearchEndpoint
  }
  {
    name: 'AI_SEARCH_EVIDENCE_INDEX'
    value: 'idx-news-filings'
  }
  {
    name: 'AZURE_OPENAI_ENDPOINT'
    value: azureOpenAiEndpoint
  }
  {
    name: 'AZURE_OPENAI_EMBEDDING_DEPLOYMENT'
    value: 'text-embedding-3-large'
  }
  {
    name: 'AZURE_OPENAI_CHAT_DEPLOYMENT'
    value: 'gpt-4o'
  }
  {
    name: 'AZURE_OPENAI_CHAT_MODEL_VERSION'
    value: 'gpt-4o:2024-11-20'
  }
  {
    name: 'SENTIMENT_CACHE_CONTAINER'
    value: 'sentiment_cache'
  }
  {
    name: 'NARRATIVE_FEATURE_CACHE_CONTAINER'
    value: 'narrative_feature_cache'
  }
] : []

var webApiExtraSettings = !isIngestion ? [
  {
    name: 'COSMOS_DATABASE_NAME'
    value: 'auspex'
  }
  {
    name: 'APP_USERS_CONTAINER'
    value: 'app_users'
  }
  {
    name: 'PORTFOLIO_TRANSACTIONS_CONTAINER'
    value: 'portfolio_transactions'
  }
  {
    name: 'SECURITY_CATALOG_CONTAINER'
    value: 'security_catalog'
  }
  {
    name: 'MARKET_DATA_CONTAINER'
    value: 'market_data'
  }
  {
    name: 'INGESTION_UNIVERSE_CONTAINER'
    value: 'ingestion_universe'
  }
  {
    name: 'AI_SEARCH_ENDPOINT'
    value: aiSearchEndpoint
  }
  {
    name: 'AI_SEARCH_EVIDENCE_INDEX'
    value: 'idx-news-filings'
  }
  {
    name: 'AZURE_OPENAI_ENDPOINT'
    value: azureOpenAiEndpoint
  }
  {
    name: 'AZURE_OPENAI_CHAT_DEPLOYMENT'
    value: 'gpt-4o'
  }
  {
    name: 'AZURE_OPENAI_CHAT_MODEL_VERSION'
    value: 'gpt-4o:2024-11-20'
  }
  {
    name: 'DECISION_LOG_CONTAINER'
    value: 'decision_log'
  }
] : []

var appSettings = concat(baseAppSettings, ingestionExtraSettings, webApiExtraSettings)

// ---------------------------------------------------------------------------
// Function App (Flex Consumption, Linux, Python 3.12)
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
      http20Enabled: true
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      cors: {
        allowedOrigins: allowedOrigins
        supportCredentials: false
      }
    }
    httpsOnly: true
    publicNetworkAccess: 'Enabled'
    // VNet integration — all outbound traffic routes through the VNet,
    // enabling the Function App to reach private endpoints for Cosmos DB and Key Vault.
    virtualNetworkSubnetId: vnetIntegrationSubnetId
    vnetRouteAllEnabled: true
    // Flex Consumption: runtime + deployment config.
    // deploymentContainer.name reference creates implicit dependency so the
    // container exists before the Function App configures it.
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storageAccount.properties.primaryEndpoints.blob}${deploymentContainer.name}'
          authentication: {
            type: 'SystemAssignedIdentity'
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: isIngestion ? 2 : 100
        instanceMemoryMB: 2048
        alwaysReady: isIngestion ? [
          {
            name: 'durable'
            instanceCount: 1
          }
        ] : []
      }
      runtime: {
        name: 'python'
        version: '3.12'
      }
    }
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
