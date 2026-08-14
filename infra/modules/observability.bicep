param location string
param environmentName string
param alertEmailAddress string
param monthlyBudgetAmount int = 165
param budgetStartDate string = utcNow('yyyy-MM-01')

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'law-auspex-${environmentName}'
  location: location
  properties: {
    retentionInDays: 30
    sku: {
      name: 'PerGB2018'
    }
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-auspex-${environmentName}'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
    DisableLocalAuth: true
    IngestionMode: 'LogAnalytics'
  }
}

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'ag-auspex-${environmentName}'
  location: 'global'
  properties: {
    groupShortName: 'auspex'
    enabled: true
    emailReceivers: [
      {
        name: 'owner'
        emailAddress: alertEmailAddress
        useCommonAlertSchema: true
      }
    ]
  }
}

var logAlerts = [
  {
    name: 'run-failed'
    displayName: 'Auspex nightly run failed or timed out'
    severity: 1
    query: 'ContainerAppConsoleLogs_CL | extend payload=parse_json(Log_s) | where tostring(payload.event) == "run_completed" and tostring(payload.status) in ("FAILED", "TIMEOUT")'
    threshold: 0
    window: 'PT15M'
    frequency: 'PT5M'
  }
  {
    name: 'run-degraded'
    displayName: 'Auspex nightly assertions degraded the run'
    severity: 2
    query: 'ContainerAppConsoleLogs_CL | extend payload=parse_json(Log_s) | where tostring(payload.event) == "run_completed" and tostring(payload.status) == "DEGRADED"'
    threshold: 0
    window: 'PT15M'
    frequency: 'PT5M'
  }
  {
    name: 'provider-errors'
    displayName: 'Auspex provider error rate exceeded 20 percent'
    severity: 2
    query: 'ContainerAppConsoleLogs_CL | extend payload=parse_json(Log_s) | where tostring(payload.event) == "provider_summary" | extend rate=todouble(payload.error_rate) | where rate > 0.2'
    threshold: 0
    window: 'PT30M'
    frequency: 'PT15M'
  }
  {
    name: 'no-buy-eligible'
    displayName: 'Auspex found no BUY-eligible securities for five runs'
    severity: 2
    query: 'ContainerAppConsoleLogs_CL | extend payload=parse_json(Log_s) | where tostring(payload.event) == "run_completed" | project TimeGenerated, eligible=toint(payload.buy_eligible) | top 5 by TimeGenerated desc | summarize runs=count(), eligible=sum(eligible) | where runs == 5 and eligible == 0'
    threshold: 0
    window: 'P2D'
    frequency: 'PT12H'
  }
]

resource scheduledAlerts 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = [
  for alert in logAlerts: {
    name: 'alert-auspex-${alert.name}'
    location: location
    properties: {
      displayName: alert.displayName
      description: alert.displayName
      severity: alert.severity
      enabled: true
      skipQueryValidation: true
      evaluationFrequency: alert.frequency
      scopes: [
        workspace.id
      ]
      windowSize: alert.window
      criteria: {
        allOf: [
          {
            query: alert.query
            timeAggregation: 'Count'
            operator: 'GreaterThan'
            threshold: alert.threshold
            failingPeriods: {
              numberOfEvaluationPeriods: 1
              minFailingPeriodsToAlert: 1
            }
          }
        ]
      }
      autoMitigate: true
      actions: {
        actionGroups: [
          actionGroup.id
        ]
      }
    }
  }
]

resource budget 'Microsoft.Consumption/budgets@2023-11-01' = {
  name: 'budget-auspex-${environmentName}-monthly'
  properties: {
    category: 'Cost'
    amount: monthlyBudgetAmount
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: '${budgetStartDate}T00:00:00Z'
      endDate: dateTimeAdd('${budgetStartDate}T00:00:00Z', 'P5Y')
    }
    notifications: {
      Actual80Percent: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 80
        thresholdType: 'Actual'
        contactEmails: [
          alertEmailAddress
        ]
        contactGroups: [
          actionGroup.id
        ]
      }
      Forecast100Percent: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 100
        thresholdType: 'Forecasted'
        contactEmails: [
          alertEmailAddress
        ]
        contactGroups: [
          actionGroup.id
        ]
      }
    }
  }
}

output workspaceId string = workspace.id
output logAnalyticsCustomerId string = workspace.properties.customerId
@secure()
output logAnalyticsSharedKey string = workspace.listKeys().primarySharedKey
output applicationInsightsId string = applicationInsights.id
output actionGroupId string = actionGroup.id
