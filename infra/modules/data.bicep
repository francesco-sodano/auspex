param location string
param cosmosAccountName string
param storageAccountName string
param logAnalyticsWorkspaceId string

var containers = [
  {
    name: 'securities'
    partitionKey: '/security_id'
  }
  {
    name: 'documents'
    partitionKey: '/security_id'
  }
  {
    name: 'extractions'
    partitionKey: '/security_id'
  }
  {
    name: 'digests'
    partitionKey: '/security_id'
  }
  {
    name: 'narratives'
    partitionKey: '/cache_key'
  }
  {
    name: 'market_daily'
    partitionKey: '/security_id'
  }
  {
    name: 'fundamentals'
    partitionKey: '/security_id'
  }
  {
    name: 'scores'
    partitionKey: '/security_id'
  }
  {
    name: 'leg_changes'
    partitionKey: '/security_id'
  }
  {
    name: 'recommendations'
    partitionKey: '/user_id'
  }
  {
    name: 'portfolio_projection'
    partitionKey: '/user_id'
  }
  {
    name: 'conversations'
    partitionKey: '/user_id'
  }
  {
    name: 'performance'
    partitionKey: '/metric_type'
  }
  {
    name: 'user_performance'
    partitionKey: '/user_id'
  }
  {
    name: 'runs'
    partitionKey: '/run_date'
  }
  {
    name: 'config_versions'
    partitionKey: '/config_type'
  }
  {
    name: 'watermarks'
    partitionKey: '/scope'
  }
  {
    name: 'user_settings'
    partitionKey: '/user_id'
  }
  // --- multi-user administration (arc42 §5.7) ---------------------------------
  // One logical partition per user. Every request path reaches these by point
  // read or single-partition query on the caller's own user_id, so none of them
  // is ever scanned cross-partition.
  {
    name: 'app_users'
    partitionKey: '/user_id'
  }
  {
    name: 'onboarding'
    partitionKey: '/user_id'
  }
  {
    name: 'recommendation_dispositions'
    partitionKey: '/user_id'
  }
  {
    name: 'deletion_jobs'
    partitionKey: '/user_id'
  }
  {
    name: 'audit_events'
    partitionKey: '/user_id'
  }
  // Administrator roster + the singleton admin-authority binding, in one small
  // logical partition ('registry') so listing users is a single-partition query
  // rather than a cross-partition scan of app_users — and so no private user
  // data is co-located with the roster.
  {
    name: 'app_user_index'
    partitionKey: '/scope'
  }
]

// Indexing overrides for the multi-user containers. Each one indexes exactly
// the fields its queries filter or order on, and excludes the free-form
// payloads (onboarding drafts, audit detail, deletion target lists) that are
// only ever read back by point read on the partition key.
var indexingOverrides = {
  app_user_index: {
    indexingMode: 'consistent'
    automatic: true
    includedPaths: [
      {
        path: '/scope/?'
      }
      {
        path: '/kind/?'
      }
      {
        path: '/status/?'
      }
      {
        path: '/role/?'
      }
      {
        path: '/registered_at/?'
      }
    ]
    excludedPaths: [
      {
        path: '/*'
      }
    ]
    compositeIndexes: [
      [
        {
          path: '/status'
          order: 'ascending'
        }
        {
          path: '/registered_at'
          order: 'ascending'
        }
      ]
    ]
  }
  app_users: {
    indexingMode: 'consistent'
    automatic: true
    includedPaths: [
      {
        path: '/user_id/?'
      }
      {
        path: '/status/?'
      }
      {
        path: '/role/?'
      }
    ]
    excludedPaths: [
      {
        path: '/*'
      }
    ]
  }
  onboarding: {
    indexingMode: 'consistent'
    automatic: true
    includedPaths: [
      {
        path: '/user_id/?'
      }
    ]
    excludedPaths: [
      {
        path: '/*'
      }
    ]
  }
  recommendation_dispositions: {
    indexingMode: 'consistent'
    automatic: true
    includedPaths: [
      {
        path: '/user_id/?'
      }
      {
        path: '/security_id/?'
      }
      {
        path: '/decision_signature/?'
      }
      {
        path: '/expires_at/?'
      }
    ]
    excludedPaths: [
      {
        path: '/*'
      }
    ]
  }
  deletion_jobs: {
    indexingMode: 'consistent'
    automatic: true
    includedPaths: [
      {
        path: '/user_id/?'
      }
      {
        path: '/status/?'
      }
    ]
    excludedPaths: [
      {
        path: '/*'
      }
    ]
  }
  audit_events: {
    indexingMode: 'consistent'
    automatic: true
    includedPaths: [
      {
        path: '/user_id/?'
      }
      {
        path: '/subject_user_id/?'
      }
      {
        path: '/occurred_at/?'
      }
    ]
    excludedPaths: [
      {
        path: '/*'
      }
    ]
    compositeIndexes: [
      [
        {
          path: '/user_id'
          order: 'ascending'
        }
        {
          path: '/occurred_at'
          order: 'descending'
        }
      ]
    ]
  }
}

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-08-15' = {
  name: cosmosAccountName
  location: location
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    disableLocalAuth: true
    publicNetworkAccess: 'Disabled'
    enableAutomaticFailover: false
    minimalTlsVersion: 'Tls12'
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
    backupPolicy: {
      type: 'Continuous'
      continuousModeProperties: {
        tier: 'Continuous7Days'
      }
    }
  }
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-08-15' = {
  parent: cosmos
  name: 'auspex'
  properties: {
    resource: {
      id: 'auspex'
    }
  }
}

resource cosmosContainers 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-08-15' = [
  for container in containers: {
    parent: database
    name: container.name
    properties: {
      resource: union({
        id: container.name
        partitionKey: {
          paths: [
            container.partitionKey
          ]
          kind: 'Hash'
          version: container.name == 'narratives' ? 1 : 2
        }
        // Multi-user containers narrow their indexing to the fields actually
        // filtered or ordered on. Everything else keeps the default index-all
        // policy so existing containers are not re-indexed by this change.
        indexingPolicy: indexingOverrides[?container.name] ?? {
          indexingMode: 'consistent'
          automatic: true
          includedPaths: [
            {
              path: '/*'
            }
          ]
          excludedPaths: [
            {
              path: '/"_etag"/?'
            }
          ]
        }
      }, container.name == 'conversations' ? {
        defaultTtl: 1296000
      } : {})
    }
  }
]

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Disabled'
    supportsHttpsTrafficOnly: true
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Deny'
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    deleteRetentionPolicy: {
      enabled: true
      days: 30
    }
    containerDeleteRetentionPolicy: {
      enabled: true
      days: 30
    }
    isVersioningEnabled: true
  }
}

resource blobContainers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [
  for name in [
    'documents'
    'sections'
    'exports'
  ]: {
    parent: blobService
    name: name
    properties: {
      publicAccess: 'None'
    }
  }
]

resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'cool-evidence-after-90-days'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: {
              blobTypes: [
                'blockBlob'
              ]
            }
            actions: {
              baseBlob: {
                tierToCool: {
                  daysAfterModificationGreaterThan: 90
                }
              }
            }
          }
        }
      ]
    }
  }
}

resource cosmosDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-${cosmos.name}'
  scope: cosmos
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        categoryGroup: 'audit'
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

resource storageDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'diag-${storage.name}'
  scope: storage
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    metrics: [
      {
        category: 'Transaction'
        enabled: true
      }
    ]
  }
}

output cosmosAccountName string = cosmos.name
output cosmosAccountId string = cosmos.id
output cosmosEndpoint string = cosmos.properties.documentEndpoint
output storageAccountName string = storage.name
output storageAccountId string = storage.id
output storageAccountUrl string = 'https://${storage.name}.blob.${environment().suffixes.storage}'
