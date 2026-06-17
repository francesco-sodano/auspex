// monitor.bicep — Log Analytics workspace + Application Insights
// Region: Switzerland North (switzerlandnorth) — supported.

@description('Environment name (dev or prod)')
param env string

@description('Azure region for all resources')
param location string

@description('Log Analytics retention in days. Use 30 for dev, 90 for prod.')
param retentionDays int

@description('Email address for operational alerts (build failures, capacity events)')
param alertEmailAddress string = 'auspex@auspex.ai'

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

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'auspex-${env}-ag'
  location: 'global'
  properties: {
    groupShortName: 'auspex'
    enabled: true
    emailReceivers: [
      {
        name: 'ops-email'
        emailAddress: alertEmailAddress
        useCommonAlertSchema: true
      }
    ]
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
    actions: {
      actionGroups: [actionGroup.id]
    }
  }
}

// Alert: Fabric capacity running > 4 hours (cost guard)
resource capacityCostGuardAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: 'auspex-${env}-alert-capacity-runtime'
  location: location
  properties: {
    displayName: 'Fabric capacity running > 4 hours'
    description: 'Fires if CapacityResumed was seen in the last 5h but CapacitySuspended was not.'
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
| where name in ("CapacityResumed", "CapacitySuspended")
| summarize resumed = countif(name == "CapacityResumed"), suspended = countif(name == "CapacitySuspended")
| where resumed > 0 and suspended == 0
| project count_ = 1
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
    actions: {
      actionGroups: [actionGroup.id]
    }
  }
}

@description('Action group resource ID for alert notifications')
output actionGroupId string = actionGroup.id

@description('Resource ID of the Log Analytics workspace')
output workspaceId string = logAnalyticsWorkspace.id

@description('Connection string for Application Insights (used as a Key Vault secret reference in Function Apps)')
output appInsightsConnectionString string = appInsights.properties.ConnectionString

@description('Application Insights resource ID')
output appInsightsId string = appInsights.id

@description('Application Insights instrumentation key')
output appInsightsInstrumentationKey string = appInsights.properties.InstrumentationKey
