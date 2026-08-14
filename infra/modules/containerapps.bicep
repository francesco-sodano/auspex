param location string
param environmentName string
param infrastructureSubnetId string
param logAnalyticsCustomerId string
@secure()
param logAnalyticsSharedKey string
param containerImage string
param registryServer string
param cosmosEndpoint string
param storageAccountUrl string
param keyVaultUri string
param sourceLedgerCosmosEndpoint string
param sourceLedgerDatabaseName string
param openAiEndpoint string
param authClientId string
param authTenantId string
param authAuthority string
param authIssuer string
param authJwksUrl string
param ownerProviderUserId string
param secEdgarUserAgent string

var commonEnvironment = [
  {
    name: 'AUSPEX_ENVIRONMENT'
    value: 'production'
  }
  {
    name: 'AUSPEX_COSMOS_ACCOUNT_ENDPOINT'
    value: cosmosEndpoint
  }
  {
    name: 'AUSPEX_COSMOS_DATABASE_NAME'
    value: 'auspex'
  }
  {
    name: 'AUSPEX_BLOB_ACCOUNT_URL'
    value: storageAccountUrl
  }
  {
    name: 'AUSPEX_KEY_VAULT_URL'
    value: keyVaultUri
  }
  {
    name: 'AUSPEX_PORTFOLIO_COSMOS_ENDPOINT'
    value: sourceLedgerCosmosEndpoint
  }
  {
    name: 'AUSPEX_PORTFOLIO_COSMOS_DATABASE'
    value: sourceLedgerDatabaseName
  }
  {
    name: 'AUSPEX_PORTFOLIO_MAPPING'
    value: '/app/config/portfolio_mapping.yaml'
  }
  {
    name: 'AUSPEX_AOAI_ENDPOINT'
    value: openAiEndpoint
  }
  {
    name: 'AUSPEX_AOAI_DEPLOYMENT_EXTRACTION'
    value: 'gpt-4.1-mini'
  }
  {
    name: 'AUSPEX_AOAI_DEPLOYMENT_NARRATIVE'
    value: 'gpt-4.1'
  }
  {
    name: 'AUSPEX_AOAI_DEPLOYMENT_PLANNER'
    value: 'gpt-4.1-mini'
  }
  {
    name: 'AUSPEX_AOAI_DEPLOYMENT_ANSWER'
    value: 'gpt-4.1'
  }
  {
    name: 'AUSPEX_AOAI_TOKENS_PER_MINUTE'
    value: '200000'
  }
  {
    name: 'AUSPEX_AOAI_NARRATIVE_TOKENS_PER_MINUTE'
    value: '30000'
  }
  {
    name: 'AUSPEX_ENTRA_AUDIENCE'
    value: authClientId
  }
  {
    name: 'AUSPEX_ENTRA_TENANT_ID'
    value: authTenantId
  }
  {
    name: 'AUSPEX_ENTRA_AUTHORITY'
    value: authAuthority
  }
  {
    name: 'AUSPEX_ENTRA_ISSUER'
    value: authIssuer
  }
  {
    name: 'AUSPEX_ENTRA_JWKS_URL'
    value: authJwksUrl
  }
  {
    name: 'AUSPEX_OWNER_PROVIDER_USER_ID'
    value: ownerProviderUserId
  }
  {
    name: 'AUSPEX_EDGAR_USER_AGENT'
    value: secEdgarUserAgent
  }
  {
    name: 'AUSPEX_PRICE_API_KEY_SECRET'
    value: 'ALPHAVANTAGE-API-KEY'
  }
  {
    name: 'AUSPEX_NEWS_API_KEY_SECRET'
    value: 'FINNHUB-API-KEY'
  }
]

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: environmentName
  location: location
  properties: {
    vnetConfiguration: {
      infrastructureSubnetId: infrastructureSubnetId
      internal: false
    }
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsCustomerId
        sharedKey: logAnalyticsSharedKey
      }
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    zoneRedundant: false
  }
}

resource api 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'app-auspex-${environmentName}-api'
  location: location
  tags: {
    'azd-env-name': environmentName
    'azd-service-name': 'api'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    environmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8080
        exposedPort: 0
        transport: 'auto'
        allowInsecure: false
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      registries: [
        {
          server: registryServer
          identity: 'system'
        }
      ]
      maxInactiveRevisions: 3
    }
    template: {
      containers: [
        {
          name: 'api'
          image: containerImage
          env: commonEnvironment
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: 8080
                scheme: 'HTTP'
              }
              initialDelaySeconds: 10
              periodSeconds: 30
              timeoutSeconds: 5
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/healthz'
                port: 8080
                scheme: 'HTTP'
              }
              initialDelaySeconds: 5
              periodSeconds: 10
              timeoutSeconds: 5
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 2
        rules: [
          {
            name: 'http'
            http: {
              metadata: {
                concurrentRequests: '20'
              }
            }
          }
        ]
      }
    }
  }
}

resource pipeline 'Microsoft.App/jobs@2024-03-01' = {
  name: 'job-auspex-${environmentName}-pipeline'
  location: location
  tags: {
    'azd-env-name': environmentName
    'azd-service-name': 'pipeline'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    environmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Schedule'
      replicaTimeout: 21600
      replicaRetryLimit: 1
      scheduleTriggerConfig: {
        cronExpression: '0 2 * * *'
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: [
        {
          server: registryServer
          identity: 'system'
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'pipeline'
          image: containerImage
          command: [
            'python'
            '-m'
            'auspex'
          ]
          args: [
            'nightly'
          ]
          env: commonEnvironment
          resources: {
            cpu: json('2')
            memory: '4Gi'
          }
        }
      ]
    }
  }
}

resource performance 'Microsoft.App/jobs@2024-03-01' = {
  name: 'job-auspex-${environmentName}-performance'
  location: location
  tags: {
    'azd-env-name': environmentName
    'azd-service-name': 'performance'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    environmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Schedule'
      replicaTimeout: 1800
      replicaRetryLimit: 1
      scheduleTriggerConfig: {
        cronExpression: '0 3 * * 0'
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: [
        {
          server: registryServer
          identity: 'system'
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'performance'
          image: containerImage
          command: [
            'python'
            '-m'
            'auspex'
          ]
          args: [
            'performance'
          ]
          env: commonEnvironment
          resources: {
            cpu: json('1')
            memory: '2Gi'
          }
        }
      ]
    }
  }
}

output apiName string = api.name
output apiFqdn string = api.properties.configuration.ingress.fqdn
output apiPrincipalId string = api.identity.principalId
output pipelineJobName string = pipeline.name
output pipelinePrincipalId string = pipeline.identity.principalId
output performanceJobName string = performance.name
output performancePrincipalId string = performance.identity.principalId
