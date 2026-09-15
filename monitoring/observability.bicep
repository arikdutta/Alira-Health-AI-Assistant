// Phase 5 monitoring: explicit 30-day retention on utterance-bearing tables, and the NLU health workbook.
//
// az deployment group create --resource-group <rg> --template-file monitoring/observability.bicep \
//   --parameters workspaceName=<log-analytics-workspace> appInsightsName=<app-insights-resource>

@description('Log Analytics workspace behind the assistant\'s workspace-based Application Insights resource.')
param workspaceName string

@description('Application Insights resource the API exports telemetry to.')
param appInsightsName string

param location string = resourceGroup().location

@description('Days of retention for tables that can carry (redacted) utterances. No archive tier beyond it.')
@minValue(4)
param utteranceRetentionInDays int = 30

resource workspace 'Microsoft.OperationalInsights/workspaces@2022-10-01' existing = {
  name: workspaceName
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

// AppEvents holds the redacted utterances and terms. AppTraces and AppExceptions are included as a backstop
// in case a log line or exception message ever carries user text.
var utteranceBearingTables = [
  'AppEvents'
  'AppTraces'
  'AppExceptions'
]

resource tableRetention 'Microsoft.OperationalInsights/workspaces/tables@2022-10-01' = [for table in utteranceBearingTables: {
  parent: workspace
  name: table
  properties: {
    plan: 'Analytics'
    retentionInDays: utteranceRetentionInDays
    totalRetentionInDays: utteranceRetentionInDays
  }
}]

resource nluHealthWorkbook 'Microsoft.Insights/workbooks@2023-06-01' = {
  name: guid(resourceGroup().id, appInsights.id, 'alira-nlu-health')
  location: location
  kind: 'shared'
  properties: {
    displayName: 'Alira assistant - NLU health'
    category: 'workbook'
    sourceId: appInsights.id
    serializedData: loadTextContent('alira_nlu_workbook.json')
  }
}

output workbookId string = nluHealthWorkbook.id
