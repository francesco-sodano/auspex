param cosmosAccountName string
param storageAccountName string
param openAiAccountName string
param registryName string
param apiPrincipalId string
param pipelinePrincipalId string
param performancePrincipalId string

var storageBlobDataContributor = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var storageBlobDataReader = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
var openAiUser = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
var acrPull = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var cosmosDataContributor = '00000000-0000-0000-0000-000000000002'
var principalIds = [
  apiPrincipalId
  pipelinePrincipalId
  performancePrincipalId
]

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-08-15' existing = {
  name: cosmosAccountName
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource openAi 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: openAiAccountName
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource cosmosAssignments 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-08-15' = [
  for principalId in principalIds: {
    parent: cosmos
    name: guid(cosmos.id, principalId, cosmosDataContributor)
    properties: {
      principalId: principalId
      roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/${cosmosDataContributor}'
      scope: cosmos.id
    }
  }
]

resource pipelineStorageAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, pipelinePrincipalId, storageBlobDataContributor)
  scope: storage
  properties: {
    principalId: pipelinePrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributor)
  }
}

resource apiStorageAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, apiPrincipalId, storageBlobDataReader)
  scope: storage
  properties: {
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataReader)
  }
}

resource openAiAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for principalId in [
    apiPrincipalId
    pipelinePrincipalId
  ]: {
    name: guid(openAi.id, principalId, openAiUser)
    scope: openAi
    properties: {
      principalId: principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUser)
    }
  }
]

resource registryAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for principalId in principalIds: {
    name: guid(registry.id, principalId, acrPull)
    scope: registry
    properties: {
      principalId: principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPull)
    }
  }
]
