targetScope = 'subscription'

@description('Short AZD environment name used in resource names and tags.')
@minLength(2)
@maxLength(16)
param environmentName string

@description('Reuse the pre-4.1 production resource names during an in-place upgrade. Leave false for all fresh deployments.')
param preserveLegacyResourceNames bool = false

@description('Primary Azure region.')
param location string

@description('Azure OpenAI region where the required GPT-4.1 models are available.')
param openAiLocation string

@description('Microsoft Entra tenant ID for the SPA/API registration.')
param authTenantId string

@description('Microsoft Entra application (client) ID used by browser PKCE and API token validation.')
param authClientId string

@description('Tenant configuration: "workforce" for an organisational tenant (login.microsoftonline.com), or "external" for a Microsoft Entra External ID tenant (<subdomain>.ciamlogin.com), which is what allows sign-up with personal Gmail/Outlook addresses through a sign-up/sign-in user flow.')
@allowed([
  'workforce'
  'external'
])
param authTenantType string = 'workforce'

@description('External tenant subdomain, i.e. the "contoso" in contoso.ciamlogin.com. Required when authTenantType is "external"; ignored otherwise.')
param authTenantSubdomain string = ''

@description('Explicit authority URL. Leave empty to derive it from the tenant type, id and subdomain.')
param authAuthorityOverride string = ''

@description('Explicit token issuer (the "iss" claim). Leave empty to derive it. An external tenant may legitimately issue either the tenant-id or the .onmicrosoft.com authority form, so set this if token validation reports an untrusted issuer.')
param authIssuerOverride string = ''

@description('Explicit JWKS URL. Leave empty to derive it.')
param authJwksUrlOverride string = ''

@description('OpenID Connect metadata URL. When set, the API reads the authoritative issuer and jwks_uri from the tenant itself, which removes any possibility of misconfiguring them. Leave empty to derive it from the authority.')
param authOpenIdConfigurationUrlOverride string = ''

@description('Optional API scope the SPA requests, e.g. api://<client-id>/Auspex.Access. Empty uses the default scope.')
param authApiScope string = ''

@description('Issuer of the tenant being migrated away from. Set together with authLegacyJwksUrl during a tenant cutover so existing users are not locked out; clear both once everyone has re-authenticated.')
param authLegacyIssuer string = ''

@description('JWKS URL of the tenant being migrated away from. See authLegacyIssuer.')
param authLegacyJwksUrl string = ''

@description('Application audience/client ID used by tokens from the legacy issuer. Required with authLegacyIssuer and authLegacyJwksUrl.')
param authLegacyAudience string = ''

@description('Microsoft Entra object ID of the pre-existing portfolio owner, whose ledger partition is preserved across the multi-user migration.')
param ownerProviderUserId string = ''

@description('Object ID carried by the pre-existing owner token in the legacy tenant. During a cutover, only this principal aliases to ownerProviderUserId.')
param ownerLegacyProviderUserId string = ''

@description('Optional pre-existing owner_user_sk ledger partition to preserve during the multi-user migration.')
param ownerLedgerPartitionKey string = ''

@description('Email address of the first administrator. Consulted only until an administrator exists; authority then binds to that principal\'s immutable Entra object ID.')
@minLength(3)
param initialAdminEmail string

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

@description('Existing primary Auspex Cosmos account name for an in-place upgrade. Leave empty to create/use the environment-derived name.')
param primaryCosmosAccountNameOverride string = ''

@description('Existing Auspex blob storage account name for an in-place upgrade. Leave empty to create/use the environment-derived name.')
param storageAccountNameOverride string = ''

@description('Existing Auspex container registry name for an in-place upgrade. Leave empty to create/use the environment-derived name.')
param registryNameOverride string = ''

@description('Existing Auspex Azure OpenAI account name for an in-place upgrade. Leave empty to create/use the environment-derived name.')
param openAiAccountNameOverride string = ''

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
var primaryCosmosAccountName = empty(primaryCosmosAccountNameOverride)
  ? 'cosmos-auspex-${suffix}'
  : primaryCosmosAccountNameOverride
var storageAccountName = empty(storageAccountNameOverride) ? 'stauspex${suffix}' : storageAccountNameOverride
var registryName = empty(registryNameOverride) ? 'crauspex${suffix}' : registryNameOverride
var openAiAccountName = empty(openAiAccountNameOverride) ? 'aoai-auspex-${suffix}' : openAiAccountNameOverride
// Authority/issuer/JWKS derivation.
//
// A *workforce* tenant is served by the cloud's standard login endpoint. An
// *external* tenant (Microsoft Entra External ID) is served by its own
// `<subdomain>.ciamlogin.com` host, and MSAL will refuse that host unless the
// SPA declares it in `knownAuthorities` — hence `authKnownAuthority`.
//
// Every derived value can be overridden, because the two tenant types disagree
// about the exact issuer string and an external tenant may issue either the
// tenant-id or the .onmicrosoft.com authority form. The API additionally reads
// the authoritative issuer/jwks_uri from the OpenID metadata document at
// runtime, so the derivation below only has to be close enough to locate that
// document.
var loginEndpoint = environment().authentication.loginEndpoint
var isExternalTenant = authTenantType == 'external'
var ciamHost = '${authTenantSubdomain}.ciamlogin.com'
var derivedAuthority = isExternalTenant
  ? 'https://${ciamHost}/${authTenantId}'
  : '${loginEndpoint}${authTenantId}'
var authAuthority = empty(authAuthorityOverride) ? derivedAuthority : authAuthorityOverride
var authIssuer = empty(authIssuerOverride) ? '${authAuthority}/v2.0' : authIssuerOverride
var authJwksUrl = empty(authJwksUrlOverride)
  ? '${authAuthority}/discovery/v2.0/keys'
  : authJwksUrlOverride
var authOpenIdConfigurationUrl = empty(authOpenIdConfigurationUrlOverride)
  ? '${authAuthority}/v2.0/.well-known/openid-configuration'
  : authOpenIdConfigurationUrlOverride
var authKnownAuthority = isExternalTenant ? ciamHost : ''

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
    preserveLegacyResourceNames: preserveLegacyResourceNames
    alertEmailAddress: alertEmailAddress
    monthlyBudgetAmount: int(monthlyBudgetAmount)
  }
}

module registry 'modules/registry.bicep' = {
  name: 'auspex-registry'
  scope: resourceGroup
  params: {
    location: location
    registryName: registryName
    environmentName: environmentName
  }
}

module data 'modules/data.bicep' = {
  name: 'auspex-data'
  scope: resourceGroup
  params: {
    location: location
    cosmosAccountName: primaryCosmosAccountName
    storageAccountName: storageAccountName
    logAnalyticsWorkspaceId: observability.outputs.workspaceId
  }
}

module openAi 'modules/openai.bicep' = {
  name: 'auspex-openai'
  scope: resourceGroup
  params: {
    location: openAiLocation
    accountName: openAiAccountName
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
    preserveLegacyResourceNames: preserveLegacyResourceNames
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
    authOpenIdConfigurationUrl: authOpenIdConfigurationUrl
    authKnownAuthority: authKnownAuthority
    authApiScope: authApiScope
    authLegacyIssuer: authLegacyIssuer
    authLegacyJwksUrl: authLegacyJwksUrl
    authLegacyAudience: authLegacyAudience
    ownerProviderUserId: ownerProviderUserId
    ownerLegacyProviderUserId: ownerLegacyProviderUserId
    ownerLedgerPartitionKey: ownerLedgerPartitionKey
    initialAdminEmail: initialAdminEmail
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
