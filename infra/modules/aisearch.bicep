// aisearch.bicep — Azure AI Search
//
// REGION NOTE: Azure AI Search supports Switzerland North as of 2024.
// Verify availability at https://azure.microsoft.com/en-us/explore/global-infrastructure/products-by-region/
// before deploying. If Switzerland North is unavailable for your subscription,
// the nearest supported region is West Europe (westeurope).
//
// SKU: basic — the cheapest tier that supports vector search (semantic ranker).
// For production with larger indexes, consider 'standard' or 'standard2'.

@description('Environment name (dev or prod)')
param env string

@description('Azure region (switzerlandnorth preferred; fallback: westeurope)')
param location string

@description('Principal ID of the web API Function App managed identity')
param webApiFuncPrincipalId string

@description('Log Analytics workspace resource ID for diagnostic settings')
param logAnalyticsWorkspaceId string

var searchServiceName = 'auspex-${env}-search'

// Search Index Data Reader — allows querying indexes (not management)
var searchIndexDataReaderRoleId = '1407120a-92aa-4202-b7e9-c0e197c71c8f'

resource searchService 'Microsoft.Search/searchServices@2024-03-01-preview' = {
  name: searchServiceName
  location: location
  sku: {
    name: 'basic'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'default'
    publicNetworkAccess: 'Enabled'
    // Managed identity only — disableLocalAuth: true disables API key auth entirely.
    // authOptions is incompatible with disableLocalAuth and has been removed.
    disableLocalAuth: true
    semanticSearch: 'free'
  }
}

resource searchDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-${searchServiceName}'
  scope: searchService
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        category: 'OperationLogs'
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

// RBAC: web API Function App MI — Search Index Data Reader
resource webApiFuncSearchRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(searchService.id, webApiFuncPrincipalId, searchIndexDataReaderRoleId)
  scope: searchService
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', searchIndexDataReaderRoleId)
    principalId: webApiFuncPrincipalId
    principalType: 'ServicePrincipal'
  }
}

@description('AI Search service name')
output searchServiceName string = searchService.name

@description('AI Search service endpoint')
output searchEndpoint string = 'https://${searchService.name}.search.windows.net'

@description('AI Search resource ID')
output searchId string = searchService.id

@description('AI Search system-assigned MI principal ID (for granting access to OpenAI)')
output principalId string = searchService.identity.principalId
