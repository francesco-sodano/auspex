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

var openAiName = 'auspex-${env}-openai'

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
    // AzureServices bypass required for Fabric Spark and Azure Monitor — tighten with private endpoint in E10.
    networkAcls: {
      defaultAction: 'Deny'
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
    capacity: 50 // 50K TPM — sufficient for daily batch embedding
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'text-embedding-3-large'
      // NOTE: If 'text-embedding-3-large' is unavailable in your region,
      // fall back to 'text-embedding-ada-002' (3072-dim vs 1536-dim).
      // Update the AI Search index vector dimensions accordingly.
    }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
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
    }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
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
