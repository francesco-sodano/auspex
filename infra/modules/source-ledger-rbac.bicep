param cosmosAccountName string
param databaseName string
param containerNames array
param logAnalyticsWorkspaceId string
param readerPrincipalId string
param performanceReaderPrincipalId string
param writerPrincipalId string

var cosmosDataReader = '00000000-0000-0000-0000-000000000001'
var cosmosDataContributor = '00000000-0000-0000-0000-000000000002'

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-08-15' existing = {
  name: cosmosAccountName
}

resource readerAssignments 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-08-15' = [
  for containerName in containerNames: {
    parent: cosmos
    name: guid(cosmos.id, databaseName, containerName, readerPrincipalId, cosmosDataReader)
    properties: {
      principalId: readerPrincipalId
      roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/${cosmosDataReader}'
      scope: '${cosmos.id}/dbs/${databaseName}/colls/${containerName}'
    }
  }
]

resource performanceReaderAssignments 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-08-15' = [
  for containerName in containerNames: {
    parent: cosmos
    name: guid(cosmos.id, databaseName, containerName, performanceReaderPrincipalId, cosmosDataReader)
    properties: {
      principalId: performanceReaderPrincipalId
      roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/${cosmosDataReader}'
      scope: '${cosmos.id}/dbs/${databaseName}/colls/${containerName}'
    }
  }
]

resource apiUserReader 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-08-15' = {
  parent: cosmos
  name: guid(cosmos.id, databaseName, 'app_users', writerPrincipalId, cosmosDataReader)
  properties: {
    principalId: writerPrincipalId
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/${cosmosDataReader}'
    scope: '${cosmos.id}/dbs/${databaseName}/colls/app_users'
  }
}

resource apiTransactionWriter 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-08-15' = {
  parent: cosmos
  name: guid(cosmos.id, databaseName, 'portfolio_transactions', writerPrincipalId, cosmosDataContributor)
  properties: {
    principalId: writerPrincipalId
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/${cosmosDataContributor}'
    scope: '${cosmos.id}/dbs/${databaseName}/colls/portfolio_transactions'
  }
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-auspex-ledger'
  scope: cosmos
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        category: 'DataPlaneRequests'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'Requests'
        enabled: true
      }
    ]
  }
}
