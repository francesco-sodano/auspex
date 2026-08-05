// keyvault.bicep — Azure Key Vault for Auspex
// Region: Switzerland North — supported.
//
// Enabled-source credentials are supplied as secure parameters by the deployment.
// FMP-API-KEY remains optional because the FMP source is disabled by default.
//
// APPLICATIONINSIGHTS-CONNECTION-STRING is written here from the monitor module output
// so Function Apps can reference it as a Key Vault reference.

@description('Environment name (dev or prod)')
param env string

@description('Azure region')
param location string

@description('Principal ID of the ingestion Function App managed identity')
param ingestFuncPrincipalId string

@description('Principal ID of the web API Function App managed identity')
param webApiFuncPrincipalId string

@description('Application Insights connection string (stored as a secret for KV reference use)')
param appInsightsConnectionString string

@description('Log Analytics workspace resource ID for diagnostic settings')
param logAnalyticsWorkspaceId string

@secure()
@description('SEC EDGAR user agent')
param edgarUserAgent string

@secure()
@description('Alpha Vantage API key')
param alphaVantageApiKey string

@secure()
@description('Finnhub API key')
param finnhubApiKey string

var kvName = 'auspex-${env}-kv'

// Key Vault Secrets User role definition ID (built-in)
var kvSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: kvName
  location: location
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true
    // Function Apps reach Key Vault via private endpoint + private DNS zone.
    // AzureServices bypass retained for ARM operations (e.g., Bicep secret writes
    // during deployment from a Microsoft-hosted runner — a trusted service).
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Deny'
      virtualNetworkRules: []
    }
  }
}

// Store the App Insights connection string as a Key Vault secret
// so Function Apps can use a KV reference for it.
resource appInsightsSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'APPLICATIONINSIGHTS-CONNECTION-STRING'
  properties: {
    value: appInsightsConnectionString
  }
}

resource edgarUserAgentSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'EDGAR-USER-AGENT'
  properties: {
    value: edgarUserAgent
  }
}

resource alphaVantageSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'ALPHAVANTAGE-API-KEY'
  properties: {
    value: alphaVantageApiKey
  }
}

resource finnhubSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'FINNHUB-API-KEY'
  properties: {
    value: finnhubApiKey
  }
}

resource kvDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-${kvName}'
  scope: keyVault
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        category: 'AuditEvent'
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

// RBAC: ingestion Function App MI — Key Vault Secrets User
resource ingestFuncKvRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, ingestFuncPrincipalId, kvSecretsUserRoleId)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsUserRoleId)
    principalId: ingestFuncPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// RBAC: web API Function App MI — Key Vault Secrets User
resource webApiFuncKvRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, webApiFuncPrincipalId, kvSecretsUserRoleId)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsUserRoleId)
    principalId: webApiFuncPrincipalId
    principalType: 'ServicePrincipal'
  }
}

@description('Key Vault resource name')
output keyVaultName string = keyVault.name

@description('Key Vault URI')
output keyVaultUri string = keyVault.properties.vaultUri

@description('Key Vault resource ID')
output keyVaultId string = keyVault.id
