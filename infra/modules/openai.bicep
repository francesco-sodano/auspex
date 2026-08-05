// openai.bicep — Azure OpenAI account + model deployments
//
// REGION NOTE: Azure OpenAI availability in Switzerland North varies by model.
// As of 2025, Switzerland North supports a subset of models.
// Verify at: https://learn.microsoft.com/en-us/azure/ai-services/openai/concepts/models#model-summary-table-and-region-availability
// If a required model is unavailable in Switzerland North, the nearest fallback is
// Sweden Central (swedencentral) or West Europe (westeurope).
//
// Deployments:
//   text-embedding-3-large  — document/news/filing embeddings for AI Search
//   gpt-4o                  — agent reasoning, chat, sentiment scoring

@description('Environment name (dev or prod)')
param env string

@description('Azure region (switzerlandnorth preferred)')
param location string

@description('Log Analytics workspace resource ID for diagnostic settings')
param logAnalyticsWorkspaceId string

@description('Principal ID of the ingestion Function App managed identity')
param ingestFuncPrincipalId string

@description('Principal ID of the Web API Function App managed identity')
param webApiFuncPrincipalId string

@description('Principal ID of the Azure AI Search managed identity')
param searchPrincipalId string

var openAiName = 'auspex-${env}-openai'

// Cognitive Services OpenAI User — permits keyless inference calls.
var cognitiveServicesOpenAiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource openAiAccount 'Microsoft.CognitiveServices/accounts@2024-04-01-preview' = {
  name: openAiName
  location: location
  kind: 'OpenAI'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    customSubDomainName: openAiName
    publicNetworkAccess: 'Enabled'
    // Managed identity only — matches CognitiveServices_LocalAuth_Modify policy effect.
    // publicNetworkAccess remains Enabled; private endpoint hardening is deferred to E10.
    disableLocalAuth: true
    // Keyless authenticated public access is required until E10 provisions a
    // Cognitive Services private endpoint for the Function subnets.
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
      ipRules: []
      virtualNetworkRules: []
    }
  }
}

resource openAiDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-${openAiName}'
  scope: openAiAccount
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        category: 'RequestResponse'
        enabled: true
      }
      {
        category: 'Audit'
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

// text-embedding-3-large for generating document/filing/news chunk embeddings
// Capacity units are in thousands of tokens per minute (TPM / 1000).
// Adjust 'capacity' based on your Azure quota for this region.
resource embeddingDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-04-01-preview' = {
  parent: openAiAccount
  name: 'text-embedding-3-large'
  sku: {
    name: 'Standard'
    capacity: 350 // 350K TPM — required for the full daily evidence generation
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'text-embedding-3-large'
      version: '1'
      // NOTE: If 'text-embedding-3-large' is unavailable in your region,
      // fall back to 'text-embedding-ada-002' (3072-dim vs 1536-dim).
      // Update the AI Search index vector dimensions accordingly.
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

// gpt-4o for agent reasoning, grounded chat, and sentiment scoring
resource gpt4oDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-04-01-preview' = {
  parent: openAiAccount
  name: 'gpt-4o'
  dependsOn: [embeddingDeployment] // deployments must be sequential in the same account
  sku: {
    name: 'Standard'
    capacity: 10 // 10K TPM — adjust based on daily usage; batch sentiment can be higher
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-4o'
      version: '2024-11-20'
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

// RBAC: ingestion Function App MI — Cognitive Services OpenAI User
resource ingestFuncOpenAiRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(openAiAccount.id, ingestFuncPrincipalId, cognitiveServicesOpenAiUserRoleId)
  scope: openAiAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesOpenAiUserRoleId)
    principalId: ingestFuncPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource webApiFuncOpenAiRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(openAiAccount.id, webApiFuncPrincipalId, cognitiveServicesOpenAiUserRoleId)
  scope: openAiAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesOpenAiUserRoleId)
    principalId: webApiFuncPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// RBAC: Search MI — Cognitive Services OpenAI User for query-time vectorization.
resource searchOpenAiRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(openAiAccount.id, searchPrincipalId, cognitiveServicesOpenAiUserRoleId)
  scope: openAiAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesOpenAiUserRoleId)
    principalId: searchPrincipalId
    principalType: 'ServicePrincipal'
  }
}

@description('Azure OpenAI account name')
output openAiName string = openAiAccount.name

@description('Azure OpenAI endpoint')
output openAiEndpoint string = openAiAccount.properties.endpoint

@description('Azure OpenAI resource ID')
output openAiId string = openAiAccount.id

@description('Embedding deployment name')
output embeddingDeploymentName string = embeddingDeployment.name

@description('GPT-4o deployment name')
output gpt4oDeploymentName string = gpt4oDeployment.name

@description('Azure OpenAI system-assigned MI principal ID')
output principalId string = openAiAccount.identity.principalId
