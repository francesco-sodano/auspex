targetScope = 'subscription'

@description('Short AZD environment name used in resource names and tags.')
@minLength(2)
@maxLength(16)
param environmentName string

@description('Primary Azure region.')
param location string

@description('Azure OpenAI region where the required GPT-4.1 models are available.')
param openAiLocation string

@description('Microsoft Entra tenant ID for the single-tenant SPA/API registration.')
param authTenantId string

@description('Microsoft Entra application (client) ID used by browser PKCE and API token validation.')
param authClientId string

@description('Microsoft Entra object ID of the single portfolio owner.')
@minLength(1)
param ownerProviderUserId string

@description('Descriptive SEC EDGAR user agent with a monitored contact address.')
@minLength(8)
param secEdgarUserAgent string

@description('Email address for Azure Monitor alerts and budget notifications.')
@minLength(3)
param alertEmailAddress string

@secure()
@description('Alpha Vantage API key stored in Key Vault during provisioning.')
param priceApiKey string = ''

@secure()
@description('Finnhub API key stored in Key Vault during provisioning.')
param newsApiKey string = ''

@description('Existing Key Vault name. Leave empty to create a tenant-local vault.')
param existingKeyVaultName string = ''

@description('Resource group of the existing Key Vault. Required only when existingKeyVaultName is set.')
param existingKeyVaultResourceGroup string = ''

@description('Existing Cosmos account containing app_users and portfolio_transactions. Leave empty to create one.')
param existingLedgerAccountName string = ''

@description('Resource group of the existing ledger Cosmos account.')
param existingLedgerResourceGroup string = ''

@description('Database containing app_users and portfolio_transactions.')
param ledgerDatabaseName string = 'auspex'

@description('Monthly Azure budget amount in the subscription billing currency.')
param monthlyBudgetAmount string = '165'

@description('GPT-4.1-mini Global Standard capacity in thousands of tokens per minute.')
param extractionModelCapacity string = '200'

@description('GPT-4.1 Global Standard capacity in thousands of tokens per minute.')
param narrativeModelCapacity string = '30'

@description('Placeholder image. azd replaces it on deployment.')
param containerImage string = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

var resourceGroupName = 'rg-auspex-${environmentName}'
var suffix = uniqueString(subscription().id, environmentName)
var createsKeyVault = empty(existingKeyVaultName)
var keyVaultName = createsKeyVault ? 'kv-auspex-${suffix}' : existingKeyVaultName
var keyVaultResourceGroupName = createsKeyVault ? resourceGroupName : existingKeyVaultResourceGroup
var createsLedger = empty(existingLedgerAccountName)
var ledgerAccountName = createsLedger ? 'cosmos-auspex-ledger-${suffix}' : existingLedgerAccountName
var ledgerResourceGroupName = createsLedger ? resourceGroupName : existingLedgerResourceGroup
var loginEndpoint = environment().authentication.loginEndpoint
var authAuthority = '${loginEndpoint}${authTenantId}'
var authIssuer = '${loginEndpoint}${authTenantId}/v2.0'
var authJwksUrl = '${loginEndpoint}${authTenantId}/discovery/v2.0/keys'

resource resourceGroup 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: {
    application: 'auspex'
    environment: environmentName
    architecture: 'arc42'
    'azd-env-name': environmentName
  }
}

resource externalKeyVaultGroup 'Microsoft.Resources/resourceGroups@2024-03-01' existing = if (!createsKeyVault) {
  name: keyVaultResourceGroupName
}

resource externalLedgerGroup 'Microsoft.Resources/resourceGroups@2024-03-01' existing = if (!createsLedger) {
  name: ledgerResourceGroupName
}

module network 'modules/network.bicep' = {
  name: 'auspex-network'
  scope: resourceGroup
  params: {
    location: location
  }
}

module observability 'modules/observability.bicep' = {
  name: 'auspex-observability'
  scope: resourceGroup
  params: {
    location: location
    environmentName: environmentName
    alertEmailAddress: alertEmailAddress
    monthlyBudgetAmount: int(monthlyBudgetAmount)
  }
}

module registry 'modules/registry.bicep' = {
  name: 'auspex-registry'
  scope: resourceGroup
  params: {
    location: location
    registryName: 'crauspex${suffix}'
    environmentName: environmentName
  }
}

module data 'modules/data.bicep' = {
  name: 'auspex-data'
  scope: resourceGroup
  params: {
    location: location
    cosmosAccountName: 'cosmos-auspex-${suffix}'
    storageAccountName: 'stauspex${suffix}'
    logAnalyticsWorkspaceId: observability.outputs.workspaceId
  }
}

module openAi 'modules/openai.bicep' = {
  name: 'auspex-openai'
  scope: resourceGroup
  params: {
    location: openAiLocation
    accountName: 'aoai-auspex-${suffix}'
    logAnalyticsWorkspaceId: observability.outputs.workspaceId
    environmentName: environmentName
    extractionCapacity: int(extractionModelCapacity)
    narrativeCapacity: int(narrativeModelCapacity)
  }
}

module keyVaultCreate 'modules/keyvault.bicep' = if (createsKeyVault) {
  name: 'auspex-keyvault'
  scope: resourceGroup
  params: {
    location: location
    keyVaultName: keyVaultName
    environmentName: environmentName
  }
}

module keyVaultSecretsLocal 'modules/keyvault-secrets.bicep' = if (createsKeyVault) {
  name: 'auspex-keyvault-secrets'
  scope: resourceGroup
  params: {
    keyVaultName: keyVaultName
    priceApiKey: priceApiKey
    newsApiKey: newsApiKey
  }
  dependsOn: [
    keyVaultCreate
  ]
}

module keyVaultSecretsExternal 'modules/keyvault-secrets.bicep' = if (!createsKeyVault) {
  name: 'auspex-keyvault-secrets-existing'
  scope: externalKeyVaultGroup
  params: {
    keyVaultName: keyVaultName
    priceApiKey: priceApiKey
    newsApiKey: newsApiKey
  }
}

module ledgerCreate 'modules/ledger.bicep' = if (createsLedger) {
  name: 'auspex-ledger'
  scope: resourceGroup
  params: {
    location: location
    cosmosAccountName: ledgerAccountName
    databaseName: ledgerDatabaseName
    environmentName: environmentName
  }
}

module compute 'modules/containerapps.bicep' = {
  name: 'auspex-compute'
  scope: resourceGroup
  params: {
    location: location
    environmentName: environmentName
    infrastructureSubnetId: network.outputs.containerAppsSubnetId
    logAnalyticsCustomerId: observability.outputs.logAnalyticsCustomerId
    logAnalyticsSharedKey: observability.outputs.logAnalyticsSharedKey
    containerImage: containerImage
    registryServer: registry.outputs.loginServer
    cosmosEndpoint: data.outputs.cosmosEndpoint
    storageAccountUrl: data.outputs.storageAccountUrl
    keyVaultUri: 'https://${keyVaultName}${environment().suffixes.keyvaultDns}/'
    sourceLedgerCosmosEndpoint: 'https://${ledgerAccountName}.documents.azure.com:443/'
    sourceLedgerDatabaseName: ledgerDatabaseName
    openAiEndpoint: openAi.outputs.endpoint
    authClientId: authClientId
    authTenantId: authTenantId
    authAuthority: authAuthority
    authIssuer: authIssuer
    authJwksUrl: authJwksUrl
    ownerProviderUserId: ownerProviderUserId
    secEdgarUserAgent: secEdgarUserAgent
  }
  dependsOn: [
    keyVaultSecretsLocal
    keyVaultSecretsExternal
    ledgerCreate
  ]
}

module privateEndpoints 'modules/private-endpoints.bicep' = {
  name: 'auspex-private-endpoints'
  scope: resourceGroup
  params: {
    location: location
    vnetName: network.outputs.vnetName
    privateEndpointSubnetId: network.outputs.privateEndpointSubnetId
    cosmosAccountId: data.outputs.cosmosAccountId
    cosmosAccountName: data.outputs.cosmosAccountName
    storageAccountId: data.outputs.storageAccountId
    storageAccountName: data.outputs.storageAccountName
    openAiAccountId: openAi.outputs.accountId
    openAiAccountName: openAi.outputs.accountName
    keyVaultId: resourceId(
      subscription().subscriptionId,
      keyVaultResourceGroupName,
      'Microsoft.KeyVault/vaults',
      keyVaultName
    )
    keyVaultName: keyVaultName
    sourceLedgerAccountId: resourceId(
      subscription().subscriptionId,
      ledgerResourceGroupName,
      'Microsoft.DocumentDB/databaseAccounts',
      ledgerAccountName
    )
    sourceLedgerAccountName: ledgerAccountName
  }
  dependsOn: [
    keyVaultCreate
    ledgerCreate
  ]
}

module resourceRbac 'modules/rbac.bicep' = {
  name: 'auspex-resource-rbac'
  scope: resourceGroup
  params: {
    cosmosAccountName: data.outputs.cosmosAccountName
    storageAccountName: data.outputs.storageAccountName
    openAiAccountName: openAi.outputs.accountName
    registryName: registry.outputs.registryName
    apiPrincipalId: compute.outputs.apiPrincipalId
    pipelinePrincipalId: compute.outputs.pipelinePrincipalId
    performancePrincipalId: compute.outputs.performancePrincipalId
  }
}

module keyVaultRbacLocal 'modules/keyvault-rbac.bicep' = if (createsKeyVault) {
  name: 'auspex-keyvault-rbac'
  scope: resourceGroup
  params: {
    keyVaultName: keyVaultName
    logAnalyticsWorkspaceId: observability.outputs.workspaceId
    pipelinePrincipalId: compute.outputs.pipelinePrincipalId
  }
  dependsOn: [
    keyVaultCreate
  ]
}

module keyVaultRbacExternal 'modules/keyvault-rbac.bicep' = if (!createsKeyVault) {
  name: 'auspex-keyvault-rbac-existing'
  scope: externalKeyVaultGroup
  params: {
    keyVaultName: keyVaultName
    logAnalyticsWorkspaceId: observability.outputs.workspaceId
    pipelinePrincipalId: compute.outputs.pipelinePrincipalId
  }
}

module sourceLedgerRbacLocal 'modules/source-ledger-rbac.bicep' = if (createsLedger) {
  name: 'auspex-source-ledger-rbac'
  scope: resourceGroup
  params: {
    cosmosAccountName: ledgerAccountName
    databaseName: ledgerDatabaseName
    containerNames: [
      'app_users'
      'portfolio_transactions'
    ]
    logAnalyticsWorkspaceId: observability.outputs.workspaceId
    readerPrincipalId: compute.outputs.pipelinePrincipalId
    performanceReaderPrincipalId: compute.outputs.performancePrincipalId
    writerPrincipalId: compute.outputs.apiPrincipalId
  }
  dependsOn: [
    ledgerCreate
  ]
}

module sourceLedgerRbacExternal 'modules/source-ledger-rbac.bicep' = if (!createsLedger) {
  name: 'auspex-source-ledger-rbac-existing'
  scope: externalLedgerGroup
  params: {
    cosmosAccountName: ledgerAccountName
    databaseName: ledgerDatabaseName
    containerNames: [
      'app_users'
      'portfolio_transactions'
    ]
    logAnalyticsWorkspaceId: observability.outputs.workspaceId
    readerPrincipalId: compute.outputs.pipelinePrincipalId
    performanceReaderPrincipalId: compute.outputs.performancePrincipalId
    writerPrincipalId: compute.outputs.apiPrincipalId
  }
}

output AZURE_LOCATION string = location
output AZURE_RESOURCE_GROUP string = resourceGroup.name
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = registry.outputs.loginServer
output SERVICE_API_NAME string = compute.outputs.apiName
output SERVICE_API_URI string = 'https://${compute.outputs.apiFqdn}'
output SERVICE_API_RESOURCE_GROUP_NAME string = resourceGroup.name
output SERVICE_PIPELINE_NAME string = compute.outputs.pipelineJobName
output SERVICE_PIPELINE_RESOURCE_GROUP_NAME string = resourceGroup.name
output SERVICE_PERFORMANCE_NAME string = compute.outputs.performanceJobName
output SERVICE_PERFORMANCE_RESOURCE_GROUP_NAME string = resourceGroup.name

output resourceGroupName string = resourceGroup.name
output registryName string = registry.outputs.registryName
output registryServer string = registry.outputs.loginServer
output apiName string = compute.outputs.apiName
output apiFqdn string = compute.outputs.apiFqdn
output pipelineJobName string = compute.outputs.pipelineJobName
output performanceJobName string = compute.outputs.performanceJobName
output cosmosAccountName string = data.outputs.cosmosAccountName
output ledgerAccountName string = ledgerAccountName
output storageAccountName string = data.outputs.storageAccountName
output openAiAccountName string = openAi.outputs.accountName
output keyVaultName string = keyVaultName
