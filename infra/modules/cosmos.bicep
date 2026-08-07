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

@description('Log Analytics workspace resource ID for diagnostic settings')
param logAnalyticsWorkspaceId string

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
    // Managed identity only — matches CosmosDB_LocalAuth_Modify policy effect
    disableLocalAuth: true
    minimalTlsVersion: 'Tls12'
    // Private endpoint is the only access path; public internet is blocked.
    // The private endpoint is created in network.bicep.
    publicNetworkAccess: 'Disabled'
    networkAclBypass: 'None'
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

resource sentimentCacheContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: database
  name: 'sentiment_cache'
  properties: {
    resource: {
      id: 'sentiment_cache'
      partitionKey: {
        paths: ['/id']
        kind: 'Hash'
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
      }
    }
  }
}

resource narrativeFeatureCacheContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: database
  name: 'narrative_feature_cache'
  properties: {
    resource: {
      id: 'narrative_feature_cache'
      partitionKey: {
        paths: ['/id']
        kind: 'Hash'
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
      }
    }
  }
}

resource dirtyCompanyEventsContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: database
  name: 'dirty_company_events'
  properties: {
    resource: {
      id: 'dirty_company_events'
      partitionKey: {
        paths: ['/security_sk']
        kind: 'Hash'
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
        compositeIndexes: [
          [
            {
              path: '/status'
              order: 'ascending'
            }
            {
              path: '/knowledge_date'
              order: 'ascending'
            }
            {
              path: '/id'
              order: 'ascending'
            }
          ]
        ]
      }
    }
  }
}

resource companyPackagesContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: database
  name: 'company_packages'
  properties: {
    resource: {
      id: 'company_packages'
      partitionKey: {
        paths: ['/security_sk']
        kind: 'Hash'
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
      }
    }
  }
}

var ingestionContainerNames = [
  'sources'
  'watermarks'
  'runs'
  'dedup'
  'sentiment_cache'
  'narrative_feature_cache'
  'dirty_company_events'
  'company_packages'
]

// Data-plane RBAC: ingestion Function App MI — contributor only on control-plane containers.
resource ingestFuncCosmosRoles 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = [for containerName in ingestionContainerNames: {
  parent: cosmosAccount
  name: guid(cosmosAccount.id, ingestFuncPrincipalId, cosmosDataContributorRoleId, containerName)
  properties: {
    roleDefinitionId: '${cosmosAccount.id}/sqlRoleDefinitions/${cosmosDataContributorRoleId}'
    principalId: ingestFuncPrincipalId
    scope: '${cosmosAccount.id}/dbs/${databaseName}/colls/${containerName}'
  }
  dependsOn: [
    sourcesContainer
    watermarksContainer
    runsContainer
    dedupContainer
    sentimentCacheContainer
    narrativeFeatureCacheContainer
    dirtyCompanyEventsContainer
    companyPackagesContainer
  ]
}]

resource appUsersContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: database
  name: 'app_users'
  properties: {
    resource: {
      id: 'app_users'
      partitionKey: {
        paths: ['/identity_key']
        kind: 'Hash'
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
      }
    }
  }
}

resource portfolioTransactionsContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: database
  name: 'portfolio_transactions'
  properties: {
    resource: {
      id: 'portfolio_transactions'
      partitionKey: {
        paths: ['/owner_user_sk']
        kind: 'Hash'
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
      }
    }
  }
}

resource decisionLogContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: database
  name: 'decision_log'
  properties: {
    resource: {
      id: 'decision_log'
      partitionKey: {
        paths: ['/owner_user_sk']
        kind: 'Hash'
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
      }
    }
  }
}

resource securityCatalogContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: database
  name: 'security_catalog'
  properties: {
    resource: {
      id: 'security_catalog'
      partitionKey: {
        paths: ['/id']
        kind: 'Hash'
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
      }
    }
  }
}

resource marketDataContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: database
  name: 'market_data'
  properties: {
    resource: {
      id: 'market_data'
      partitionKey: {
        paths: ['/id']
        kind: 'Hash'
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
      }
    }
  }
}

resource ingestionUniverseContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: database
  name: 'ingestion_universe'
  properties: {
    resource: {
      id: 'ingestion_universe'
      partitionKey: {
        paths: ['/id']
        kind: 'Hash'
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
      }
    }
  }
}

// Data-plane RBAC: web API Function App MI — contributor is required for
// first-authenticated-call app_user registration and later owner-scoped writes.
resource webApiFuncCosmosRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: cosmosAccount
  name: guid(cosmosAccount.id, webApiFuncPrincipalId, cosmosDataContributorRoleId)
  properties: {
    roleDefinitionId: '${cosmosAccount.id}/sqlRoleDefinitions/${cosmosDataContributorRoleId}'
    principalId: webApiFuncPrincipalId
    scope: '${cosmosAccount.id}/dbs/${databaseName}/colls/app_users'
  }
  dependsOn: [appUsersContainer]
}

resource webApiPortfolioTransactionsCosmosRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: cosmosAccount
  name: guid(cosmosAccount.id, webApiFuncPrincipalId, cosmosDataContributorRoleId, 'portfolio_transactions')
  properties: {
    roleDefinitionId: '${cosmosAccount.id}/sqlRoleDefinitions/${cosmosDataContributorRoleId}'
    principalId: webApiFuncPrincipalId
    scope: '${cosmosAccount.id}/dbs/${databaseName}/colls/portfolio_transactions'
  }
  dependsOn: [portfolioTransactionsContainer]
}

resource webApiDecisionLogCosmosRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: cosmosAccount
  name: guid(cosmosAccount.id, webApiFuncPrincipalId, cosmosDataContributorRoleId, 'decision_log')
  properties: {
    roleDefinitionId: '${cosmosAccount.id}/sqlRoleDefinitions/${cosmosDataContributorRoleId}'
    principalId: webApiFuncPrincipalId
    scope: '${cosmosAccount.id}/dbs/${databaseName}/colls/decision_log'
  }
  dependsOn: [decisionLogContainer]
}

resource webApiSecurityCatalogCosmosRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: cosmosAccount
  name: guid(cosmosAccount.id, webApiFuncPrincipalId, cosmosDataReaderRoleId, 'security_catalog')
  properties: {
    roleDefinitionId: '${cosmosAccount.id}/sqlRoleDefinitions/${cosmosDataReaderRoleId}'
    principalId: webApiFuncPrincipalId
    scope: '${cosmosAccount.id}/dbs/${databaseName}/colls/security_catalog'
  }
  dependsOn: [securityCatalogContainer]
}

resource webApiMarketDataCosmosRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: cosmosAccount
  name: guid(cosmosAccount.id, webApiFuncPrincipalId, cosmosDataReaderRoleId, 'market_data')
  properties: {
    roleDefinitionId: '${cosmosAccount.id}/sqlRoleDefinitions/${cosmosDataReaderRoleId}'
    principalId: webApiFuncPrincipalId
    scope: '${cosmosAccount.id}/dbs/${databaseName}/colls/market_data'
  }
  dependsOn: [marketDataContainer]
}

resource webApiIngestionUniverseCosmosRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: cosmosAccount
  name: guid(cosmosAccount.id, webApiFuncPrincipalId, cosmosDataContributorRoleId, 'ingestion_universe')
  properties: {
    roleDefinitionId: '${cosmosAccount.id}/sqlRoleDefinitions/${cosmosDataContributorRoleId}'
    principalId: webApiFuncPrincipalId
    scope: '${cosmosAccount.id}/dbs/${databaseName}/colls/ingestion_universe'
  }
  dependsOn: [ingestionUniverseContainer]
}

resource webApiCompanyPackagesCosmosRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: cosmosAccount
  name: guid(cosmosAccount.id, webApiFuncPrincipalId, cosmosDataReaderRoleId, 'company_packages')
  properties: {
    roleDefinitionId: '${cosmosAccount.id}/sqlRoleDefinitions/${cosmosDataReaderRoleId}'
    principalId: webApiFuncPrincipalId
    scope: '${cosmosAccount.id}/dbs/${databaseName}/colls/company_packages'
  }
  dependsOn: [companyPackagesContainer]
}

resource ingestFuncSecurityCatalogCosmosRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: cosmosAccount
  name: guid(cosmosAccount.id, ingestFuncPrincipalId, cosmosDataContributorRoleId, 'security_catalog')
  properties: {
    roleDefinitionId: '${cosmosAccount.id}/sqlRoleDefinitions/${cosmosDataContributorRoleId}'
    principalId: ingestFuncPrincipalId
    scope: '${cosmosAccount.id}/dbs/${databaseName}/colls/security_catalog'
  }
  dependsOn: [securityCatalogContainer]
}

resource ingestFuncMarketDataCosmosRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: cosmosAccount
  name: guid(cosmosAccount.id, ingestFuncPrincipalId, cosmosDataContributorRoleId, 'market_data')
  properties: {
    roleDefinitionId: '${cosmosAccount.id}/sqlRoleDefinitions/${cosmosDataContributorRoleId}'
    principalId: ingestFuncPrincipalId
    scope: '${cosmosAccount.id}/dbs/${databaseName}/colls/market_data'
  }
  dependsOn: [marketDataContainer]
}

resource ingestFuncIngestionUniverseCosmosRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: cosmosAccount
  name: guid(cosmosAccount.id, ingestFuncPrincipalId, cosmosDataReaderRoleId, 'ingestion_universe')
  properties: {
    roleDefinitionId: '${cosmosAccount.id}/sqlRoleDefinitions/${cosmosDataReaderRoleId}'
    principalId: ingestFuncPrincipalId
    scope: '${cosmosAccount.id}/dbs/${databaseName}/colls/ingestion_universe'
  }
  dependsOn: [ingestionUniverseContainer]
}

resource ingestFuncPortfolioTransactionsCosmosRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: cosmosAccount
  name: guid(cosmosAccount.id, ingestFuncPrincipalId, cosmosDataReaderRoleId, 'portfolio_transactions')
  properties: {
    roleDefinitionId: '${cosmosAccount.id}/sqlRoleDefinitions/${cosmosDataReaderRoleId}'
    principalId: ingestFuncPrincipalId
    scope: '${cosmosAccount.id}/dbs/${databaseName}/colls/portfolio_transactions'
  }
  dependsOn: [portfolioTransactionsContainer]
}

resource cosmosDiag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-${accountName}'
  scope: cosmosAccount
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        category: 'DataPlaneRequests'
        enabled: true
      }
      {
        category: 'QueryRuntimeStatistics'
        enabled: true
      }
      {
        category: 'ControlPlaneRequests'
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

@description('Cosmos DB account name')
output accountName string = cosmosAccount.name

@description('Cosmos DB account endpoint')
output endpoint string = cosmosAccount.properties.documentEndpoint

@description('Cosmos DB account resource ID')
output accountId string = cosmosAccount.id
