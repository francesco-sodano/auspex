// monitor.bicep — Log Analytics workspace + Application Insights
// Region: Switzerland North (switzerlandnorth) — supported.

@description('Environment name (dev or prod)')
param env string

@description('Azure region for all resources')
param location string

@description('Log Analytics retention in days. Use 30 for dev, 90 for prod.')
param retentionDays int

var workspaceName = 'auspex-${env}-law'
var appInsightsName = 'auspex-${env}-ai'

resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: workspaceName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: retentionDays
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalyticsWorkspace.id
    RetentionInDays: retentionDays
    IngestionMode: 'LogAnalytics'
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

// Alert: daily build did not complete by 06:00 CET (05:00 UTC)
resource buildCompletionAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: 'auspex-${env}-alert-build-not-complete'
  location: location
  properties: {
    displayName: 'Daily build did not complete by 06:00 CET'
    description: 'Fires if daily_build_completed metric is not emitted before 05:00 UTC (= 06:00 CET).'
    severity: 1
    enabled: true
    scopes: [appInsights.id]
    evaluationFrequency: 'PT15M'
    windowSize: 'PT1H'
    criteria: {
      allOf: [
        {
          query: '''
customMetrics
| where name == "daily_build_completed"
| where timestamp > ago(1h)
| summarize count()
| where count_ == 0
'''
          timeAggregation: 'Count'
          operator: 'GreaterThanOrEqual'
          threshold: 1
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {}
  }
}

// Alert: Fabric capacity running > 4 hours (cost guard)
resource capacityCostGuardAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: 'auspex-${env}-alert-capacity-runtime'
  location: location
  properties: {
    displayName: 'Fabric capacity running > 4 hours'
    description: 'Fires if the capacity scheduler has not emitted a suspend event within 4 hours of a resume event.'
    severity: 2
    enabled: true
    scopes: [appInsights.id]
    evaluationFrequency: 'PT30M'
    windowSize: 'PT5H'
    criteria: {
      allOf: [
        {
          query: '''
customEvents
| where name == "CapacityResumed"
| where timestamp > ago(5h)
| join kind=leftanti (
    customEvents
    | where name == "CapacitySuspended"
    | where timestamp > ago(5h)
) on $left.timestamp < $right.timestamp
| summarize count()
| where count_ > 0
'''
          timeAggregation: 'Count'
          operator: 'GreaterThanOrEqual'
          threshold: 1
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    actions: {}
  }
}

@description('Resource ID of the Log Analytics workspace')
output workspaceId string = logAnalyticsWorkspace.id

@description('Connection string for Application Insights (used as a Key Vault secret reference in Function Apps)')
output appInsightsConnectionString string = appInsights.properties.ConnectionString

@description('Application Insights resource ID')
output appInsightsId string = appInsights.id

@description('Application Insights instrumentation key')
output appInsightsInstrumentationKey string = appInsights.properties.InstrumentationKey
