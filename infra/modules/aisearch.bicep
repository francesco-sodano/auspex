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
    authOptions: {
      // RBAC-only; disable API key auth for managed identity access.
      // Note: 'aadOrApiKey' allows both; 'rbac' disables API keys entirely.
      // Using aadOrApiKey for CI tooling compatibility; switch to rbac for hardening.
      aadOrApiKey: {
        aadAuthFailureMode: 'http401WithBearerChallenge'
      }
    }
    semanticSearch: 'free'
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
