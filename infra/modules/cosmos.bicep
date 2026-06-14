// cosmos.bicep — Cosmos DB serverless account (SQL API) for the Auspex control plane
// Region: Switzerland North — supported.
//
// Containers (all partition key /source_id):
//   sources    — source registry
//   watermarks — per-source watermark state
//   runs       — per-execution run log
//   dedup      — idempotency keys (TTL = 7 days)

@description('Environment name (dev or prod)')
param env string

@description('Azure region')
param location string

@description('Principal ID of the ingestion Function App managed identity')
param ingestFuncPrincipalId string

@description('Principal ID of the web API Function App managed identity')
param webApiFuncPrincipalId string

var accountName = 'auspex-${env}-cosmos'
var databaseName = 'auspex'

// Cosmos DB Built-in Data Contributor and Data Reader role IDs are data-plane
// roles defined on the Cosmos DB account itself (not ARM RBAC).
// They are assigned via sqlRoleAssignments below.
var cosmosDataContributorRoleId = '00000000-0000-0000-0000-000000000002'
var cosmosDataReaderRoleId = '00000000-0000-0000-0000-000000000001'

resource cosmosAccount 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' = {
  name: accountName
  location: location
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    capabilities: [
      {
        name: 'EnableServerless'
      }
    ]
    enableAutomaticFailover: false
    enableMultipleWriteLocations: false
    // Disable local auth — use managed identity only
    disableLocalAuth: false
    publicNetworkAccess: 'Enabled'
  }
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-05-15' = {
  parent: cosmosAccount
  name: databaseName
  properties: {
    resource: {
      id: databaseName
    }
  }
}

resource sourcesContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: database
  name: 'sources'
  properties: {
    resource: {
      id: 'sources'
      partitionKey: {
        paths: ['/source_id']
        kind: 'Hash'
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
      }
    }
  }
}

resource watermarksContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: database
  name: 'watermarks'
  properties: {
    resource: {
      id: 'watermarks'
      partitionKey: {
        paths: ['/source_id']
        kind: 'Hash'
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
      }
    }
  }
}

resource runsContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: database
  name: 'runs'
  properties: {
    resource: {
      id: 'runs'
      partitionKey: {
        paths: ['/source_id']
        kind: 'Hash'
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
      }
    }
  }
}

// dedup container — TTL = 7 days (604800 seconds)
resource dedupContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: database
  name: 'dedup'
  properties: {
    resource: {
      id: 'dedup'
      partitionKey: {
        paths: ['/source_id']
        kind: 'Hash'
      }
      defaultTtl: 604800
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
      }
    }
  }
}

// Data-plane RBAC: ingestion Function App MI — Cosmos DB Built-in Data Contributor
resource ingestFuncCosmosRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: cosmosAccount
  name: guid(cosmosAccount.id, ingestFuncPrincipalId, cosmosDataContributorRoleId)
  properties: {
    roleDefinitionId: '${cosmosAccount.id}/sqlRoleDefinitions/${cosmosDataContributorRoleId}'
    principalId: ingestFuncPrincipalId
    scope: cosmosAccount.id
  }
}

// Data-plane RBAC: web API Function App MI — Cosmos DB Built-in Data Reader
resource webApiFuncCosmosRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: cosmosAccount
  name: guid(cosmosAccount.id, webApiFuncPrincipalId, cosmosDataReaderRoleId)
  properties: {
    roleDefinitionId: '${cosmosAccount.id}/sqlRoleDefinitions/${cosmosDataReaderRoleId}'
    principalId: webApiFuncPrincipalId
    scope: cosmosAccount.id
  }
}

@description('Cosmos DB account name')
output accountName string = cosmosAccount.name

@description('Cosmos DB account endpoint')
output endpoint string = cosmosAccount.properties.documentEndpoint

@description('Cosmos DB account resource ID')
output accountId string = cosmosAccount.id
